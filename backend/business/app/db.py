"""数据库引擎与会话(SQLAlchemy 2.0 async)。"""

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
    """建表(M0 用 create_all;生产改用 Alembic 迁移)。

    User 表的 nickname / email 等字段已在 `models.User` 中声明(create_all 会自动建列),
    无需在运行时 ALTER;数据库 schema 由迁移脚本统一管理。之前为兼容老表做的
    "ALTER TABLE users ADD COLUMN nickname / MODIFY email" 冗余代码已移除。
    """
    from .models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with SessionLocal() as session:
        yield session
