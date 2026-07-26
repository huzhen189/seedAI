"""多意图识别(A+B 路由, v1.2.5)。

对外唯一入口: recognize_intents(messages, model_id, ...) -> SplitResult。

策略:
  1. 轻量门控(_lightweight_multi_check): 命中 ≥2 个意图大类才进入, 否则单意图(零额外开销)。
  2. 方案 B(混合分层, split_hybrid): 默认快路径。确定性切段 → 逐段复用
     cascade._classify_segment(单意图分类, 不含多意图步骤, 无递归) → 合并相邻同意图续写段
     → 连词/指代推断串行(依赖)/并行(独立)。可解释、零新增分类逻辑。
  3. 升级判定: 方案 B 未识别出 ≥2 个有效子任务, 或平均置信 < split_escalate_low_conf,
     升级方案 A(LLM 深拆, split_by_llm)做全局推理兜底(擅长跨段隐含意图/深层依赖)。
  4. 两路皆失败 → 返回 is_multi=False(上层 classify_v3 当单意图处理)。

source ∈ {"hybrid", "llm"} 供日志/统计区分走了哪条路径。

设计要点: _classify_segment 被复用而非 classify_v3(skip_split=...), 彻底消除了
「逐段分类再次触发拆分」的递归规避 hack, 流程自洽、可维护。
"""

from __future__ import annotations

import json
import logging
import re
import time

from ..core.models import RISK_HIGH, RISK_LOW, RISK_MEDIUM, SplitResult, SubTask
from ..config import settings
from ..providers import get_chat_model, resolve_fallback_order
from .catalog import skill_whitelist
from .common import (
    VALID_INDUSTRIES,
    VALID_LEVEL1,
    VALID_LEVEL2,
    SAFETY_HARD_KEYWORDS,
    SAFETY_SOFT_CRITICAL,
    SAFETY_SOFT_HIGH,
)
from .tools import resolve_skill
from ..analytics import record_multi_intent_path

logger = logging.getLogger("ai_service.intent.multi_intent")


# ──────────────────────────────────────────────────────────────
# 轻量门控: 各意图大类的触发关键词(命中 ≥2 类 → 疑似多意图)
# ──────────────────────────────────────────────────────────────
_GATE_KEYWORDS: dict[str, list[str]] = {
    "build": ["网站", "博客", "官网", "落地页", "页面", "建站", "生成站", "做个站", "网页", "landing", "主页"],
    "doc": ["文档", "说明书", "部署文档", "readme", "教程", "方案书", "计划书", "写个文档"],
    "code": ["代码", "函数", "脚本", "修复", "改一下", "bug", "优化代码", "写个组件", "snippet"],
    "learn": ["解释", "怎么", "为什么", "什么是", "教程", "学习", "讲讲"],
    "translate": ["翻译", "译成", "translate", "翻成"],
    "design": ["配色", "设计风格", "布局建议", "字体推荐", "动效方案"],
    "search": ["搜索", "查一下", "帮我查", "最新", "最近有什么"],
}


def _lightweight_multi_check(messages: list[dict]) -> bool:
    """Stage 1: 规则门控(零 LLM)。命中 ≥2 个意图大类关键词 → 疑似多意图。"""
    from .common import last_user_message
    text = last_user_message(messages)
    if not text:
        return False
    hits = set()
    for intent, kws in _GATE_KEYWORDS.items():
        if any(kw in text for kw in kws):
            hits.add(intent)
    return len(hits) >= 2


