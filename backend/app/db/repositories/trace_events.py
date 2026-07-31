from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TraceEvent

from ._base import BaseRepo, RepositoryError


logger = logging.getLogger("app.db.repositories.trace_events")


class TraceEventsRepo(BaseRepo[TraceEvent]):
    model = TraceEvent

    async def list_by_trace(
        self, session: AsyncSession, trace_id: str, *, limit: int = 10000
    ) -> list[TraceEvent]:
        if not trace_id.strip():
            raise ValueError("trace_id 不得为空")
        if not 1 <= limit <= 10000:
            raise ValueError("limit 必须在 1..10000 之间")
        try:
            result = await session.execute(
                select(TraceEvent)
                .where(TraceEvent.trace_id == trace_id)
                .order_by(TraceEvent.seq.asc())
                .limit(limit)
            )
            return list(result.scalars().all())
        except SQLAlchemyError as exc:
            logger.exception("读取 trace 事件失败 trace_id=%s", trace_id)
            raise RepositoryError("list_by_trace", "TraceEvent", str(exc)) from exc


trace_event_repo = TraceEventsRepo()
