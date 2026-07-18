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
            # 兼容性迁移:新增 nickname 列(老表可能无此列,幂等)
            try:
                await conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN nickname VARCHAR(64) NOT NULL DEFAULT ''"
                    )
                )
            except Exception as e:  # 重复列(1060)等已存在场景,忽略
                msg = str(e)
                if "Duplicate column" in msg or "1060" in msg or "column" in msg.lower():
                    pass
                else:
                    raise
            # 兼容性迁移: email 改为可空(老表为 NOT NULL DEFAULT '', 无邮箱用户会撞唯一索引), 幂等
            try:
                await conn.execute(
                    text("ALTER TABLE users MODIFY email VARCHAR(128) NULL")
                )
            except Exception:
                # 已可空 / 无此列等场景忽略
                pass


async def get_db():
    async with SessionLocal() as session:
        yield session
