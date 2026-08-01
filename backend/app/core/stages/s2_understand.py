from __future__ import annotations

from app.core.contracts import IntentCandidate, StageId, StageStatus
from app.core.turn_context import TurnContext
from app.router.intent import understand
from .base import BaseStage


class S2UnderstandStage(BaseStage):
    """S2 意图理解(§5.6,确定性优先)。

    调用 ``router.intent.understand`` 对清洗后的用户消息做结构化意图判定(域/言语行为/
    风险),构造成 ``UnderstandingResult`` 并派生首条 intent candidate 写入 ``context.understanding``。
    """

    stage_id = StageId.S2

    async def run(self, context: TurnContext):
        logger.debug("[S2] 意图理解 msg=%.60s", context.clean_message)
        result = understand(context.clean_message)
        frame = result.utterance_frame
        context.understanding = result.model_copy(
            update={"intent_candidates": [IntentCandidate(intent_id=f"{frame.domain_hint.value}_{frame.speech_act.value}", confidence=frame.confidence)]}
        )
        logger.info("[S2] 理解结果 domain=%s speech=%s risk=%s", frame.domain_hint.value, frame.speech_act.value, frame.risk.value)
        return self.result(StageStatus.COMPLETED, "deterministic_understanding")
