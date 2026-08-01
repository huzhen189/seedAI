from __future__ import annotations

from app.core.contracts import MemoryDecision, StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S7PersistStateStage(BaseStage):
    stage_id = StageId.S7

    async def run(self, context: TurnContext):
        context.sir_final = context.sir_after_dst
        context.memory_decision = MemoryDecision(status="skipped", reason_codes=["memory_gate_not_required"])
        if context.validation is not None and context.validation.status != "pass":
            return self.result(StageStatus.NO_OP, "execution_not_committed")
        return self.result(StageStatus.COMPLETED, "canonical_state_ready")
