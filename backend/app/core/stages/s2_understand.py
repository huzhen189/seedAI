from __future__ import annotations
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

from app.core.contracts import Domain, IntentCandidate, StageId, StageStatus
from app.core.continuation import resolve_continuation
from app.core.turn_context import TurnContext
from app.router.intent import (
    understand, escalate_if_needed, inherit_retro_domain, recompute_slots,
    record_intent_example, fill_await_slots,
)
from app.analytics import record_ai_intent, record_intent_result
from .base import BaseStage


class S2UnderstandStage(BaseStage):
    """S2 意图理解(§5.6,确定性优先 + 多意图 + 分句 + LLM 升级)。

    流程：
      1. understand() 做确定性多意图理解（方案①分句+多信号采集，CHAT 兜底）；
      2. escalate_if_needed() 在规则无法稳妥分解时单次 LLM 升级（方案③，带自愈降级）；
      3. 把 resolved_intents 写入 context.understanding，并为每个候选记录 analytics。
    不再假设单意图：下游 S4/S5/S6 均消费 intent_bundle / plan 的列表。
    """

    stage_id = StageId.S2

    async def run(self, context: TurnContext):
        logger.debug("[S2] 意图理解 msg=%.60s", context.clean_message)
        t0 = time.time()
        result = understand(context.clean_message)
        result = await escalate_if_needed(context.clean_message, result)
        # 回溯控制：把 CHAT 兜底提升为上一轮的域（S1 已按事实产物回填 prior_domain）。
        # 「改成浅色风格」这类指令不含任何域触发词，不继承就会被降级成闲聊追问。
        if context.prior_turn_id is not None and context.prior_domain is not None:
            result = inherit_retro_domain(result, context.prior_domain)
            # 域继承后必须用提升后的 resolved_intents 重新抽取 site 槽位：
            # understand() 早于此步运行，对"无域触发词"的回溯轮会把 theme/sections 槽位
            # 整批丢弃，导致 S3 合并为零变更（不落快照、不改 spec）。见 recompute_slots。
            result = recompute_slots(context.clean_message, result)

        # 跨轮承接解析（T2，确定性无 LLM）：把前情承接关系解析成一等数据结构，
        # 写入 context.continuation。fail-soft：异常时置 None，不影响后续。
        # 必须在 recompute_slots 之后（域继承会重算 sir_delta，提前写入会被覆盖）。
        try:
            context.continuation = resolve_continuation(context.clean_message, context.context_gist)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[S2] 承接解析异常(忽略): %s", exc, exc_info=True)
            context.continuation = None
        # 注：承接**不再**在此处硬塞 ``site.brief``。
        # 旧实现每轮都往 delta 里追加 ``（承接：…）``，多轮后 brief 滚成一长串重复
        # 摘要；更根本的问题是"承接"被当成了槽位补丁，而它其实是**状态机的输入信号**。
        # v2 起交由 S3 调用 ``core.transition.plan_round`` 统一处置：
        # 首次承接播种 ``task.goal`` + 记 ``continuation_source``（幂等，只播一次），
        # 并顺带把 ``site.brief`` 视作已给定、不再追问。

        # 续答抽槽：上一轮 S5 挂起收集（SIR pending 非空）时，本轮把待收集槽位当作答案，
        # 用一次 LLM 解析用户自由文本回填 sir_delta——只补确定性抽取仍缺失的槽位。
        # 这样「手绘的卡通像素风格，平台托管」类自由描述能真正补齐必填槽，避免反复追问死循环。
        if context.sir_base.pending:
            pending_defs = [
                p for p in context.sir_base.pending
                if isinstance(p, dict) and str(p.get("key", "")).startswith("site.")
            ]
            filled = set(result.sir_delta.slots.keys()) | set(context.sir_base.slots.keys())
            remaining = [p for p in pending_defs if p.get("key") not in filled]
            if remaining and any(r.domain == Domain.SITE for r in result.resolved_intents):
                try:
                    extra = await fill_await_slots(
                        context.clean_message, remaining, context.sir_base.slots
                    )
                except Exception as exc:  # noqa: BLE001 — 抽槽失败不得中断主链路
                    extra = {}
                    logger.warning("[S2] 续答抽槽异常(忽略): %s", exc)
                if extra:
                    merged_slots = dict(result.sir_delta.slots)
                    for k, v in extra.items():
                        if k not in merged_slots:
                            merged_slots[k] = v
                    result = result.model_copy(update={
                        "sir_delta": result.sir_delta.model_copy(update={"slots": merged_slots})
                    })
                    logger.info("[S2] 续答 LLM 抽槽补齐 slots=%s", extra)

        # context.understanding 持有完整多意图集合（resolved_intents 是真相）。
        context.understanding = result
        # 社交前缀收集：S8 会前置到最终回复一次（避免复合句寒暄被过度裁剪丢弃）。
        context.social_prefix = result.social_prefix

        duration_ms = (time.time() - t0) * 1000
        source = "llm" if result.escalated else "rule"
        # 统计：每个解析出的意图都记录一条（decision=域_言语行为，success=是否可执行）。
        for item in result.resolved_intents:
            await record_ai_intent(
                decision=item.intent_id,
                source=source,
                success=bool(item.executable),
                confidence=float(item.confidence or 0.0),
                duration_ms=duration_ms if item is result.resolved_intents[0] else 0.0,
            )
            # 接通业务端 an:intent:hit/total 统计(此前为死键, 管理后台意图命中率面板一直为空)。
            # 注意: 此处的 matched 表示「意图是否被识别命中」, 而非「是否可执行(映射到工具)」——
            # 聊天意图(chat)虽不可执行, 但已被正确归类到 chat 域, 应记为命中; 可执行率由
            # 上面的 record_ai_intent(success=executable) 单独统计。否则纯聊天场景命中率恒为 0,
            # 雷达「意图识别」维度永远 0, 表现为「没数据」。
            await record_intent_result(
                level1=item.domain.value,
                level2=item.intent_id,
                matched=True,
            )
        primary = next((r for r in result.resolved_intents if r.executable), result.resolved_intents[0] if result.resolved_intents else None)
        # 逐意图一行：意图 / 方法 / 置信度 / 可执行——置信度此前从未在日志明确呈现，
        # 用户复盘时无法判断"为什么低置信"。executable=False 的 CHAT 兜底也一并列出。
        intent_summary = [
            f"{r.intent_id}({r.method.value},conf={float(r.confidence or 0):.2f},exec={r.executable})"
            for r in result.resolved_intents
        ]
        logger.info(
            "[S2] 理解结果 耗时=%.0fms | 意图数=%d | escalated=%s | primary=%s | needs_clarify=%s | %s",
            duration_ms, len(result.resolved_intents), result.escalated,
            primary.intent_id if primary else None, result.needs_clarification,
            " ".join(intent_summary),
        )
        # 方案③ LLM 升级：把升级原文与本次结果整合到同一条日志，避免跨行难对应。
        # 规则路径(escalated=False)无升级响应，不打印本段。
        if result.escalated and result.escalation_llm_response:
            logger.info(
                "[S2] LLM 升级响应(整合) 耗时=%.0fms needs_clarify=%s:\n%s",
                duration_ms, result.needs_clarification, result.escalation_llm_response,
            )
        # 随生产补充知识库：把已确认的意图示例后台沉淀进 intents 集合（fail-soft，不阻塞）。
        if primary is not None and primary.executable:
            asyncio.create_task(record_intent_example(context.clean_message, primary.intent_id))
        return self.result(StageStatus.COMPLETED, "deterministic_understanding")
