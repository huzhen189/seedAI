"""独立种子数据(M11d)：超级管理员。

从 reset_all 中拆出 —— 重置只负责「清空 + 重建 schema」，
建账号是另一件事，混在一起会让「重置一下看看」意外造出一个高权限账号。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.security import hash_password

from .session import SessionLocal


logger = logging.getLogger("app.db.seed")

SUPER_ADMIN_ACCOUNT = "huzhen"


async def ensure_super_admin(
    session_factory: Any = SessionLocal,
    *,
    account: str = SUPER_ADMIN_ACCOUNT,
    password: str = "huzhen189",
) -> bool:
    """幂等创建/修复固定超管；返回 True 表示本次新建。"""
    async with session_factory() as session:
        if not isinstance(session, AsyncSession):
            raise TypeError("session_factory 必须返回 AsyncSession")
        try:
            existing = (
                await session.execute(select(User).where(User.account == account))
            ).scalar_one_or_none()
            if existing is not None:
                if existing.role != "super_admin" or not existing.is_super_admin:
                    existing.role = "super_admin"
                    existing.is_super_admin = True
                    await session.commit()
                    logger.warning("已把既有账号 %s 提升为 super_admin", account)
                return False
            session.add(
                User(
                    account=account,
                    nickname="超级管理员",
                    display_name="超级管理员",
                    email="huzhen@huzhen.net.cn",
                    password_hash=hash_password(password),
                    role="super_admin",
                    plan="enterprise",
                    tier="max",
                    is_super_admin=True,
                )
            )
            await session.commit()
            logger.warning("已创建超级管理员 %s", account)
            return True
        except BaseException:
            await session.rollback()
            raise


__all__ = ["SUPER_ADMIN_ACCOUNT", "ensure_super_admin"]
