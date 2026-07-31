from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MetricsEvent

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.metrics_events")


class MetricsEventsRepo(BaseRepo[MetricsEvent]):
    model = MetricsEvent

    async def by_user_and_type(
        self,
        session: AsyncSession,
        user_id: int,
        event_type: str,
        *,
        limit: int = 100,
    ) -> list[MetricsEvent]:
        if user_id <= 0 or not event_type.strip():
            raise ValueError("user_id 必须为正整数且 event_type 不得为空")
        return await self.list(
            session, user_id=user_id, event_type=event_type, limit=limit
        )

    async def get_by_idempotency_key(
        self, session: AsyncSession, idempotency_key: str
    ) -> MetricsEvent | None:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key 不得为空")
        return await self.get_by(session, idempotency_key=idempotency_key)

    async def insert_idempotent(
        self, session: AsyncSession, *, idempotency_key: str, **values: object
    ) -> MetricsEvent:
        existing = await self.get_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            return existing
        return await self.insert(session, idempotency_key=idempotency_key, **values)
