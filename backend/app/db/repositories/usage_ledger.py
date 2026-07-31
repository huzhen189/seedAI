from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageLedger

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.usage_ledger")


class UsageLedgerRepo(BaseRepo[UsageLedger]):
    model = UsageLedger

    async def by_user(
        self, session: AsyncSession, user_id: int, *, limit: int = 100
    ) -> list[UsageLedger]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(session, user_id=user_id, limit=limit)

    async def get_by_idempotency_key(
        self, session: AsyncSession, idempotency_key: str
    ) -> UsageLedger | None:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key 不得为空")
        return await self.get_by(session, idempotency_key=idempotency_key)

    async def insert_idempotent(
        self, session: AsyncSession, *, idempotency_key: str, **values: object
    ) -> UsageLedger:
        existing = await self.get_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            return existing
        return await self.insert(session, idempotency_key=idempotency_key, **values)
