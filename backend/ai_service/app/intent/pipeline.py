"""SIR 意图管道(状态化跨轮意图解析) — 重构核心。

三层管道(见文档 §3.1):
  Layer 1  上下文重建: 从 Redis 加载 IntentState(跨轮信念) → 归一化当前输入 + 先验
  Layer 2  意图理解:   LLM 结构化 NLU(run_semantic)
  Layer 3  规则+策略: 规则五维硬信号(run_rules) + 融合 + 粘性信念更新(update_belief)
                      + 决策(COMMIT/CLARIFY/CHAT/RESET) + 写回 State + 可观测

相对现状/Plan C 的根本差异: **信念跨轮累积 + 粘性抗打断**(见文档 §3.3),
满足"哪怕多轮对话依然明确识别意图"的硬需求。

外部契约保持兼容:
  - detect_intent_v2 仍返回同样的 dict(router.py 不用大改, 仅多透传 clarify 字段)。
  - PipelineResult 仍含 decision / selected_skill / intent / plan / risk / tools /
    sub_tasks / split_reason / evidence, 并新增 clarify_questions / clarify_rounds /
    request_id, 供 Worker 决策分流(新增 clarify 分支)。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from .context import ContextResult, run_context
from .rules import RuleResult, run_rules
from .safety import SafetyResult, run_safety
from .semantic import SemanticResult, run_semantic
from .splitter import maybe_split
from .tools import SkillCandidate, ToolResult, run_tools
from .selection import resolve_selection, clear_pending_options
from .state import (
    IntentState, load_state, save_state, reset_state, update_belief,
    COMMIT_THRESHOLD, CLARIFY_THRESHOLD, CLARIFY_MAX_ROUNDS,
)
from .observation import record as observe_record
from ..core.models import SubTask

logger = logging.getLogger("ai_service.intent.pipeline")

# ── F2 复用: 整站关键词(防止 site→page 误降级) ──
_SITE_KW = ("网站", "官网", "站点", "建站", "生成网站", "门户", "整站", "主页", "首页")
_BUILD_KW = ("网站", "官网", "站点", "建站", "生成网站", "页面", "网页", "落地页",
             "h5", "H5", "主页", "首页", "landing")
_CONTENT_KW = ("天气", "美食", "地图", "定位", "展示", "列表", "预约", "价格", "商品", "联系",
               "关于", "模块", "功能", "板坜", "轮播", "表单", "导航", "评论", "搜索", "登录",
               "注册", "新闻", "博客", "案例", "团队", "服务", "产品", "介绍", "详情", "订单",
               "购物车", "支付", "会员", "课程", "视频", "图片", "下载", "分享", "日历", "日程",
               "签到", "排行", "统计", "图表", "特色", "活动", "资讯", "动态", "留言", "客服",
               "品牌", "风格", "配色", "主题")

# ── RESET 信号(用户显式退出建站/澄清) ──
_RESET_PHRASES = ("随便聊聊", "不用了", "聊天而已", "不做了", "当我没说", "就聊聊天",
                  "只是随便问问", "不用帮我做", "不用做了", "只是问问", "当我没听见",
                  "算了不做了", "不用管了")

# ── 缺失规格 → 动态追问模板 ──
_MISSING_Q: dict[str, str] = {
    "page_count": "需要多少个页面？例如首页、产品页、关于页等。",
    "tech_stack": "有偏好的技术栈吗？例如 React、Vue、WordPress 或纯 HTML/CSS。",
    "style": "希望什么样的视觉风格？例如科技感、简约、活泼、商务。",
    "audience": "面向什么受众？例如 B 端企业客户、C 端个人用户。",
    "deadline": "有上线时间预期吗？",
    "features": "需要哪些核心功能模块？例如表单、搜索、地图定位、支付。",
}
_GENERIC_Q = [
    "为了更精准地帮你, 请问这是什么类型的网站？个人博客、企业官网、电商、还是其他？",
    "大概需要哪些页面和功能？有技术栈偏好吗？",
]


def _last_user_message(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content") or ""
            return c if isinstance(c, str) else ""
    return ""


def _has_conversation_requirement(messages: list[dict]) -> bool:
    """对话里是否存在『可读取的建站需求』(供死亡路由放行判断, RC3)。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content") or ""
        if not isinstance(c, str):
            continue
        if any(kw in c for kw in _BUILD_KW) and any(kw in c for kw in _CONTENT_KW):
            return True
    return False


