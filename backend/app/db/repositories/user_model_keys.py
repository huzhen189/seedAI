from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserModelKey

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.user_model_keys")


class UserModelKeysRepo(BaseRepo[UserModelKey]):
    model = UserModelKey

    async def by_user_and_provider(
        self, session: AsyncSession, user_id: int, provider: str
    ) -> UserModelKey | None:
        if user_id <= 0 or not provider.strip():
            raise ValueError("user_id 必须为正整数且 provider 不得为空")
        return await self.get_by(session, user_id=user_id, provider=provider)

    async def active_for_user(
        self, session: AsyncSession, user_id: int
    ) -> list[UserModelKey]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(session, user_id=user_id, status="active", limit=100)
