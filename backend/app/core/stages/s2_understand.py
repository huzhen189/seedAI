from __future__ import annotations

from app.core.contracts import IntentCandidate, StageId, StageStatus
from app.core.turn_context import TurnContext
from app.router.intent import understand
from .base import BaseStage


class S2UnderstandStage(BaseStage):
    stage_id = StageId.S2

    async def run(self, context: TurnContext):
        result = understand(context.clean_message)
        frame = result.utterance_frame
        context.understanding = result.model_copy(
            update={"intent_candidates": [IntentCandidate(intent_id=f"{frame.domain_hint.value}_{frame.speech_act.value}", confidence=frame.confidence)]}
        )
        return self.result(StageStatus.COMPLETED, "deterministic_understanding")
