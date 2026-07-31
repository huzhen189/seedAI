from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VectorCollection

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.vector_collections")


class VectorCollectionsRepo(BaseRepo[VectorCollection]):
    model = VectorCollection

    async def by_name(
        self, session: AsyncSession, collection: str
    ) -> VectorCollection | None:
        if not collection.strip():
            raise ValueError("collection 不得为空")
        return await self.get_by(session, collection=collection)

    async def by_owner(
        self, session: AsyncSession, scope: str, owner_id: int
    ) -> list[VectorCollection]:
        if owner_id < 0:
            raise ValueError("owner_id 不得为负数")
        return await self.list(session, scope=scope, owner_id=owner_id, limit=100)
