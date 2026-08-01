from __future__ import annotations

from app.core.contracts import StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S0GatewayStage(BaseStage):
    """S0 网关(§5.6)。

    仅做 Turn 受理确认。鉴权、归属、脱敏、幂等、预算预留等重活已由 Transport 层在
    受理事务(W0)中完成;本阶段直接返回 COMPLETED,把 turn_id 纳入 output_refs 供后续追踪。
    """

    stage_id = StageId.S0

    async def run(self, context: TurnContext):
        logger.debug("[S0] turn 受理确认 turn=%s", context.turn_id)
        # Transport 已在同一 W0 事务完成鉴权、归属、脱敏、幂等与预算预留。
        return self.result(StageStatus.COMPLETED, "turn_accepted", output_refs=[context.turn_id])
