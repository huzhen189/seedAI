"""数据库引擎与会话(SQLAlchemy 2.0 async)。

提供:
  - `engine` / `SessionLocal`:异步引擎与会话工厂;
  - `init_db()`:启动时建表 + 增量补齐缺失列(schema diff,见下方说明)。
"""

import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

logger = logging.getLogger(__name__)

# 异步引擎:
#  - echo=False:不打 SQL 日志(避免刷屏,排查时临时改 True);
#  - future=True:启用 SQLAlchemy 2.0 风格 API(async_sessionmaker 等);
#  - database_url 已是异步驱动(mysql+aiomysql,见 config.model_post_init)。
#  - pool_pre_ping=True:每次从池取连接前先执行 `SELECT 1` 探活,若连接已被
#    服务端 / 公网 NAT / 防火墙静默断开(典型报错 2013 "Lost connection to MySQL
#    server during query"),立即丢弃并新建,避免拿到死连接直接 500(本地开发机跨公网
#    连云 MySQL 时,空闲连接常被几分钟级的防火墙超时掐断)。
#  - pool_recycle=1800:连接使用 30min 后强制回收重建,远小于 MySQL wait_timeout
#    (28800s)与常见防火墙空闲超时,双保险防止空闲死连接。
#  - pool_size / max_overflow:开发期合理并发上限。
engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=10,
    max_overflow=20,
)
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
    """安全初始化:补齐缺失列 + 种子用户。

    建表由 scripts/reset_all.py 统一管理;这里不自动建表,
    避免重启时意外触发 schema 变更。
    若关键表不存在则告警,提示运行 reset_all.py。
    """
    from .models import Base

    async with engine.begin() as conn:
        # 自动补齐缺失表(仅 create_all 新增, 不删改已有表) + 增量补齐缺失列
        def _ensure_schema(sync_conn):
            from sqlalchemy import inspect
            insp = inspect(sync_conn)
            existing = set(insp.get_table_names())
            expected = set(Base.metadata.tables.keys())
            missing_tables = expected - existing
            if missing_tables:
                # 仅创建缺失的表(安全: 不影响已有数据)
                Base.metadata.create_all(sync_conn)
                logger.info("数据库已自动创建缺失表: %s", ", ".join(sorted(missing_tables)))
            return existing
        existing_tables = await conn.run_sync(_ensure_schema)
        # 仅当表已存在时才做增量补齐(新增列)
        if existing_tables:
            added = await conn.run_sync(_add_missing_columns)
            if added:
                logger.info("数据库 schema 已自动补齐缺失列: %s", ", ".join(added))
            else:
                logger.debug("数据库 schema 与 model 一致,无缺失列需补齐")

    # 第三步:超级管理员种子注入(文档 §2.3)。
    # 首次启动(或任何时候)把 SEED_SUPER_ADMIN 指定的用户名角色置为 super_admin,
    # 解决"角色自举"问题 —— 普通注册只能得到 user,初始超管只能由该环境变量赋予。
    await _seed_super_admin()
    # 第四步:默认超管用户自动创建(清库重建表后无需手动注册)。
    await _seed_default_user()


