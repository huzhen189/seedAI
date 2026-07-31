from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.users")


class UsersRepo(BaseRepo[User]):
    model = User

    async def get_by_account(self, session: AsyncSession, account: str) -> User | None:
        normalized = account.strip()
        if not normalized:
            raise ValueError("account 不得为空")
        return await self.get_by(session, account=normalized)

    async def get_by_email(self, session: AsyncSession, email: str) -> User | None:
        return await self.by_email(session, email)

    async def by_email(self, session: AsyncSession, email: str) -> User | None:
        normalized = email.strip().lower()
        if not normalized:
            raise ValueError("email 不得为空")
        return await self.get_by(session, email=normalized)

    async def by_tier(
        self, session: AsyncSession, tier: str, *, limit: int = 100
    ) -> list[User]:
        return await self.list(session, tier=tier, limit=limit)

    async def update_role(self, session: AsyncSession, user: User, role: str) -> User:
        normalized = role.strip()
        if not normalized:
            raise ValueError("role 不得为空")
        return await self.update(session, user, role=normalized)

    async def update_plan(self, session: AsyncSession, user: User, plan: str) -> User:
        normalized = plan.strip()
        if not normalized:
            raise ValueError("plan 不得为空")
        return await self.update(session, user, plan=normalized)

    async def update_profile(
        self,
        session: AsyncSession,
        user: User,
        *,
        nickname: str | None = None,
        email: str | None = None,
        password_hash: str | None = None,
    ) -> User:
        values: dict[str, Any] = {}
        if nickname is not None:
            values["nickname"] = nickname.strip()
        if email is not None:
            values["email"] = email.strip().lower() or None
        if password_hash is not None:
            if not password_hash:
                raise ValueError("password_hash 不得为空")
            values["password_hash"] = password_hash
        if not values:
            return user
        return await self.update(session, user, **values)


user_repo = UsersRepo()
