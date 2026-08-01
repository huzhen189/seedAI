from __future__ import annotations

from app.core.contracts import MemoryDecision, StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S7PersistStateStage(BaseStage):
    """S7 状态固化(§5.6)。

    把 DST 后的 SIR 置为最终态(``sir_final``),并标记记忆决策(当前记忆后端未就绪→skipped)。
    若 S5 校验未通过(需审批/被阻止),则不提交执行态(NO_OP),保留供审批链路收口。
    """

    stage_id = StageId.S7

    async def run(self, context: TurnContext):
        context.sir_final = context.sir_after_dst
        context.memory_decision = MemoryDecision(status="skipped", reason_codes=["memory_gate_not_required"])
        if context.validation is not None and context.validation.status != "pass":
            logger.debug("[S7] 执行未提交,跳过固化 turn=%s status=%s", context.turn_id, context.validation.status)
            return self.result(StageStatus.NO_OP, "execution_not_committed")
        logger.debug("[S7] 规范状态就绪 turn=%s", context.turn_id)
        return self.result(StageStatus.COMPLETED, "canonical_state_ready")