def _is_reset_signal(text: str) -> bool:
    return any(p in text for p in _RESET_PHRASES)


def _build_clarify_questions(sem: SemanticResult) -> list[str]:
    """动态最少必要追问: 优先用 LLM 给的 questions, 否则按 missing_specs 合成。"""
    if sem.questions:
        return sem.questions[:2]
    qs: list[str] = []
    for k in sem.missing_specs:
        q = _MISSING_Q.get(k)
        if q and q not in qs:
            qs.append(q)
        if len(qs) >= 2:
            break
    if not qs:
        qs = _GENERIC_Q[:2]
    return qs


@dataclass
class PipelineResult:
    intent: dict = field(default_factory=lambda: {"level1": "chat", "level2": "casual", "confidence": 0.3, "industry": "other"})
    plan: list[dict] = field(default_factory=list)
    risk: SafetyResult = field(default_factory=SafetyResult)
    tools: ToolResult = field(default_factory=ToolResult)
    evidence: dict = field(default_factory=dict)
    decision: str = "route"  # "route"|"block"|"confirm"|"options"|"fallback"|"split"|"clarify"
    selected_skill: str = "explain"
    sub_tasks: list = field(default_factory=list)   # 多意图: list[SubTask]
    split_reason: str = ""                            # 拆分原因(供统计/前端展示)
    # ── SIR 新增 ──
    clarify_questions: list = field(default_factory=list)
    clarify_rounds: int = 0
    request_id: str = ""


