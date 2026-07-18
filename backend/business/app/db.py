"""数据库引擎与会话(SQLAlchemy 2.0 async)。

提供:
  - `engine` / `SessionLocal`:异步引擎与会话工厂;
  - `init_db()`:启动时建表 + 增量补齐缺失列(schema diff,见下方说明)。
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

logger = logging.getLogger(__name__)

# 异步引擎:
#  - echo=False:不打 SQL 日志(避免刷屏,排查时临时改 True);
#  - future=True:启用 SQLAlchemy 2.0 风格 API(async_sessionmaker 等);
#  - database_url 已是异步驱动(mysql+aiomysql,见 config.model_post_init)。
engine = create_async_engine(settings.database_url, echo=False, future=True)
# expire_on_commit=False:commit 后对象属性不失效,避免后续访问触发隐式查询
# (生成流里要反复读字段,关掉更顺)。
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _add_missing_columns(sync_conn) -> list[str]:
    """schema diff:对比 model 与数据库实际表,对缺失列执行 ALTER TABLE ADD COLUMN。

    仅**新增**列(不删列、不改列类型),以免破坏既有数据。返回本次新增的列描述列表。

    设计取舍:
      - 用 `inspect` 读取数据库真实列名,与 `Base.metadata` 中各 model 的列做差集;
      - 缺失列的类型直接由 `Column.type.compile(dialect=...)` 编译为该数据库的 DDL 类型串
        (如 MySQL 下 `String(64)` -> `VARCHAR(64)`,SQLite 下 -> `VARCHAR(64)`),无需手写
        两套方言的类型映射,天然兼容 MySQL 与本地 SQLite 回落;
      - 列名/表名用 dialect 的 `identifier_preparer` 安全引用(自动加反引号/引号,防关键字冲突);
      - 非空列若有 `server_default`(SQL 默认值)或标量 `default`,自动补 `DEFAULT` 子句,否则补
        `NOT NULL`。**重要约束:新增 NOT NULL 且无默认值的列,在表已有数据时会在 MySQL 报
        "Invalid use of NULL"** —— 因此给列加非空约束时务必同时声明 `server_default` 或标量 `default`。
    """
    from .models import Base

    dialect = sync_conn.dialect
    preparer = dialect.identifier_preparer
    inspector = inspect(sync_conn)
    added: list[str] = []

    for tname, table in Base.metadata.tables.items():
        # 表可能尚不存在(create_all 已建好,这里防御性跳过),交由 create_all 负责新建
        if not inspector.has_table(tname):
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(tname)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            # 按 model 的 SQLAlchemy 类型编译为该 dialect 的 DDL 类型串
            col_type = col.type.compile(dialect=dialect)
            col_name = preparer.quote(col.name)
            tbl_name = preparer.quote(tname)
            ddl = f"ALTER TABLE {tbl_name} ADD COLUMN {col_name} {col_type}"
            # 非空列:尽量带默认值,避免 MySQL 对既有数据报 "Invalid use of NULL"
            if not col.nullable:
                srv = col.server_default
                default = col.default
                if srv is not None and srv.arg is not None:
                    # server_default.arg 是 SQL 片段(如 "" 表示空串),字符串需转成合法 SQL 字面量
                    ddl += f" DEFAULT {srv.arg!r}" if isinstance(srv.arg, str) else f" DEFAULT {srv.arg}"
                elif default is not None and not callable(default.arg):
                    # 标量默认值(Python 值),用 !r 生成合法字面量(字符串加引号、整数原样)
                    ddl += f" DEFAULT {default.arg!r}"
                else:
                    ddl += " NOT NULL"
            sync_conn.execute(text(ddl))
            added.append(f"{tname}.{col.name} ({col_type})")
    return added


async def init_db():
    """建表 + 增量补齐缺失列。

    1. `create_all`:表不存在时建整张表(model 声明的全部列都在);
    2. `_add_missing_columns`:表已存在时,对比 model 与真实 schema,缺列则按 model 的
       字段结构/类型自动 ALTER ADD —— **改了 model(加字段)后,重启即自动补齐数据库**,
       无需手写迁移,适合开发期与小版本平滑升级。

    说明:这套 diff 是轻量自动迁移,**不替代 Alembic 这类正式迁移工具**;生产环境仍建议
    用迁移脚本管理 schema 演进(本项目约定 schema 由迁移统一管理)。它只补缺失列,
    不会删多余列、不会改列类型,避免误伤既有数据。
    """
    from .models import Base

    async with engine.begin() as conn:
        # 第一步:确保每张表存在(缺失的表一次建好,含 model 声明的全部列)
        await conn.run_sync(Base.metadata.create_all)
        # 第二步:对已有表补缺失列(未来 model 加新字段时自动生效)
        added = await conn.run_sync(_add_missing_columns)
    if added:
        logger.info("数据库 schema 已自动补齐缺失列: %s", ", ".join(added))
    else:
        logger.debug("数据库 schema 与 model 一致,无缺失列需补齐")


async def get_db():
    async with SessionLocal() as session:
        yield session
