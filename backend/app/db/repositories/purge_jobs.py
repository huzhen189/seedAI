from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PurgeJob

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.purge_jobs")


class PurgeJobsRepo(BaseRepo[PurgeJob]):
    model = PurgeJob

    async def queued(self, session: AsyncSession, *, limit: int = 100) -> list[PurgeJob]:
        return await self.list(session, status="queued", limit=limit)

    async def by_resource(self, session: AsyncSession, resource_id: int) -> list[PurgeJob]:
        if resource_id <= 0:
            raise ValueError("resource_id 必须为正整数")
        return await self.list(
            session, resource_type="project", resource_id=resource_id, limit=100
        )
