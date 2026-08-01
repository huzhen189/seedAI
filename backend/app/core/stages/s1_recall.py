from __future__ import annotations

from app.core.contracts import RecallResult, StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S1RecallStage(BaseStage):
    """S1 召回(§5.6)。

    当前无记忆后端可用,按规范不进入任何召回,标记 SKIPPED 并带 ``recall_gate_no_signal``
    原因,链路继续向 S2。投产时此处接 Chroma/记忆检索,回填 ``context.recall``。
    """

    stage_id = StageId.S1

    async def run(self, context: TurnContext):
        logger.debug("[S1] 召回跳过(无后端) turn=%s", context.turn_id)
        context.recall = RecallResult(status="skipped")
        return self.result(StageStatus.SKIPPED, "recall_gate_no_signal")
