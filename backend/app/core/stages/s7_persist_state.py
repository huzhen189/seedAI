from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

from app.config import settings
from app.core.contracts import MemoryDecision, StageId, StageStatus
from app.core.memory_write import persist_and_extract
from app.core.turn_context import TurnContext
from .base import BaseStage


class S7PersistStateStage(BaseStage):
    """S7 状态固化(§5.6) + 记忆写入主链路派发（统一对所有 turn 跑一次）。

    把 DST 后的 SIR 置为最终态(``sir_final``)，并**对每一个 turn 统一派发一次**
    「总结 + 聊天级 QC + 记忆点提取落库」（异步、fail-soft、不在 token 流内）：
    不论意图类型（闲聊/建站/检索…）、不论意图数量，S7 都只做一次；QC 与记忆提取
    共享一次 LLM 调用(``llm_extract``)，绝不反噬主链路。

    与旧版差异：去掉了「S5 校验未通过(pass 以外)就跳过固化/记忆」的限制——
    按需求「不管意图都走一次」。仅在「开关关闭」或「用户/助手文本皆空」时跳过。
    见 docs/plan-memory-v2-landing.md §2。
    """

    stage_id = StageId.S7

    async def run(self, context: TurnContext):
        context.sir_final = context.sir_after_dst

        # 统一派发「总结 + QC + 记忆」任务（所有 turn 一次，fail-soft、后台异步）。
        self._dispatch_memory_write(context)
        logger.info("[S7] 规范状态就绪 turn=%s (总结+QC+记忆 已派发)", context.turn_id)
        context.memory_decision = MemoryDecision(status="persisted", reason_codes=["async_extraction_dispatched"])
        return self.result(StageStatus.COMPLETED, "canonical_state_ready")

    def _dispatch_memory_write(self, context: TurnContext) -> None:
        if not getattr(settings, "memory_extraction_enabled", True):
            logger.debug("[S7] 记忆提取开关关闭，跳过 turn=%s", context.turn_id)
            return
        user_text = (context.clean_message or "").strip()
        assistant_text = context.reply_final or "".join(
            f.text for f in context.response_fragments if f.status == "success"
        )
        if not user_text and not assistant_text:
            return
        user_id = int(context.user.user_id)
        project_id = int(context.session.project_id) if context.session.project_id else None
        conversation_id = int(context.session.conversation_id) if context.session.conversation_id else None
        asyncio.create_task(
            persist_and_extract(
                user_id=user_id,
                project_id=project_id,
                conversation_id=conversation_id,
                user_text=user_text,
                assistant_text=assistant_text,
                trace_id=context.trace_id,  # turn_id == trace_id，供 QC 落库关联
            )
        )


__all__ = ["S7PersistStateStage"]
