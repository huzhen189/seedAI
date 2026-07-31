"""SeedAI 全量重置协调器。

默认仅输出计划；实际执行必须同时提供 execute、环境许可和精确确认短语。
MySQL DDL 不支持事务回滚，因此任何失败都会立即尝试重建完整 schema，并把失败阶段写入报告。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import PROJECT_ROOT, settings
from app.models import Base, User, UserState
from app.security import hash_password

from .schema_check import check_schema
from .session import SessionLocal, engine


logger = logging.getLogger("app.db.reset_all")

CONFIRMATION_PHRASE: Final[str] = "RESET seed_ai"
PRESERVED_CHROMA_COLLECTIONS: Final[frozenset[str]] = frozenset(
    {
        "kb_design",
        "kb_intent",
        "rag_corpus",
        "components",
        "error_patterns",
        "intents",
    }
)
RUNTIME_COLLECTIONS: Final[frozenset[str]] = frozenset({"cache_generate", "cache_gen"})


class ResetSafetyError(RuntimeError):
    """全量重置未满足安全前置条件。"""


class ResetExecutionError(RuntimeError):
    """全量重置执行失败，包含可审计的阶段报告。"""

    def __init__(self, report: ResetReport, cause: BaseException) -> None:
        super().__init__(f"全量重置在阶段 {report.current_stage} 失败: {cause}")
        self.report = report
        self.cause = cause


@dataclass
class ResetReport:
    environment: str
    database_target: str
    executed: bool = False
    current_stage: str = "planned"
    dropped_tables: list[str] = field(default_factory=list)
    recreated_tables: int = 0
    redis_flushed: bool = False
    chroma_deleted: list[str] = field(default_factory=list)
    chroma_preserved: list[str] = field(default_factory=list)
    artifacts_removed: int = 0
    super_admin_created: bool = False
    schema_ok: bool = False
    recovery_attempted: bool = False
    recovery_succeeded: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def database_target(database_url: str) -> str:
    """返回不含凭证的数据库目标描述。"""
    url = make_url(database_url)
    database = url.database or "<none>"
    host = url.host or "local"
    port = f":{url.port}" if url.port else ""
    return f"{url.drivername}://{host}{port}/{database}"


def is_remote_database_target(database_url: str) -> bool:
    """识别公网/远程数据库，防止 ENV 误标 dev 时绕过生产保护。"""
    host = (make_url(database_url).host or "").strip().lower()
    return host not in {"", "localhost", "127.0.0.1", "::1", "mysql", "db"}


def validate_reset_request(
    *,
    execute: bool,
    allow_production: bool,
    confirmation: str,
    environment: str,
    database_url: str = "sqlite+aiosqlite:///:memory:",
) -> None:
    if not execute:
        return
    if confirmation != CONFIRMATION_PHRASE:
        raise ResetSafetyError(
            f"确认短语不匹配；必须精确传入 {CONFIRMATION_PHRASE!r}"
        )
    if (environment == "production" or is_remote_database_target(database_url)) and not allow_production:
        raise ResetSafetyError("生产环境或远程数据库重置必须显式 allow_production=True")
    if environment not in {"local", "test", "dev", "production"}:
        raise ResetSafetyError(f"未知运行环境: {environment}")


def is_runtime_collection(name: str) -> bool:
    """只识别文档定义的用户/项目隔离集合和生成缓存。"""
    return name in RUNTIME_COLLECTIONS or name.startswith("u_") or name.startswith("p_")


def validate_artifact_root(path: Path) -> Path:
    """拒绝盘符根、用户主目录及非 artifacts 目录，防止误删。"""
    resolved = path.expanduser().resolve()
    forbidden = {
        Path(resolved.anchor).resolve(),
        Path.home().resolve(),
        PROJECT_ROOT.resolve(),
        PROJECT_ROOT.parent.resolve(),
    }
    if resolved in forbidden:
        raise ResetSafetyError(f"拒绝清理高风险目录: {resolved}")
    if resolved.name.lower() != "artifacts":
        raise ResetSafetyError(f"产物根目录必须以 artifacts 命名: {resolved}")
    return resolved


def _drop_tables_sync(connection: Connection) -> list[str]:
    inspector = inspect(connection)
    tables = inspector.get_table_names()
    preparer = connection.dialect.identifier_preparer
    dialect = connection.dialect.name
    if dialect == "mysql":
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
    elif dialect == "sqlite":
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        for table in tables:
            quoted = preparer.quote(table)
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS {quoted}")
    finally:
        if dialect == "mysql":
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        elif dialect == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    return tables


async def drop_all_tables(database_engine: AsyncEngine) -> list[str]:
    async with database_engine.begin() as connection:
        return await connection.run_sync(_drop_tables_sync)


async def recreate_schema(database_engine: AsyncEngine) -> int:
    async with database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return len(Base.metadata.tables)


async def flush_redis(redis_url: str) -> None:
    try:
        import redis.asyncio as redis_async
    except ImportError as exc:
        raise RuntimeError("缺少 redis 依赖，不能完成全量重置") from exc
    client = redis_async.from_url(redis_url, decode_responses=True, protocol=2)
    try:
        await client.ping()
        await client.flushdb()
    finally:
        await client.aclose()


def _clear_chroma_sync(chroma_url: str) -> tuple[list[str], list[str]]:
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError("缺少 chromadb 依赖，不能完成全量重置") from exc
    parsed = urlparse(chroma_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResetSafetyError(f"CHROMA_URL 非法: {chroma_url}")
    client = chromadb.HttpClient(
        host=parsed.hostname,
        port=parsed.port or (443 if parsed.scheme == "https" else 8000),
        ssl=parsed.scheme == "https",
    )
    deleted: list[str] = []
    preserved: list[str] = []
    for collection in client.list_collections():
        name = collection.name if hasattr(collection, "name") else str(collection)
        if is_runtime_collection(name):
            client.delete_collection(name)
            deleted.append(name)
        else:
            preserved.append(name)
    return sorted(deleted), sorted(preserved)


async def clear_chroma(chroma_url: str) -> tuple[list[str], list[str]]:
    return await asyncio.to_thread(_clear_chroma_sync, chroma_url)


def _clear_artifacts_sync(artifact_root: Path) -> int:
    root = validate_artifact_root(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    removed = 0
    for child in root.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_symlink():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    return removed


async def clear_artifacts(artifact_root: Path) -> int:
    return await asyncio.to_thread(_clear_artifacts_sync, artifact_root)


async def ensure_super_admin(session_factory: Any = SessionLocal) -> bool:
    """重置脚本专属：幂等创建固定超管及 user_states 行。"""
    account = "huzhen"
    async with session_factory() as session:
        if not isinstance(session, AsyncSession):
            raise TypeError("session_factory 必须返回 AsyncSession")
        try:
            result = await session.execute(select(User).where(User.account == account))
            existing = result.scalar_one_or_none()
            if existing is not None:
                if existing.role != "super_admin" or not existing.is_super_admin:
                    existing.role = "super_admin"
                    existing.is_super_admin = True
                    await session.commit()
                return False
            user = User(
                account=account,
                nickname="超级管理员",
                display_name="超级管理员",
                email="huzhen@huzhen.net.cn",
                password_hash=hash_password("huzhen189"),
                role="super_admin",
                plan="enterprise",
                tier="max",
                is_super_admin=True,
            )
            session.add(user)
            await session.flush()
            session.add(UserState(user_id=user.id, status="idle", progress_pct=0))
            await session.commit()
            return True
        except BaseException:
            await session.rollback()
            raise


async def reset_all(
    *,
    execute: bool = False,
    allow_production: bool = False,
    confirmation: str = "",
    database_engine: AsyncEngine = engine,
    artifact_root: Path | None = None,
) -> ResetReport:
    """执行 MySQL/Redis/Chroma/产物全量重置；默认仅返回计划。"""
    validate_reset_request(
        execute=execute,
        allow_production=allow_production,
        confirmation=confirmation,
        environment=settings.env,
        database_url=settings.database_url,
    )
    report = ResetReport(
        environment=settings.env,
        database_target=database_target(settings.database_url),
        executed=execute,
    )
    if not execute:
        report.warnings.append("dry-run：未执行任何删除操作")
        return report

    root = validate_artifact_root(artifact_root or Path(settings.artifact_dir))
    try:
        report.current_stage = "drop_database"
        report.dropped_tables = await drop_all_tables(database_engine)

        report.current_stage = "flush_redis"
        await flush_redis(settings.redis_url)
        report.redis_flushed = True

        report.current_stage = "clear_chroma"
        deleted, preserved = await clear_chroma(settings.chroma_url)
        report.chroma_deleted = deleted
        report.chroma_preserved = preserved

        report.current_stage = "clear_artifacts"
        report.artifacts_removed = await clear_artifacts(root)

        report.current_stage = "recreate_schema"
        report.recreated_tables = await recreate_schema(database_engine)

        report.current_stage = "schema_check"
        schema_report = await check_schema(database_engine)
        report.schema_ok = schema_report.ok

        report.current_stage = "seed_super_admin"
        report.super_admin_created = await ensure_super_admin()

        report.current_stage = "completed"
        logger.warning("全量重置完成: %s", report.as_dict())
        return report
    except BaseException as exc:
        logger.exception("全量重置失败，开始尝试恢复 schema")
        report.recovery_attempted = True
        try:
            report.recreated_tables = await recreate_schema(database_engine)
            schema_report = await check_schema(database_engine)
            report.recovery_succeeded = schema_report.ok
            report.schema_ok = schema_report.ok
        except BaseException as recovery_error:
            logger.exception("全量重置失败后的 schema 恢复也失败")
            report.warnings.append(f"schema 恢复失败: {type(recovery_error).__name__}")
        raise ResetExecutionError(report, exc) from exc


__all__ = [
    "CONFIRMATION_PHRASE",
    "PRESERVED_CHROMA_COLLECTIONS",
    "RUNTIME_COLLECTIONS",
    "ResetExecutionError",
    "ResetReport",
    "ResetSafetyError",
    "clear_artifacts",
    "clear_chroma",
    "database_target",
    "drop_all_tables",
    "ensure_super_admin",
    "flush_redis",
    "is_remote_database_target",
    "is_runtime_collection",
    "recreate_schema",
    "reset_all",
    "validate_artifact_root",
    "validate_reset_request",
]
