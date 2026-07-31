from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Degradation

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.degradations")


class DegradationsRepo(BaseRepo[Degradation]):
    model = Degradation

    async def by_feature(
        self, session: AsyncSession, feature: str, *, limit: int = 100
    ) -> list[Degradation]:
        if not feature.strip():
            raise ValueError("feature 不得为空")
        return await self.list(session, feature=feature, limit=limit)