# ──────────────────────────────────────────────────────────────
# 方案 B: 混合分层(确定性切段 + 逐段复用单意图分类器 + 连词/指代连依赖)
# ──────────────────────────────────────────────────────────────
_SERIAL_CUES = (
    "然后", "接着", "之后", "再", "最后", "随后", "方才", "继而",
    "先", "步骤", "做完", "等", "其后", "过后", "完了",
)
_PARALLEL_CUES = (
    "另外", "同时", "并且", "还有", "此外", "顺便", "顺带",
    "一方面", "另一方面", "以及", "加上", "与此同时", "顺手", "除此之外",
)
_REFERENCE_WORDS = (
    "刚才", "上面", "前述", "上述", "之前", "之前的", "刚生成", "刚做", "刚写",
    "刚创建", "那份", "这个站", "这个页面", "产出的", "生成的", "上面那个",
    "前述的", "刚搭", "刚产出",
)
_SPLIT_BEFORE = (
    "另外", "同时", "并且", "还有", "此外", "顺便", "顺带",
    "一方面", "另一方面", "以及", "加上", "然后", "接着", "之后", "随后",
    "先", "再", "最后", "步骤",
)
_SPLIT_RE = re.compile(r"(?<=[\u4e00-\u9fff])(" + "|".join(_SPLIT_BEFORE) + r")")
_CUE_LEAD_RE = re.compile(
    r"^(另外|同时|并且|还有|此外|顺便|顺带|一方面|另一方面|以及|加上|"
    r"然后|接着|之后|随后|先|再|最后|步骤\s*\d+)"
)
_SEG_RE = re.compile(r"[\n；;。！？!?]+")


def _segment_text(text: str) -> list[dict]:
    """把超长文本切成候选段, 保留每段前导连接词(cue)。

    返回: [{"text": str, "cue": str}, ...] (cue 为该段开头的串行/并行连接词, 无则 "")
    """
    prefixed = _SPLIT_RE.sub(r"\n\1", text)
    parts = _SEG_RE.split(prefixed)
    out: list[dict] = []
    for p in parts:
        s = (p or "").strip()
        if len(s) < 2:
            continue
        cue = ""
        m = _CUE_LEAD_RE.match(s)
        if m:
            cue = m.group(1).strip()
            s = s[m.end():].strip()
        if len(s) < 2:
            continue
        out.append({"text": s, "cue": cue})
    return out


def _infer_risk(text: str, level2: str) -> str:
    """确定性风险推断(复用 common 安全关键词集, 零 LLM)。

    删除/危险词 → high; 改已有逻辑/高危词 → medium; 纯新增 → low。
    """
    if any(kw in text for kw in SAFETY_HARD_KEYWORDS):
        return RISK_HIGH
    if level2 in ("fix", "modify", "review", "delete"):
        return RISK_MEDIUM
    if any(kw in text for kw in SAFETY_SOFT_CRITICAL) or any(kw in text.lower() for kw in SAFETY_SOFT_HIGH):
        return RISK_MEDIUM
    return RISK_LOW


