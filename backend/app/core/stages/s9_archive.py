from __future__ import annotations

from app.core.contracts import ArchiveResult, StageId, StageStatus
from app.core.turn_context import TurnContext
from app.services.finalize import finalize_service
from .base import BaseStage


class S9ArchiveStage(BaseStage):
    stage_id = StageId.S9

    async def run(self, context: TurnContext):
        if self.session is None:
            raise RuntimeError("S9 requires a database session")
        status = await finalize_service.finalize(self.session, context)
        context.archive_result = ArchiveResult(status="finalized" if status in {"completed", "blocked"} else "attempt_archived")
        return self.result(StageStatus.COMPLETED, f"turn_{status}")
