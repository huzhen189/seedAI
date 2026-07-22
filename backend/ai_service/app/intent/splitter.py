"""意图拆分器(§多意图 v1.0)。

两阶段:
- Stage 1 轻量门控(规则, 零 LLM): 检测用户输入是否命中 ≥2 个不同意图大类关键词。
  命中才进入 Stage 2, 否则直接判定为单意图(零额外延迟)。
- Stage 2 深度拆分(LLM): 输出结构化 SubTask[] JSON, 含约束:
  不同目标才拆 / 子任务原子可独立执行 / 依赖方声明上游产出 / 上限 3 个 / 补齐上下文。

对外入口: split_intent() / _maybe_split()(由 pipeline 调用)。
对外模型: core/models.py 的 SubTask / SplitResult。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from ..core.models import RISK_HIGH, RISK_LOW, RISK_MEDIUM, SplitResult, SubTask
from ..providers import get_chat_model, resolve_fallback_order
from .common import VALID_INDUSTRIES, VALID_LEVEL1, VALID_LEVEL2
from .tools import run_tools

logger = logging.getLogger("ai_service.intent.splitter")

# 可用 skill 白名单(供 LLM 映射到具体 skill, 单一来源)
SKILL_WHITELIST = (
    "explain(解释/闲聊/对比/排查), write_code(写代码/组件/跨语言翻译), "
    "fix_agent(修Bug), review_agent(代码评审/重构), design_agent(UI设计/配色建议), "
    "search_agent(联网搜索查资料), generate_site(生成网站/页面/游戏/修改已有站), "
    "requirement_agent(需求分析), generate_doc(写文档/README/教程/方案), "
    "rag_retrieve(检索知识库)"
)

MAX_SUBTASKS = 3  # 拆分粒度上限(约束 3)

# 轻量门控: 各意图大类的触发关键词(命中 ≥2 类 → 疑似多意图)
_GATE_KEYWORDS: dict[str, list[str]] = {
    "build": ["网站", "博客", "官网", "落地页", "页面", "建站", "生成站", "做个站", "网页", "landing", "主页"],
    "doc": ["文档", "说明书", "部署文档", "readme", "教程", "方案书", "计划书", "写个文档"],
    "code": ["代码", "函数", "脚本", "修复", "改一下", "bug", "优化代码", "写个组件", "snippet"],
    "learn": ["解释", "怎么", "为什么", "什么是", "教程", "学习", "讲讲"],
    "translate": ["翻译", "译成", "translate", "翻成"],
    "design": ["配色", "设计风格", "布局建议", "字体推荐", "动效方案"],
    "search": ["搜索", "查一下", "帮我查", "最新", "最近有什么"],
}

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
    f"{SKILL_WHITELIST}\n\n"
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


def _extract_last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "") or ""
    return ""


def _lightweight_multi_check(messages: list[dict]) -> bool:
    """Stage 1: 规则门控(零 LLM)。命中 ≥2 个意图大类关键词 → 疑似多意图。"""
    text = _extract_last_user(messages)
    if not text:
        return False
    hits = set()
    for intent, kws in _GATE_KEYWORDS.items():
        if any(kw in text for kw in kws):
            hits.add(intent)
    # 排除单类内重复命中; 需要跨 ≥2 类才判多意图
    return len(hits) >= 2


def _normalize_sub_task(raw: dict, idx: int, base_industry: str) -> Optional[SubTask]:
    """把一个 LLM 输出的子任务字典规整为 SubTask(校验 skill/intent 合法性)。"""
    skill = str(raw.get("skill", "")).strip()
    l1 = str(raw.get("level1", "")).strip()
    l2 = str(raw.get("level2", "")).strip()
    industry = str(raw.get("industry", base_industry)).strip() or "other"
    if industry not in VALID_INDUSTRIES:
        industry = "other"

    # skill 合法性校验: 不在白名单 → 用 run_tools 按意图重新映射
    from ..registry import SkillRegistry
    if not SkillRegistry.get(skill):
        if l1 in VALID_LEVEL1 and l2 in VALID_LEVEL2:
            tools = run_tools(l1, l2, 0.8, industry=industry, project_status="draft")
            if tools.skills:
                skill = tools.skills[0].name
        if not SkillRegistry.get(skill):
            skill = "explain"  # 最终兜底

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
        selected_skill=skill,
        context_hint=str(raw.get("context_hint", "")).strip(),
        risk_level=risk,
        dependencies=[str(d).strip() for d in raw.get("dependencies", []) if str(d).strip()],
    )


async def split_intent(
    messages: list[dict],
    model_id: str = "deepseek",
    base_industry: str = "other",
) -> SplitResult:
    """Stage 2: LLM 深度拆分。失败时返回 is_multi=False(不阻断主流程)。"""
    user_text = _extract_last_user(messages)
    if not user_text.strip():
        return SplitResult(is_multi=False, reason="无用户输入")

    t0 = time.time()
    order = resolve_fallback_order(model_id)
    last_e: Exception | None = None
    for mid in order:
        try:
            chat = get_chat_model(mid, streaming=False)
            resp = await chat.ainvoke([
                {"role": "system", "content": SPLIT_SYSTEM},
                {"role": "user", "content": f"用户请求: {user_text[:1500]}"},
            ])
            raw = (resp.content or "").strip()
            elapsed = (time.time() - t0) * 1000
            logger.info("[拆分] LLM返回 model=%s 耗时=%.0fms raw=%.200s", mid, elapsed, raw)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
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

            # 依赖引用校验: 仅保留指向真实存在的 sub_id
            valid_ids = {s.id for s in sub_tasks}
            for s in sub_tasks:
                s.dependencies = [d for d in s.dependencies if d in valid_ids and d != s.id]

            # 调度策略推断: 有依赖 → serial/mixed, 否则 parallel
            has_dep = any(s.dependencies for s in sub_tasks)
            strategy = "serial" if has_dep else "parallel"

            logger.info(
                "[拆分] 多意图命中 is_multi=%s tasks=%d strategy=%s",
                is_multi, len(sub_tasks), strategy,
            )
            return SplitResult(
                is_multi=True,
                sub_tasks=sub_tasks,
                split_reason=reason,
                confidence=float(data.get("confidence", 0.8)),
                strategy=strategy,
            )
        except Exception as e:
            last_e = e
            logger.warning("[拆分] 模型%s调用失败: %s", mid, e)
            continue

    # 所有模型失败 → 降级为单意图(不阻断)
    logger.error("[拆分] 全部模型失败, 降级单意图: %s", last_e)
    return SplitResult(is_multi=False, sub_tasks=[], split_reason="拆分LLM不可用, 降级单意图", confidence=0.0)


async def maybe_split(
    messages: list[dict],
    model_id: str,
    base_industry: str = "other",
) -> SplitResult:
    """组合门控: 轻量规则先行, 命中才调 LLM 深拆。供 pipeline._aggregate 之后调用。"""
    if not _lightweight_multi_check(messages):
        return SplitResult(is_multi=False, sub_tasks=[], split_reason="轻量门控: 单意图")
    return await split_intent(messages, model_id, base_industry)