async def _seed_super_admin() -> None:
    """把 settings.seed_super_admin 指定的用户提升为 super_admin(若不存在则跳过)。"""
    username = (settings.seed_super_admin or "").strip()
    if not username:
        return
    from .models import User

    try:
        async with SessionLocal() as session:
            user = (
                await session.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()
            if user is None:
                logger.warning(
                    "SEED_SUPER_ADMIN=%s 对应的用户不存在,跳过注入(请先注册该账号)",
                    username,
                )
                return
            if user.role != "super_admin":
                user.role = "super_admin"
                await session.commit()
                logger.info("已将用户 '%s' 注入为 super_admin", username)
            else:
                logger.debug("用户 '%s' 已是 super_admin,无需变更", username)
    except Exception as e:  # 种子失败不应阻断启动
        logger.warning("super_admin 种子注入失败(已跳过): %s", e)


async def _seed_default_user() -> None:
    """自动创建默认超管用户(账号:huzhen, 每次 init_db 若不存在则创建)。"""
    from .models import User
    from .security import hash_password

    username = "huzhen"
    try:
        async with SessionLocal() as session:
            existing = (
                await session.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()
            if existing is not None:
                logger.debug("默认用户 '%s' 已存在,跳过", username)
                return
            user = User(
                username=username,
                nickname="小胡",
                email="785297147@qq.com",
                password_hash=hash_password("huzhen189"),
                role="super_admin",
                plan="enterprise",
            )
            session.add(user)
            await session.commit()
            logger.info("已创建默认超管用户: %s (super_admin)", username)
    except Exception as e:
        logger.warning("默认用户创建失败(已跳过): %s", e)


async def reset_db() -> dict:
    """前端触发的全量重置: DROP 表 → FLUSHDB Redis → 重建 → 种子用户。

    返回 {success, tables_dropped, redis_cleared, message}。
    注意: 调用后服务需重启才能完全生效(进程内 engine 连接已失效)。
    """
    from .models import Base

    result: dict = {"success": True, "tables_dropped": 0, "redis_cleared": False}

    # 1) DROP 所有表
    async with engine.begin() as conn:
        def _drop(sync_conn):
            insp = inspect(sync_conn)
            tables = insp.get_table_names()
            for t in tables:
                sync_conn.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
            return tables
        tables = await conn.run_sync(_drop)
        result["tables_dropped"] = len(tables)
        logger.info("reset_db: 已 DROP %s 张表", len(tables))

    # 2) FLUSHDB Redis
    try:
        from .cache import get_redis
        r = await get_redis()
        await r.flushdb()
        result["redis_cleared"] = True
        logger.info("reset_db: Redis 已清空")
    except Exception as e:
        logger.warning("reset_db: Redis 清理失败: %s", e)

    # 3) 重建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("reset_db: 表已重建")

    # 4) 补齐列 + 种子用户
    await init_db()

    await engine.dispose()
    result["message"] = "数据已清理并重建。业务服务(7101)将在 3 秒后自动重启；AI 服务(7102)请手动重启。"
    logger.info("reset_db: 完成, engine 已 dispose, 3s 后自动重启业务服务")
    return result


def schedule_biz_restart() -> None:
    """重置完成后延迟 3 秒重启业务服务(7101)。

    通过写 bat 脚本 + subprocess.Popen 启动, 旧进程 os._exit(0)
    避免端口冲突。AI 服务(7102)由用户手动重启。
    """
    import os as _os
    import subprocess as _sp
    import sys as _sys
    import tempfile as _tmp
    import time as _time

    def _restart() -> None:
        _time.sleep(3)
        old_pid = _os.getpid()
        venv_py = _sys.executable
        cwd = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..")
        bat = _os.path.join(_tmp.gettempdir(), "seedai_biz_restart.bat")
        lines = [
            "@echo off",
            # 先杀 7101 上所有 LISTENING 进程, 再起新的, 避免端口残留幽灵进程
            "for /f \"tokens=5\" %%a in ('netstat -ano ^| findstr :7101 ^| findstr LISTENING') do taskkill /PID %%a /F 2>nul",
            f"taskkill /PID {old_pid} /F 2>nul",
            "timeout /t 2 /nobreak >nul",
            f'cd /d "{cwd}"',
            f'"{venv_py}" -m uvicorn app.main:app --app-dir backend\\\\business --host 0.0.0.0 --port 7101',
        ]
        with open(bat, "w", encoding="ascii") as f:
            f.write("\n".join(lines))
        _sp.Popen(
            ["cmd", "/c", bat],
            creationflags=0x00000008,  # DETACHED_PROCESS
            close_fds=True,
        )

    import threading as _th
    t = _th.Thread(target=_restart, daemon=True)
    t.start()


async def get_db():
    async with SessionLocal() as session:
        yield session
