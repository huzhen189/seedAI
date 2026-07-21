"""意图管道入口 + 汇总器。

并行调用 5 模块 → 优先级决策 → 证据融合 → 输出最终 PipelineResult。

用法:
  result = await classify_v2(messages, model_id, conversation_id=..., ...)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .context import ContextResult, run_context
from .rules import RuleResult, run_rules
from .safety import SafetyResult, run_safety
from .semantic import SemanticResult, run_semantic
from .tools import SkillCandidate, ToolResult, run_tools

logger = logging.getLogger("ai_service.intent.pipeline")


@dataclass
class PipelineResult:
    intent: dict = field(default_factory=lambda: {"level1": "learn", "level2": "casual", "confidence": 0.3, "industry": "other"})
    plan: list[dict] = field(default_factory=list)
    risk: SafetyResult = field(default_factory=SafetyResult)
    tools: ToolResult = field(default_factory=ToolResult)
    evidence: dict = field(default_factory=dict)
    decision: str = "route"  # "route"|"block"|"confirm"|"options"|"fallback"
    selected_skill: str = "explain"


async def classify_v2(
    messages: list[dict],
    model_id: str = "deepseek",
    *,
    conversation_id: int | None = None,
    context_hint: str = "",
    project_status: str = "draft",
    checkpoint_info: dict | None = None,
) -> PipelineResult:
    """v2 意图管道: 5 模块并行 + 汇总器决策。

    时序优化: LLM 语义模块先发射(asyncio.create_task),
    4 个同步规则模块在 LLM 飞行期间完成(0延迟)。
    """
    logger.info("[管道] 开始 5模块并行 %d条消息 model=%s", len(messages), model_id)

    # ── 发射语义任务(LLM, 异步) ──
    semantic_task = asyncio.create_task(
        run_semantic(messages, model_id, context_hint=context_hint, checkpoint_info=checkpoint_info)
    )

    # ── 4 个同步规则模块(0ms) ──
    rule_result: RuleResult = RuleResult()
    context_result: ContextResult = ContextResult()
    safety_result: SafetyResult = SafetyResult()

    try:
        rule_result = run_rules(messages)
        context_result = run_context(messages, conversation_id=conversation_id, frontend_hint=context_hint)
        safety_result = run_safety(messages)
    except Exception as e:
        logger.warning("[管道] 规则模块异常: %s", e)

    # ── 等语义模块完成 ──
    semantic_result: SemanticResult
    try:
        semantic_result = await asyncio.wait_for(semantic_task, timeout=35.0)
    except asyncio.TimeoutError:
        logger.error("[管道] 语义模块超时35s → 降级规则结果")
        semantic_result = SemanticResult(
            level1="learn", level2="casual", confidence=0.3,
            industry=rule_result.industry or "other",
            raw_output="timeout", latency_ms=35000,
        )
    except Exception as e:
        logger.error("[管道] 语义模块异常: %s → 降级规则结果", e)
        semantic_result = SemanticResult(
            level1="learn", level2="casual", confidence=0.3,
            industry=rule_result.industry or "other",
        )

    # ── 汇总器 ──
    return _aggregate(rule_result, semantic_result, context_result, safety_result, project_status)


def _aggregate(
    rule: RuleResult,
    semantic: SemanticResult,
    context: ContextResult,
    safety: SafetyResult,
    project_status: str,
) -> PipelineResult:
    """汇总器: 优先级决策 + 证据融合 + 置信度排序。"""
    evidence = {
        "rule": {"pattern": rule.pattern, "keywords": rule.keywords, "confidence": rule.confidence},
        "semantic": {"level1": semantic.level1, "level2": semantic.level2, "confidence": semantic.confidence, "industry": semantic.industry, "latency_ms": semantic.latency_ms},
        "context": {"has_context": context.has_context, "source": context.source, "hint": context.hint[:80]},
        "safety": {"risk_level": safety.risk_level, "risk_tags": safety.risk_tags},
    }

    # ── Step 1: 安全优先(可短路) ──
    if safety.risk_level == "critical":
        logger.warning("[汇总] 安全检查→拦截 risk=%s tags=%s", safety.risk_level, safety.risk_tags)
        return PipelineResult(
            intent={"level1": "learn", "level2": "casual", "confidence": 0.0, "industry": semantic.industry},
            plan=[{"action": "block", "reason": safety.block_reason}],
            risk=safety,
            evidence=evidence,
            decision="block",
            selected_skill="explain",
        )

    if safety.risk_level == "high":
        logger.info("[汇总] 安全检查→需要二次确认 risk=%s reason=%s", safety.risk_level, safety.block_reason)
        return PipelineResult(
            intent={"level1": "learn", "level2": "casual", "confidence": 0.5, "industry": semantic.industry},
            plan=[{"action": "confirm", "reason": safety.block_reason}],
            risk=safety,
            evidence=evidence,
            decision="confirm",
            selected_skill="explain",
        )

    # ── Step 2: 意图消歧义(融合多路证据) ──
    # 权重: 语义 70% + 规则 20% + 上下文修正 10%
    final_l1 = semantic.level1
    final_l2 = semantic.level2
    confidence = semantic.confidence

    # 上下文修正: 如果上下文明确指示不同意图, 可以翻转
    if context.correction and context.source != "none":
        ctx_correction = context.correction
        logger.info("[汇总] 上下文修正 %s/%s → %s/%s (原因: %s)",
                   final_l1, final_l2,
                   ctx_correction.get("level1"), ctx_correction.get("level2"),
                   ctx_correction.get("reason"))
        final_l1 = ctx_correction.get("level1", final_l1)
        final_l2 = ctx_correction.get("level2", final_l2)
        confidence = min(confidence * 0.85, 0.85)  # 上下文修正降低置信度

    # 规则与语义冲突时降低置信度
    if rule.pattern and rule.confidence > 0.5:
        if not _intent_compatible(rule.pattern, final_l1):
            logger.info("[汇总] 规则(%s)与语义(%s)冲突 → 降低置信度", rule.pattern, final_l1)
            confidence *= 0.7

    industry = semantic.industry or rule.industry or "other"

    # ── Step 3: 工具选择 ──
    tools = run_tools(final_l1, final_l2, confidence, industry=industry, project_status=project_status)

    if not tools.skills:
        logger.info("[汇总] 无可用工具 → 降级 explain")
        return PipelineResult(
            intent={"level1": "learn", "level2": "casual", "confidence": confidence, "industry": industry},
            plan=[{"action": "fallback", "skill": "explain"}],
            risk=safety,
            tools=tools,
            evidence=evidence,
            decision="fallback",
            selected_skill="explain",
        )

    # 低置信度 → 出多选项
    if confidence < 0.5 or (tools.skills and tools.skills[0].confidence < 0.5):
        logger.info("[汇总] 低置信度(%.0f%%) → 出多选项", confidence * 100)
        return PipelineResult(
            intent={"level1": final_l1, "level2": final_l2, "confidence": confidence, "industry": industry},
            plan=[{"action": "options", "skills": [s.name for s in tools.skills]}],
            risk=safety,
            tools=tools,
            evidence=evidence,
            decision="options",
            selected_skill=tools.fallback,
        )

    # 正常路由
    selected = tools.skills[0].name
    logger.info("[汇总] 决策完成 intent=%s/%s conf=%.0f%% skill=%s",
               final_l1, final_l2, confidence * 100, selected)
    return PipelineResult(
        intent={"level1": final_l1, "level2": final_l2, "confidence": confidence, "industry": industry},
        plan=[
            {"action": "classify", "result": f"{final_l1}/{final_l2}", "confidence": confidence},
            {"action": "route", "skill": selected, "confidence": tools.skills[0].confidence},
        ],
        risk=safety,
        tools=tools,
        evidence=evidence,
        decision="route",
        selected_skill=selected,
    )


def _intent_compatible(rule_pattern: str, semantic_l1: str) -> bool:
    """判断规则意图和语义意图是否兼容。"""
    compatible = {
        "build": {"build", "learn"},
        "code": {"code", "learn"},
        "learn": {"learn", "build", "code", "doc", "translate"},
        "doc": {"doc", "learn"},
        "translate": {"translate", "learn"},
    }
    return semantic_l1 in compatible.get(rule_pattern, {"learn"})
