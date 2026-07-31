from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelCall

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.model_calls")


class ModelCallsRepo(BaseRepo[ModelCall]):
    model = ModelCall

    async def by_model(
        self, session: AsyncSession, model: str, *, limit: int = 100
    ) -> list[ModelCall]:
        if not model.strip():
            raise ValueError("model 不得为空")
        return await self.list(session, model=model, limit=limit)

    async def insert_idempotent(
        self, session: AsyncSession, *, idempotency_key: str, **values: object
    ) -> ModelCall:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key 不得为空")
        existing = await self.get_by(session, idempotency_key=idempotency_key)
        if existing is not None:
            return existing
        return await self.insert(session, idempotency_key=idempotency_key, **values)