async def classify_v2(
    messages: list[dict],
    model_id: str = "deepseek",
    *,
    conversation_id: int | None = None,
    context_hint: str = "",
    project_status: str = "draft",
    project_constraints: list[str] | None = None,
    checkpoint_info: dict | None = None,
    user_id: int | None = None,          # v0.9.0: Chroma 用户偏好
    project_id: int | None = None,       # v0.9.0: Chroma 项目记忆
    has_requirement_doc: bool = False,   # v1.0.7: 是否已存在需求文档
) -> PipelineResult:
    t0 = time.time()
    req_id = uuid.uuid4().hex
    current_user_msg = _last_user_message(messages)
    has_conv_req = _has_conversation_requirement(messages)

    logger.info("[SIR] [1/6] 开始 conv=%s %d条消息 model=%s project=%s",
                conversation_id, len(messages), model_id, project_status)

    # ── [0/6] 选项选择短路:用户在回复待选项 / 显式指定 skill → 直接路由(不重分类) ──
    from ..registry import SkillRegistry
    _sel = resolve_selection(
        messages, conversation_id,
        skill_exists=lambda n: SkillRegistry.get(n) is not None,
        known_skills=set(SkillRegistry.names()),
    )
    if _sel is not None:
        _chosen, _cands = _sel
        logger.info("[SIR] [0/6] 命中选项选择 → 短路路由 skill=%s", _chosen)
        clear_pending_options(conversation_id)
        return PipelineResult(
            intent={"level1": "chat", "level2": "casual", "confidence": 1.0, "industry": "other"},
            plan=[{"action": "route", "skill": _chosen, "confidence": 1.0, "from_selection": True}],
            risk=SafetyResult(),
            tools=ToolResult(skills=[SkillCandidate(name=_chosen, confidence=1.0, reason="用户选择/指定")]),
            evidence={"selection": {"chosen": _chosen, "candidates": _cands}},
            decision="route",
            selected_skill=_chosen,
            request_id=req_id,
        )

    # ── [1/6] L1: 加载跨轮信念状态(无 redis/无 conv → None, 退化每轮独立) ──
    prior = await load_state(conversation_id)
    state = prior if prior is not None else IntentState(conv_id=conversation_id or 0)
    belief_before = state.running_conf
    logger.info("[SIR] [1/6] 加载信念 conv=%s prior_conf=%.2f rounds=%d",
                conversation_id, belief_before, state.clarify_rounds)

    # ── [2/6] L2: 发射语义任务(LLM, 异步) ──
    semantic_task = asyncio.create_task(
        run_semantic(messages, model_id, context_hint=context_hint, checkpoint_info=checkpoint_info)
    )

    # ── [3/6] L3 同步规则模块(零延迟, 与语义 LLM 等待期重叠) ──
    rule_result: RuleResult = RuleResult()
    context_result: ContextResult = ContextResult()
    safety_result: SafetyResult = SafetyResult()
    try:
        rule_result = run_rules(messages, project_status=project_status,
                                has_requirement_doc=has_requirement_doc,
                                has_conversation_requirement=has_conv_req)
        context_result = run_context(messages, conversation_id=conversation_id,
                                     frontend_hint=context_hint,
                                     user_id=user_id, project_id=project_id)
        safety_result = run_safety(messages, project_constraints=project_constraints)
    except Exception as e:
        logger.warning("[SIR] [3/6] 规则模块异常: %s", e)

    # ── [4/6] 等语义模块完成 ──
    semantic_result: SemanticResult
    try:
        semantic_result = await asyncio.wait_for(semantic_task, timeout=35.0)
    except asyncio.TimeoutError:
        logger.error("[SIR] 语义模块超时35s → 降级")
        semantic_result = SemanticResult(confidence=0.3)
    except Exception as e:
        logger.error("[SIR] 语义模块异常: %s → 降级", e)
        semantic_result = SemanticResult(confidence=0.3)

    # ── [5/6] 融合 + 粘性信念更新 ──
    rule_score = rule_result.signals.get("score", 0.0)
    cur_score = max(0.0, min(1.0, 0.55 * semantic_result.confidence + 0.45 * rule_score))
    cur_l1, cur_l2 = semantic_result.level1, semantic_result.level2

    running, bel_l1, bel_l2 = update_belief(prior, cur_score, cur_l1, cur_l2)

    # 上下文修正(仅在"尚未收敛"时应用, 保护连续性; F2 防 site→page 降级)
    if context_result.correction and context_result.source != "none" and running < COMMIT_THRESHOLD:
        ctx = context_result.correction
        tgt_l1, tgt_l2 = ctx.get("level1", bel_l1), ctx.get("level2", bel_l2)
        if (bel_l1 == "build" and bel_l2 == "site" and tgt_l2 == "page"
                and any(kw in current_user_msg for kw in _SITE_KW)):
            logger.info("[SIR] 上下文修正跳过(当前消息明确整站, 防 site→page)")
        else:
            bel_l1, bel_l2 = tgt_l1, tgt_l2
            running = min(running * 0.9, 0.85)
            logger.info("[SIR] 上下文修正 → %s/%s (conf=%.2f)", bel_l1, bel_l2, running)

    industry = semantic_result.industry or rule_result.industry or "other"

    # 写回信念状态(累积 specs / missing / 方向)
    state.belief_l1, state.belief_l2 = bel_l1, bel_l2
    state.running_conf = running
    if semantic_result.specs:
        state.specs.update(semantic_result.specs)
    if semantic_result.missing_specs:
        merged = list(state.missing_specs)
        for k in semantic_result.missing_specs:
            if k not in merged:
                merged.append(k)
        state.missing_specs = merged

    evidence = {
        "semantic": {"level1": semantic_result.level1, "level2": semantic_result.level2,
                     "confidence": semantic_result.confidence, "industry": industry,
                     "is_actionable": semantic_result.is_actionable,
                     "clarification_needed": semantic_result.clarification_needed,
                     "missing_specs": semantic_result.missing_specs, "latency_ms": semantic_result.latency_ms},
        "rule": {"score": rule_score, "signals": rule_result.signals, "keywords": rule_result.keywords},
        "context": {"has_context": context_result.has_context, "source": context_result.source},
        "safety": {"risk_level": safety_result.risk_level, "risk_tags": safety_result.risk_tags},
        "belief": {"prior_conf": belief_before, "running_conf": running,
                   "l1": bel_l1, "l2": bel_l2, "rounds": state.clarify_rounds},
    }
    logger.info("[SIR] [5/6] 融合 sem=%.2f rule=%.2f → cur=%.2f 信念=%.2f(%s/%s)",
                semantic_result.confidence, rule_score, cur_score, running, bel_l1, bel_l2)

    # ── 安全优先(可短路) ──
    if safety_result.risk_level == "critical":
        logger.warning("[SIR] 安全检查→拦截")
        state.running_conf = 0.0
        await save_state(state)
        observe_record(request_id=req_id, conversation_id=conversation_id, user_id=user_id,
                       raw_input=current_user_msg, llm_intent=f"{bel_l1}/{bel_l2}",
                       llm_confidence=running, rules_triggered=["critical_safety"],
                       belief_before=belief_before, belief_after=0.0, decision="block",
                       latency_ms=(time.time() - t0) * 1000, tokens_used=semantic_result.tokens_used,
                       specialist_routed=None, outcome="blocked")
        return PipelineResult(
            intent={"level1": "chat", "level2": "casual", "confidence": 0.0, "industry": industry},
            plan=[{"action": "block", "reason": safety_result.block_reason}],
            risk=safety_result, evidence=evidence, decision="block", selected_skill="explain",
            request_id=req_id,
        )

    # ── 工具映射(供决策选择 skill) ──
    tools = run_tools(bel_l1, bel_l2, running, industry=industry,
                      project_status=project_status, has_requirement_doc=has_requirement_doc,
                      has_conversation_requirement=has_conv_req)
    intended_skill = tools.skills[0].name if tools.skills else "agent_chat"

    # ── [6/6] 决策(COMMIT / CLARIFY / CHAT / RESET) ──
    result = await _decide(
        t0=t0, req_id=req_id, conversation_id=conversation_id, user_id=user_id,
        state=state, belief_before=belief_before,
        bel_l1=bel_l1, bel_l2=bel_l2, running=running,
        intended_skill=intended_skill, industry=industry,
        current_user_msg=current_user_msg, semantic=semantic_result,
        safety=safety_result, evidence=evidence,
    )

    # 多意图拆分门控(仅 COMMIT/route 且 build 类)
    if result.decision == "route" and bel_l1 == "build":
        try:
            split = await maybe_split(messages, model_id, base_industry=industry)
            if split.is_multi and split.sub_tasks:
                result.decision = "split"
                result.sub_tasks = split.sub_tasks
                result.split_reason = split.split_reason
                logger.info("[SIR] 多意图拆分 decision=split tasks=%d", len(split.sub_tasks))
        except Exception as e:
            logger.warning("[SIR] 多意图拆分异常, 降级单意图: %s", e)

    return result


