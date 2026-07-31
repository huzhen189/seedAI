from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RecycleBin

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.recycle_bin")


class RecycleBinRepo(BaseRepo[RecycleBin]):
    model = RecycleBin

    async def pending_for_user(
        self, session: AsyncSession, user_id: int, *, limit: int = 100
    ) -> list[RecycleBin]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(
            session, user_id=user_id, purge_state="pending", limit=limit
        )

    async def by_project(self, session: AsyncSession, project_id: int) -> RecycleBin | None:
        if project_id <= 0:
            raise ValueError("project_id 必须为正整数")
        return await self.get_by(
            session, resource_type="project", resource_id=project_id
        )
