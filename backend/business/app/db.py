"""数据库引擎与会话(SQLAlchemy 2.0 async)。"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """建表(M0 用 create_all;生产改用 Alembic 迁移)。"""
    from sqlalchemy import text

    from .models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # MySQL 时确保字符集(仅提示性,SQLite 忽略)
    if settings.database_url.startswith("mysql"):
        async with engine.begin() as conn:
            await conn.execute(text("SET NAMES utf8mb4;"))


async def get_db():
    async with SessionLocal() as session:
        yield session
