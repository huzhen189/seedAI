from __future__ import annotations

from app.core.contracts import StageId, StageStatus
from app.core.turn_context import TurnContext
from app.router.intent import classify
from .base import BaseStage


class S4ClassifyStage(BaseStage):
    stage_id = StageId.S4

    async def run(self, context: TurnContext):
        if context.understanding is None:
            return self.result(StageStatus.FAILED, "understanding_required")
        context.intent_bundle, context.plan = classify(context.clean_message, context.understanding)
        return self.result(StageStatus.COMPLETED, "bounded_plan_created")
