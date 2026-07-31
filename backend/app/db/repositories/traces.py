from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Trace

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.traces")


class TracesRepo(BaseRepo[Trace]):
    model = Trace

    async def get_by_trace_id(self, session: AsyncSession, trace_id: str) -> Trace | None:
        if not trace_id.strip():
            raise ValueError("trace_id 不得为空")
        return await self.get_by(session, trace_id=trace_id)

    async def finish(
        self,
        session: AsyncSession,
        trace: Trace,
        status: str,
        total_tokens: int = 0,
    ) -> Trace:
        if status not in {"done", "failed", "aborted", "completed"}:
            raise ValueError("trace 终态必须是 done/failed/aborted/completed")
        if total_tokens < 0:
            raise ValueError("total_tokens 不得为负数")
        return await self.update(
            session,
            trace,
            status=status,
            total_tokens=total_tokens,
            finished_at=datetime.now(UTC),
        )


trace_repo = TracesRepo()