async def split_hybrid(
    messages: list[dict],
    model_id: str = "deepseek",
    *,
    base_industry: str = "other",
    project_status: str = "draft",
    has_requirement_doc: bool = False,
    project_constraints: list[str] | None = None,
    conversation_id: int | None = None,
    user_id: int | None = None,
    project_id: int | None = None,
) -> SplitResult:
    """方案 B: 混合分层识别。逐段复用 cascade._classify_segment, 返回 source="hybrid"。

    流程: 切段 → 逐段 _classify_segment → 合并相邻同意图续写段 → 连词/指代连依赖。
    """
    if not settings.split_b_enabled:
        return SplitResult(is_multi=False, sub_tasks=[], split_reason="方案B 已禁用", source="hybrid")

    from .common import last_user_message
    user_text = last_user_message(messages)
    if not user_text.strip():
        return SplitResult(is_multi=False, sub_tasks=[], split_reason="无用户输入", source="hybrid")

    t0 = time.time()
    # 延迟导入, 复用单意图分类器(不含拆分步骤, 无递归)
    from .cascade import _classify_segment

    segments = _segment_text(user_text)
    if len(segments) < 2:
        return SplitResult(
            is_multi=False, sub_tasks=[],
            split_reason=f"方案B: 切分仅 {len(segments)} 段, 单意图", source="hybrid",
        )

    # ── 逐段分类(复用 _classify_segment, conversation_id=None 避免污染共享槽位) ──
    classified: list[dict] = []
    for seg in segments:
        try:
            pr = await _classify_segment(
                [{"role": "user", "content": seg["text"]}],
                model_id,
                project_status=project_status,
                has_requirement_doc=has_requirement_doc,
                project_constraints=project_constraints,
                conversation_id=None,
            )
            intent = pr.intent
            l1 = intent.get("level1", "chat")
            l2 = intent.get("level2", "casual")
            industry = intent.get("industry", base_industry) or base_industry
            if industry not in VALID_INDUSTRIES:
                industry = "other"
            classified.append({
                "text": seg["text"], "cue": seg["cue"],
                "l1": l1, "l2": l2, "industry": industry,
                "skill": pr.selected_skill,
                "conf": float(intent.get("confidence", 0.3)),
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("[方案B] 片段分类失败, 跳过: %s", e)
            continue

    if len(classified) < 2:
        return SplitResult(
            is_multi=False, sub_tasks=[],
            split_reason="方案B: 有效片段 <2, 单意图", source="hybrid",
        )

    # ── 合并相邻「同意图 + 无连接词」的续写段(句号续写属同一目标) ──
    blocks: list[dict] = []
    for c in classified:
        if (blocks and blocks[-1]["l1"] == c["l1"] and blocks[-1]["l2"] == c["l2"]
                and not c["cue"]):
            blocks[-1]["texts"].append(c["text"])
            blocks[-1]["min_conf"] = min(blocks[-1]["min_conf"], c["conf"])
        else:
            blocks.append({
                "l1": c["l1"], "l2": c["l2"], "industry": c["industry"],
                "skill": c["skill"], "cue": c["cue"],
                "texts": [c["text"]], "min_conf": c["conf"],
            })

    # ── 依赖链接: 串行/并行判定(依赖=数据/控制流, 非仅连词) ──
    sub_tasks: list[SubTask] = []
    for idx, b in enumerate(blocks):
        deps: list[str] = []
        combined = "；".join(b["texts"])
        if idx > 0:
            if any(w in combined for w in _REFERENCE_WORDS):
                deps = [sub_tasks[-1].id]  # 指代前文产出 → 依赖最近前置块
            elif b["cue"] and b["cue"] in _SERIAL_CUES:
                deps = [sub_tasks[-1].id]  # 显式串行连接词 → 依赖上一块(保序)
            # 并行连接词(另外/同时/并且) 或 无连接词 → 不连边(并行/独立)
        sub_tasks.append(SubTask(
            id=f"sub_{idx}",
            goal=combined[:60],
            original_text=combined,
            level1=b["l1"] if b["l1"] in VALID_LEVEL1 else "learn",
            level2=b["l2"] if b["l2"] in VALID_LEVEL2 else "casual",
            industry=b["industry"],
            selected_skill=b["skill"],
            context_hint=(f"依赖前置子任务 {deps} 的产出" if deps else ""),
            risk_level=_infer_risk(combined, b["l2"]),
            dependencies=deps,
        ))

    # ── 上限截断(超长保护) ──
    if len(sub_tasks) > settings.split_b_max_subtasks:
        kept = sub_tasks[: settings.split_b_max_subtasks]
        valid_ids = {s.id for s in kept}
        for s in kept:
            s.dependencies = [d for d in s.dependencies if d in valid_ids and d != s.id]
        trunc_note = f"；复杂度过高, 仅保留前 {len(kept)} 个(建议分步对话)"
        sub_tasks = kept
    else:
        trunc_note = ""

    has_dep = any(s.dependencies for s in sub_tasks)
    strategy = "mixed" if has_dep else "parallel"
    avg_conf = sum(b["min_conf"] for b in blocks[:len(sub_tasks)]) / max(len(sub_tasks), 1)

    logger.info(
        "[方案B] 完成 段=%d 块=%d 子任务=%d 策略=%s 平均置信=%.2f 耗时=%.0fms",
        len(segments), len(blocks), len(sub_tasks), strategy, avg_conf,
        (time.time() - t0) * 1000,
    )
    return SplitResult(
        is_multi=len(sub_tasks) >= 2,
        sub_tasks=sub_tasks,
        split_reason=f"混合分层切分{trunc_note}",
        confidence=round(avg_conf, 2),
        strategy=strategy,
        source="hybrid",
    )


# ──────────────────────────────────────────────────────────────
# 方案 A: LLM 深度拆分(全局推理, 含 Schema 校验 + 自愈修复环)
# ──────────────────────────────────────────────────────────────
MAX_SUBTASKS = 3  # 拆分粒度上限(约束: 单次最多 3 个)

SPLIT_SYSTEM = (
    "你是智能建站助手小胡的『多意图拆解器』。判断用户请求是否包含多个独立可交付目标, "
    "若是则拆成可独立执行的子任务。\n\n"
    "## 拆分约束(必须严格遵守)\n"
    "1. 不同目标原则: 仅当用户请求涉及 2 个及以上『独立可交付目标』才拆分。\n"
    "   - 例(应拆): \"做个个人博客, 再写份部署文档\" → 博客(建站) + 文档(写文档)\n"
    "   - 例(应拆): \"生成电商站, 包含商品页和购物车\" → 仍是 1 个目标(电商站) → 不拆\n"
    "   - 例(不拆): \"做个好看的博客\" → 1 个目标 → 不拆\n"
    "2. 原子性原则: 每个子任务必须能独立执行并产出可交付结果。\n"
    "   若 B 依赖 A 的产出(如『根据生成的代码写文档』), B.dependencies=[A的id], "
    "   且 B.context_hint 须注明需要 A 的什么产出。\n"
    "3. 粒度上限: 单次最多拆 3 个。超过则合并为 1 个, reason 说明『复杂度过高, 建议分多次对话』。\n"
    "4. 上下文补全: 每个子任务 context_hint 必须含该子任务执行所需的全部上下文\n"
    "   (项目背景 / 已有文件 / 对其他子任务产出的引用)。\n"
    "5. 风险分级: risk_level 按操作影响判定 high/medium/low:\n"
    "   - high: 删库/改表/删文件/认证核心/支付/权限变更/环境变量覆写\n"
    "   - medium: 改已有代码逻辑/数据库schema变更/新增依赖包/端口变更\n"
    "   - low: 纯新增内容(新页面/文档/查代码/解释)\n\n"
    "## 可用 skill(从以下选, 不要编造)\n"
    f"{skill_whitelist()}\n\n"
    "## 输出格式(只返回 JSON, 不要多余文字)\n"
    "{\n"
    '  "is_multi": true/false,\n'
    '  "reason": "为什么拆 / 为什么不拆",\n'
    '  "sub_tasks": [\n'
    "    {\n"
    '      "goal": "该子任务目标(简短)",\n'
    '      "original_text": "从用户输入摘出的对应片段",\n'
    '      "level1": "build|code|doc|learn|translate",\n'
    '      "level2": "对应子意图(如 site/page/doc/readme/...)",\n'
    '      "industry": "13选1 或 other/none",\n'
    '      "skill": "generate_site/generate_doc/... (从白名单选)",\n'
    '      "context_hint": "该子任务专属上下文(补齐自洽所需)",\n'
    '      "risk_level": "low/medium/high",\n'
    '      "dependencies": []\n'
    "    }\n"
    "  ]\n"
    "}\n"
)


def _normalize_sub_task(raw: dict, idx: int, base_industry: str) -> SubTask | None:
    """把一个 LLM 输出的子任务字典规整为 SubTask(校验 skill/intent 合法性)。"""
    skill = str(raw.get("skill", "")).strip()
    l1 = str(raw.get("level1", "")).strip()
    l2 = str(raw.get("level2", "")).strip()
    industry = str(raw.get("industry", base_industry)).strip() or "other"
    if industry not in VALID_INDUSTRIES:
        industry = "other"
    risk = str(raw.get("risk_level", RISK_LOW)).strip().lower()
    if risk not in (RISK_HIGH, RISK_MEDIUM, RISK_LOW):
        risk = RISK_LOW
    return SubTask(
        id=f"sub_{idx}",
        goal=str(raw.get("goal", "")).strip(),
        original_text=str(raw.get("original_text", "")).strip(),
        level1=l1 if l1 in VALID_LEVEL1 else "learn",
        level2=l2 if l2 in VALID_LEVEL2 else "casual",
        industry=industry,
        selected_skill=resolve_skill(l1, l2, candidate=skill),
        context_hint=str(raw.get("context_hint", "")).strip(),
        risk_level=risk,
        dependencies=[str(d).strip() for d in raw.get("dependencies", []) if str(d).strip()],
    )


async def split_by_llm(
    messages: list[dict],
    model_id: str = "deepseek",
    base_industry: str = "other",
) -> SplitResult:
    """方案 A: LLM 深度拆分(含 JSON 非法 / 结构缺失自愈修复环)。失败时返回 is_multi=False。"""
    from .common import last_user_message
    user_text = last_user_message(messages)
    if not user_text.strip():
        return SplitResult(is_multi=False, sub_tasks=[], split_reason="无用户输入")

    t0 = time.time()
    order = resolve_fallback_order(model_id)
    last_e: Exception | None = None

    for mid in order:
        data = None
        last_raw = ""
        repair_round = 0
        # ── 自愈修复环: JSON 非法 / 结构缺失 → 回喂错误重生成(上限 split_repair_max_rounds) ──
        while repair_round <= settings.split_repair_max_rounds:
            try:
                chat = get_chat_model(mid, streaming=False)
                user_content = f"用户请求: {user_text[:1500]}"
                if repair_round > 0:
                    user_content += (
                        f"\n\n[上次输出无法解析为合法 JSON, 请严格只返回单个 JSON 对象, "
                        f"不要任何多余文字]\n上次输出片段: {last_raw[:500]}"
                    )
                resp = await chat.ainvoke([
                    {"role": "system", "content": SPLIT_SYSTEM},
                    {"role": "user", "content": user_content},
                ])
                raw = (resp.content or "").strip()
                last_raw = raw
                elapsed = (time.time() - t0) * 1000
                logger.info("[拆分A] LLM返回 model=%s 轮=%d 耗时=%.0fms raw=%.200s",
                            mid, repair_round, elapsed, raw)
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if not m:
                    repair_round += 1
                    last_e = ValueError("未找到 JSON 对象")
                    continue
                parsed = json.loads(m.group(0))
                if not isinstance(parsed.get("sub_tasks"), list):
                    repair_round += 1
                    last_e = ValueError("sub_tasks 字段缺失或非列表")
                    continue
                data = parsed
                break
            except Exception as e:  # noqa: BLE001
                last_e = e
                repair_round += 1
                logger.warning("[拆分A] 模型%s 解析/调用失败(轮%d): %s", mid, repair_round, e)
                continue
        if data is None:
            continue  # 该 provider 自愈失败, 尝试下一个

        is_multi = bool(data.get("is_multi", False))
        reason = str(data.get("reason", ""))
        raw_tasks = data.get("sub_tasks", []) or []

        if not is_multi or not raw_tasks:
            return SplitResult(is_multi=False, sub_tasks=[], split_reason=reason, confidence=0.6)

        sub_tasks: list[SubTask] = []
        for i, rt in enumerate(raw_tasks[:MAX_SUBTASKS]):
            st = _normalize_sub_task(rt, i, base_industry)
            if st:
                sub_tasks.append(st)
        if not sub_tasks:
            return SplitResult(is_multi=False, sub_tasks=[], split_reason="拆分结果为空", confidence=0.6)

        # 依赖引用校验: 仅保留指向真实存在的 sub_id(并剔除自依赖)
        valid_ids = {s.id for s in sub_tasks}
        for s in sub_tasks:
            s.dependencies = [d for d in s.dependencies if d in valid_ids and d != s.id]

        has_dep = any(s.dependencies for s in sub_tasks)
        strategy = "serial" if has_dep else "parallel"

        logger.info("[拆分A] 多意图命中 is_multi=%s tasks=%d strategy=%s", is_multi, len(sub_tasks), strategy)
        return SplitResult(
            is_multi=True,
            sub_tasks=sub_tasks,
            split_reason=reason,
            confidence=0.85,
            strategy=strategy,
            source="llm",
        )

    # 所有模型失败 → 降级为单意图(不阻断)
    logger.error("[拆分A] 全部模型失败, 降级单意图: %s", last_e)
    return SplitResult(is_multi=False, sub_tasks=[], split_reason="拆分LLM不可用, 降级单意图", confidence=0.0)


# ──────────────────────────────────────────────────────────────
# A+B 路由: 先方案 B, 必要时升级方案 A
# ──────────────────────────────────────────────────────────────
def _should_escalate(b: SplitResult) -> bool:
    """方案 B 结果是否需要升级到方案 A(LLM 深拆)。"""
    if not b.is_multi or len(b.sub_tasks) < 2:
        return True
    if b.confidence < settings.split_escalate_low_conf:
        return True
    return False


async def recognize_intents(
    messages: list[dict],
    model_id: str = "deepseek",
    *,
    base_industry: str = "other",
    project_status: str = "draft",
    has_requirement_doc: bool = False,
    project_constraints: list[str] | None = None,
    conversation_id: int | None = None,
    user_id: int | None = None,
    project_id: int | None = None,
) -> SplitResult:
    """A+B 路由: 先方案 B, 必要时升级方案 A, 返回统一 SplitResult(source=hybrid|llm)。

    每次门控通过后记一次路径埋点(record_multi_intent_path), 供 A/B 占比统计。
    """
    if not _lightweight_multi_check(messages):
        return SplitResult(is_multi=False, sub_tasks=[], split_reason="轻量门控: 单意图", source="")

    t0 = time.time()
    # ── 方案 B(默认快路径) ──
    try:
        b = await split_hybrid(
            messages, model_id, base_industry=base_industry,
            project_status=project_status, has_requirement_doc=has_requirement_doc,
            project_constraints=project_constraints, conversation_id=conversation_id,
            user_id=user_id, project_id=project_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[A+B] 方案B 异常, 升级方案A: %s", e)
        b = SplitResult(is_multi=False, sub_tasks=[], source="hybrid")

    if not _should_escalate(b):
        dur = (time.time() - t0) * 1000
        logger.info("[A+B] 采用方案B source=hybrid tasks=%d 耗时=%.0fms", len(b.sub_tasks), dur)
        await record_multi_intent_path(
            "hybrid", escalated=False,
            sub_task_count=len(b.sub_tasks), duration_ms=dur,
        )
        return b

    # ── 升级方案 A(LLM 深拆兜底) ──
    try:
        a = await split_by_llm(messages, model_id, base_industry=base_industry)
    except Exception as e:  # noqa: BLE001
        logger.warning("[A+B] 方案A 异常, 退回方案B: %s", e)
        a = SplitResult(is_multi=False, sub_tasks=[], source="llm")

    dur = (time.time() - t0) * 1000
    if a.is_multi and a.sub_tasks:
        logger.info("[A+B] 升级方案A source=llm tasks=%d (方案B 平均置信=%.2f) 耗时=%.0fms",
                    len(a.sub_tasks), b.confidence, dur)
        await record_multi_intent_path(
            "llm", escalated=True,
            sub_task_count=len(a.sub_tasks), duration_ms=dur,
        )
        return a

    # 两路都没拆出多意图 → 退回方案 B 的结果(可能 <2, 上层当单意图)
    logger.info("[A+B] 两路均未识别多意图, 退回方案B(单意图) 耗时=%.0fms", dur)
    await record_multi_intent_path(
        b.source or "hybrid", escalated=True,
        sub_task_count=len(b.sub_tasks), duration_ms=dur,
    )
    return b
