"""数据库引擎与会话(SQLAlchemy 2.0 async)。"""

import contextlib

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings


# 异步引擎:
#  - echo=False:不打 SQL 日志(避免刷屏,排查时临时改 True);
#  - future=True:启用 SQLAlchemy 2.0 风格 API(async_sessionmaker 等);
#  - database_url 已是异步驱动(mysql+aiomysql,见 config.model_post_init)。
engine = create_async_engine(settings.database_url, echo=False, future=True)
# expire_on_commit=False:commit 后对象属性不失效,避免后续访问触发隐式查询
# (生成流里要反复读 user_text 等,关掉更顺)。
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
                    text("ALTER TABLE users ADD COLUMN nickname VARCHAR(64) NOT NULL DEFAULT ''")
                )
            except Exception as e:  # 重复列(1060)等已存在场景,忽略
                msg = str(e)
                if "Duplicate column" in msg or "1060" in msg or "column" in msg.lower():
                    pass
                else:
                    raise
            # 兼容性迁移: email 改为可空(老表为 NOT NULL DEFAULT '', 无邮箱用户会撞唯一索引), 幂等
            with contextlib.suppress(Exception):
                await conn.execute(text("ALTER TABLE users MODIFY email VARCHAR(128) NULL"))


async def get_db():
    async with SessionLocal() as session:
        yield session
