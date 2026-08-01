"""M2 空跑 Stage 的共享实现。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.contracts import StageId, StageResult, StageStatus
from app.core.turn_context import TurnContext


class SkeletonStage:
    """无副作用的 Stage 占位实现。

    仅用于验证十阶段顺序、审计和 skip/no-op 语义；M3 起各子类逐步实现真实职责。
    """

    stage_id: StageId

    async def run(self, context: TurnContext) -> StageResult:
        del context
        entered_at = datetime.now(UTC)
        status = (
            StageStatus.COMPLETED
            if self.stage_id in {StageId.S0, StageId.S8, StageId.S9}
            else StageStatus.NO_OP
        )
        return StageResult(
            stage=self.stage_id,
            status=status,
            reason_code="m2_skeleton_no_side_effect",
            entered_at=entered_at,
            left_at=datetime.now(UTC),
        )
