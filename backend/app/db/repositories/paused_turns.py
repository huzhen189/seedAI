from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PausedTurn

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.paused_turns")


class PausedTurnsRepo(BaseRepo[PausedTurn]):
    model = PausedTurn

    async def by_turn_id(self, session: AsyncSession, turn_id: str) -> PausedTurn | None:
        if not turn_id.strip():
            raise ValueError("turn_id 不得为空")
        return await self.get_by(session, turn_id=turn_id)

    async def paused_for_user(
        self, session: AsyncSession, user_id: int, *, limit: int = 100
    ) -> list[PausedTurn]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(session, user_id=user_id, status="paused", limit=limit)
