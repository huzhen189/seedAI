from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import ErrorEnvelope, StageId, StageResult, StageStatus
from app.core.turn_context import TurnContext


class BaseStage:
    stage_id: StageId

    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session

    def result(
        self,
        status: StageStatus,
        reason_code: str,
        *,
        entered_at: datetime | None = None,
        error: ErrorEnvelope | None = None,
        output_refs: list[str] | None = None,
    ) -> StageResult:
        start = entered_at or datetime.now(UTC)
        end = datetime.now(UTC)
        return StageResult(
            stage=self.stage_id,
            status=status,
            reason_code=reason_code,
            entered_at=start,
            left_at=end,
            duration_ms=max(0, int((end - start).total_seconds() * 1000)),
            error=error,
            output_refs=output_refs or [],
        )

    async def run(self, context: TurnContext) -> StageResult:
        raise NotImplementedError
