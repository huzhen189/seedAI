from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


logger = logging.getLogger("app.db.session")


def create_engine(database_url: str) -> AsyncEngine:
    options: dict[str, Any] = {
        "echo": False,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
    if database_url.startswith("mysql+"):
        options.update(pool_size=10, max_overflow=20)
    try:
        return create_async_engine(database_url, **options)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"无法创建异步数据库引擎，请检查 DATABASE_URL: {exc}") from exc


engine = create_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    session = SessionLocal()
    try:
        yield session
    except BaseException:
        try:
            await session.rollback()
        except Exception as rollback_error:
            logger.exception("请求数据库会话回滚失败: %s", rollback_error)
        raise
    finally:
        try:
            await session.close()
        except Exception as close_error:
            logger.exception("请求数据库会话关闭失败: %s", close_error)
            raise RuntimeError("数据库会话关闭失败") from close_error


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    session = SessionLocal()
    try:
        yield session
        await session.commit()
    except BaseException:
        try:
            await session.rollback()
        except Exception as rollback_error:
            logger.exception("事务回滚失败: %s", rollback_error)
        raise
    finally:
        try:
            await session.close()
        except Exception as close_error:
            logger.exception("事务会话关闭失败: %s", close_error)
            raise RuntimeError("事务会话关闭失败") from close_error
