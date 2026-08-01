from __future__ import annotations

from app.core.contracts import SirState, StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S3DstStage(BaseStage):
    stage_id = StageId.S3

    async def run(self, context: TurnContext):
        if context.understanding is None:
            context.sir_after_dst = context.sir_base
            return self.result(StageStatus.NO_OP, "understanding_missing")
        delta = context.understanding.sir_delta
        merged_slots = {**context.sir_base.slots, **delta.slots}
        context.sir_after_dst = SirState(
            slots=merged_slots,
            constraints=[*context.sir_base.constraints, *delta.constraints],
            pending=[*context.sir_base.pending, *delta.pending],
            memory_hints=[*context.sir_base.memory_hints, *delta.memory_hints],
        )
        context.sir_diff = {"changed_slots": sorted(delta.slots)}
        return self.result(StageStatus.NO_OP if not delta.slots and not delta.constraints else StageStatus.COMPLETED, "sir_delta_merged")
