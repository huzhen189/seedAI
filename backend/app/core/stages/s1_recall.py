from __future__ import annotations

from app.core.contracts import RecallResult, StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S1RecallStage(BaseStage):
    stage_id = StageId.S1

    async def run(self, context: TurnContext):
        context.recall = RecallResult(status="skipped")
        return self.result(StageStatus.SKIPPED, "recall_gate_no_signal")
