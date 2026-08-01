from __future__ import annotations

from app.core.contracts import StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S0GatewayStage(BaseStage):
    stage_id = StageId.S0

    async def run(self, context: TurnContext):
        # Transport 已在同一 W0 事务完成鉴权、归属、脱敏、幂等与预算预留。
        return self.result(StageStatus.COMPLETED, "turn_accepted", output_refs=[context.turn_id])
