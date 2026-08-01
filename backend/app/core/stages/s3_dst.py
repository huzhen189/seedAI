from __future__ import annotations

from app.core.contracts import SirState, StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S3DstStage(BaseStage):
    """S3 槽位/约束合并(§5.6, DST)。

    把 S2 理解的意图增量(``understanding.sir_delta``)合并进基础 SIR 状态,得到
    ``sir_after_dst``。无理解结果时原样透传基态(NO_OP);有改动则标记 COMPLETED。
    """

    stage_id = StageId.S3

    async def run(self, context: TurnContext):
        if context.understanding is None:
            logger.debug("[S3] 无理解结果,透传基态 turn=%s", context.turn_id)
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
        status = StageStatus.NO_OP if not delta.slots and not delta.constraints else StageStatus.COMPLETED
        logger.debug("[S3] 合并 delta slots=%s -> %s", list(delta.slots), status.value)
        return self.result(status, "sir_delta_merged")
