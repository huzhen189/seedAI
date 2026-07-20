"""User Repository —— Redis 缓存优先读 + 双写。"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User
from .base import BaseRepo

logger = logging.getLogger("business.repo.user")


class UserRepo(BaseRepo[User]):
    model = User
    cache_prefix = "user"
    cache_ttl = 9000  # 150 分钟（×5）
    cache_enabled = True

    async def get_by_username(self, db: AsyncSession, username: str) -> Optional[User]:
        """按用户名查(走 MySQL 唯一索引, 不缓存)。"""
        return await self.get_by(db, username=username)

    async def get_by_email(self, db: AsyncSession, email: str) -> Optional[User]:
        """按邮箱查(走 MySQL 唯一索引, 不缓存)。"""
        return await self.get_by(db, email=email)

    async def update_role(self, db: AsyncSession, user: User, role: str) -> User:
        """改角色——立即失效 Redis, 防旧权限被缓存命中。"""
        return await self.update(db, user, role=role)

    async def update_plan(self, db: AsyncSession, user: User, plan: str) -> User:
        """改套餐——立即失效 Redis。"""
        return await self.update(db, user, plan=plan)

    async def update_profile(
        self, db: AsyncSession, user: User,
        nickname: str | None = None,
        email: str | None = None,
        password_hash: str | None = None,
    ) -> User:
        """更新个人信息(部分字段)。"""
        data = {}
        if nickname is not None:
            data["nickname"] = nickname
        if email is not None:
            data["email"] = email
        if password_hash is not None:
            data["password_hash"] = password_hash
        if not data:
            return user
        return await self.update(db, user, **data)


# 模块级单例
user_repo = UserRepo()
