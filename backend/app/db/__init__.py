"""新数据库初始化与会话出口。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base

from .session import SessionLocal, engine, get_db, transaction


async def init_db() -> None:
    """只创建新数据库缺失表；不删除、迁移或重置任何数据。"""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    return SessionLocal()


__all__ = ["SessionLocal", "engine", "get_db", "get_session", "init_db", "transaction"]