async def _decide(
    *, t0, req_id, conversation_id, user_id, state: IntentState,
    belief_before, bel_l1, bel_l2, running, intended_skill, industry,
    current_user_msg, semantic: SemanticResult, safety: SafetyResult, evidence: dict,
) -> PipelineResult:
    """决策策略 + 状态写回 + 可观测(见文档 §3.4)。"""

    def _emit(decision: str, l1: str, l2: str, conf: float, skill: str,
              questions: list | None = None, rounds: int = 0,
              outcome: str = "pending", reset_first: bool = False) -> PipelineResult:
        if reset_first:
            # RESET: 清空信念
            state.belief_l1, state.belief_l2, state.running_conf = "chat", "casual", 0.0
            state.clarify_rounds = 0
            state.missing_specs = []
        else:
            # 非澄清/非重置 → 清零澄清轮次(已推进到下一步)
            if decision != "clarify":
                state.clarify_rounds = 0
        state.updated_at = time.time()
        # 异步保存(失败静默)
        _task = asyncio.ensure_future(save_state(state))
        observe_record(
            request_id=req_id, conversation_id=conversation_id, user_id=user_id,
            raw_input=current_user_msg, llm_intent=f"{l1}/{l2}",
            llm_confidence=conf, rules_triggered=evidence["rule"]["keywords"][:5] or [],
            belief_before=belief_before, belief_after=conf, decision=decision,
            latency_ms=(time.time() - t0) * 1000, tokens_used=semantic.tokens_used,
            specialist_routed=skill if decision in ("route", "clarify") else None,
            outcome=outcome,
        )
        return PipelineResult(
            intent={"level1": l1, "level2": l2, "confidence": conf, "industry": industry},
            plan=[{"action": decision, "skill": skill, "confidence": conf}],
            risk=safety, tools=ToolResult(skills=[SkillCandidate(name=skill, confidence=conf, reason=f"意图:{l1}/{l2}")]),
            evidence=evidence, decision=decision, selected_skill=skill,
            clarify_questions=questions or [], clarify_rounds=rounds, request_id=req_id,
        )

    # 1) RESET: 用户显式退出
    if _is_reset_signal(current_user_msg):
        logger.info("[SIR] 检测到 RESET 信号 → CHAT")
        return _emit("route", "chat", "casual", 0.3, "agent_chat", reset_first=True,
                     outcome="reset_chat")

    # 2) unsupported → 交 Worker 的 unsupported 分支(路由到 agent_chat + 发 unsupported 事件)
    if bel_l1 == "unsupported":
        logger.info("[SIR] 不支持意图 → chat降级")
        return _emit("route", "unsupported", "casual", running, "agent_chat", outcome="unsupported")

    # 3) CLARIFY: 未收敛 / LLM 要求澄清 / 缺规格
    need_clarify = (CLARIFY_THRESHOLD <= running < COMMIT_THRESHOLD) \
        or semantic.clarification_needed or bool(semantic.missing_specs)
    if need_clarify:
        if state.clarify_rounds < CLARIFY_MAX_ROUNDS:
            questions = _build_clarify_questions(semantic)
            new_rounds = state.clarify_rounds + 1
            state.clarify_rounds = new_rounds
            logger.info("[SIR] CLARIFY 轮次=%d/%d questions=%s", new_rounds, CLARIFY_MAX_ROUNDS, questions)
            return _emit("clarify", bel_l1, bel_l2, running, intended_skill,
                         questions=questions, rounds=new_rounds, outcome="clarified")
        else:
            # 2 轮追问耗尽: 偏 build → 进需求分析(commit requirement); 偏 chat → 闲聊
            skill = "agent_requirement" if bel_l1 == "build" else "agent_chat"
            logger.info("[SIR] 澄清轮次耗尽 → 提交 skill=%s", skill)
            return _emit("route", bel_l1, bel_l2, running, skill, outcome="clarify_exhausted")

    # 4) COMMIT: 已收敛
    if running >= COMMIT_THRESHOLD:
        logger.info("[SIR] COMMIT → skill=%s", intended_skill)
        return _emit("route", bel_l1, bel_l2, running, intended_skill, outcome="committed")

    # 5) 低置信 → 闲聊
    logger.info("[SIR] 低置信 → CHAT")
    return _emit("route", "chat", "casual", max(running, 0.3), "agent_chat", outcome="chatted")
