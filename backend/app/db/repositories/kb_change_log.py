from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KbChangeLog

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.kb_change_log")


class KbChangeLogRepo(BaseRepo[KbChangeLog]):
    model = KbChangeLog

    async def by_collection(
        self, session: AsyncSession, collection: str, *, limit: int = 100
    ) -> list[KbChangeLog]:
        if not collection.strip():
            raise ValueError("collection 不得为空")
        return await self.list(session, collection=collection, limit=limit)
