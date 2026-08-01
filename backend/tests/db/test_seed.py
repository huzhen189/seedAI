"""M11a：独立种子数据 ensure_super_admin 的幂等性与提升语义。

建超管已从 reset_all 拆出（reset 只管清空重建，不顺手造高权限账号）。
这里验证：首次调用新建并返回 True；重复调用不新建返回 False；既有普通账号会被提升为 super_admin。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.db.seed import ensure_super_admin
from app.models import User

from .conftest import isolated_database


def test_ensure_super_admin_creates_once_then_idempotent() -> None:
    async def scenario() -> None:
        async with isolated_database() as (_, session_factory):
            created = await ensure_super_admin(
                session_factory=session_factory, account="su_a", password="pw_a"
            )
            assert created is True

            async with session_factory() as session:
                user = (
                    await session.execute(select(User).where(User.account == "su_a"))
                ).scalar_one()
                assert user.role == "super_admin"

            # 二次调用不应再新建，返回 False
            created2 = await ensure_super_admin(
                session_factory=session_factory, account="su_a", password="pw_a"
            )
            assert created2 is False

            async with session_factory() as session:
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(User)
                        .where(User.account == "su_a")
                    )
                ).scalar_one()
                assert count == 1

    asyncio.run(scenario())


def test_ensure_super_admin_promotes_existing_user() -> None:
    async def scenario() -> None:
        async with isolated_database() as (_, session_factory):
            async with session_factory() as session:
                session.add(
                    User(
                        account="promote_me",
                        display_name="PM",
                        password_hash="x",
                        role="user",
                        status="active",
                    )
                )
                await session.commit()

            # 账号已存在 → 视为「未新建」，但已提升为 super_admin
            promoted = await ensure_super_admin(
                session_factory=session_factory, account="promote_me", password="pw"
            )
            assert promoted is False

            async with session_factory() as session:
                user = (
                    await session.execute(
                        select(User).where(User.account == "promote_me")
                    )
                ).scalar_one()
                assert user.role == "super_admin"

    asyncio.run(scenario())
