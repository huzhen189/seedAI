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
    """S7 状态固化(§5.6) + 记忆写入主链路派发。

    把 DST 后的 SIR 置为最终态(``sir_final``)；若 S5 校验未通过(需审批/被阻止)则不提交
    执行态(NO_OP)。固化后派发记忆写入（异步、fail-soft、不在 token 流内）：
    把本轮对话压缩提炼为 user_facts/project_facts/user_prefs/project_exps/session_summary，
    分库写入 MySQL（主）与向量库（辅，仅存标题）。见 docs/plan-memory-v2-landing.md §2。
    """

    stage_id = StageId.S7

    async def run(self, context: TurnContext):
        context.sir_final = context.sir_after_dst
        if context.validation is not None and context.validation.status != "pass":
            logger.debug("[S7] 执行未提交,跳过固化 turn=%s status=%s", context.turn_id, context.validation.status)
            context.memory_decision = MemoryDecision(status="skipped", reason_codes=["execution_not_committed"])
            return self.result(StageStatus.NO_OP, "execution_not_committed")

        # 派发记忆写入（响应已交付之后，异步、fail-soft）。
        self._dispatch_memory_write(context)
        logger.info("[S7] 规范状态就绪 turn=%s (记忆写入已派发)", context.turn_id)
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
            )
        )


__all__ = ["S7PersistStateStage"]
