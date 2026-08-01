from __future__ import annotations

from app.core.contracts import ArchiveResult, StageId, StageStatus
from app.core.turn_context import TurnContext
from app.services.finalize import finalize_service
from .base import BaseStage


class S9ArchiveStage(BaseStage):
    """S9 归档/终态(§5.6,终态唯一归属)。

    调 ``finalize_service.finalize`` 把本轮结果(assistant 消息、Turn 终态、用量等)落库收口。
    注意:**本阶段是终态的唯一写入点**;SSE 的 ``done`` 事件只读其结论,绝不重复调用 finalize,
    否则同事务二次 add 会撞消息唯一约束导致整个 Turn 回滚。
    """

    stage_id = StageId.S9

    async def run(self, context: TurnContext):
        if self.session is None:
            raise RuntimeError("S9 requires a database session")
        logger.debug("[S9] 终态收口 turn=%s", context.turn_id)
        status = await finalize_service.finalize(self.session, context)
        context.archive_result = ArchiveResult(status="finalized" if status in {"completed", "blocked"} else "attempt_archived")
        logger.info("[S9] 终态收口完成 turn=%s -> %s", context.turn_id, status)
        return self.result(StageStatus.COMPLETED, f"turn_{status}")
