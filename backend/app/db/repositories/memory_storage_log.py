from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MemoryStorageLog

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.memory_storage_log")


class MemoryStorageLogRepo(BaseRepo[MemoryStorageLog]):
    model = MemoryStorageLog

    async def by_user_and_decision(
        self, session: AsyncSession, user_id: int, decision: str, *, limit: int = 100
    ) -> list[MemoryStorageLog]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(
            session, user_id=user_id, decision=decision, limit=limit
        )
