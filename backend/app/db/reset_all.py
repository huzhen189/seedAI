"""SeedAI 全量重置协调器。

默认仅输出计划；实际执行必须同时提供 execute、环境许可和精确确认短语。
MySQL DDL 不支持事务回滚，因此任何失败都会立即尝试重建完整 schema，并把失败阶段写入报告。

M11a：dry-run 不再是「空报告 + 一句警告」，而是真实探测四类资源并逐项列清单
（表名 + 行数、Redis DB + key 数、Chroma 集合去留、产物子目录 + 文件数/体积），
让「切换生产前先看看要删什么」这件事可被审计。探测全程 fail-soft：
任一依赖不可达只记 probe_errors，不影响其余项，也绝不触发任何删除。

建超管已拆到 app.db.seed —— 重置只管清空重建，不顺手造高权限账号。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import PROJECT_ROOT, settings
from app.models import Base

from .schema_check import check_schema
from .seed import ensure_super_admin
from .seed_system_rules import seed_system_rules
from .session import engine


logger = logging.getLogger("app.db.reset_all")

CONFIRMATION_PHRASE: Final[str] = "RESET seed_ai"
PRESERVED_CHROMA_COLLECTIONS: Final[frozenset[str]] = frozenset(
    {
        "kb_design",
        "rag_corpus",
        "components",
        "error_patterns",
        "intents",
        # 系统规则语义索引（知识底座，重置保留；seed 阶段用最新 MySQL 行重建，保证 rule_id 一致）。
        "system_rules",
    }
)
# 运行时集合 = 承载用户/项目产生的数据，重置必须清空。
# 知识底座(components/error_patterns/intents/kb_design/rag_corpus 等 PRESERVED_*)不在此列，永远保留。
# 注：kb_intent 已并入 intents（intents 为唯一意图语义集合），不再单独保留。
RUNTIME_COLLECTIONS: Final[frozenset[str]] = frozenset(
    {
        "cache_generate",
        "cache_gen",
        "memory",
        "conversation_context",
        "user_preferences",
        "project_memory",
        "project_code",
    }
)


class ResetSafetyError(RuntimeError):
    """全量重置未满足安全前置条件。"""


class ResetExecutionError(RuntimeError):
    """全量重置执行失败，包含可审计的阶段报告。"""

    def __init__(self, report: ResetReport, cause: BaseException) -> None:
        super().__init__(f"全量重置在阶段 {report.current_stage} 失败: {cause}")
        self.report = report
        self.cause = cause


@dataclass
class TablePlan:
    """一张将被 DROP 的表。"""

    name: str
    rows: int | None = None
    known: bool = True  # 是否在 Base.metadata 中有对应 ORM 定义


@dataclass
class ResetPlan:
    """dry-run 清单：执行 reset 会删掉哪些东西。"""

    database_target: str = ""
    tables: list[TablePlan] = field(default_factory=list)
    unknown_tables: list[str] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)
    total_rows: int = 0
    redis: dict[str, Any] = field(default_factory=dict)
    chroma: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    probe_errors: list[str] = field(default_factory=list)

    @property
    def safe_to_execute(self) -> bool:
        """库里存在 ORM 未定义的表 = 可能是别的系统的库，拒绝闭眼执行。"""
        return not self.unknown_tables and not self.probe_errors


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
    system_rules_seeded: int = 0
    schema_ok: bool = False
    recovery_attempted: bool = False
    recovery_succeeded: bool = False
    warnings: list[str] = field(default_factory=list)
    plan: ResetPlan | None = None

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


# ------------------------------------------------------------------ dry-run 探测
# 全部 fail-soft：探测失败只记 probe_errors，绝不抛、绝不删。


def _probe_tables_sync(connection: Connection) -> tuple[list[TablePlan], list[str], list[str]]:
    inspector = inspect(connection)
    live = sorted(inspector.get_table_names())
    known = set(Base.metadata.tables.keys())
    preparer = connection.dialect.identifier_preparer
    plans: list[TablePlan] = []
    for name in live:
        rows: int | None
        try:
            rows = connection.execute(
                text(f"SELECT COUNT(*) FROM {preparer.quote(name)}")  # noqa: S608 — 标识符已被 quote
            ).scalar_one()
        except Exception:  # 单表统计失败不该毁掉整份清单
            rows = None
        plans.append(TablePlan(name=name, rows=rows, known=name in known))
    unknown = sorted(n for n in live if n not in known)
    missing = sorted(known - set(live))
    return plans, unknown, missing


async def probe_database(database_engine: AsyncEngine) -> tuple[list[TablePlan], list[str], list[str]]:
    async with database_engine.connect() as connection:
        return await connection.run_sync(_probe_tables_sync)


async def probe_redis(redis_url: str) -> dict[str, Any]:
    try:
        import redis.asyncio as redis_async
    except ImportError as exc:
        raise RuntimeError("缺少 redis 依赖") from exc
    parsed = urlparse(redis_url)
    db_index = parsed.path.lstrip("/") or "0"
    client = redis_async.from_url(redis_url, decode_responses=True, protocol=2)
    try:
        await client.ping()
        keys = int(await client.dbsize())
    finally:
        await client.aclose()
    return {
        "target": f"{parsed.hostname or 'local'}:{parsed.port or 6379}/{db_index}",
        "db_index": db_index,
        "keys": keys,
        "action": "FLUSHDB（只清当前 DB，不动其他 DB）",
    }


def _probe_chroma_sync(chroma_url: str) -> dict[str, Any]:
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError("缺少 chromadb 依赖") from exc
    parsed = urlparse(chroma_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResetSafetyError(f"CHROMA_URL 非法: {chroma_url}")
    client = chromadb.HttpClient(
        host=parsed.hostname,
        port=parsed.port or (443 if parsed.scheme == "https" else 8000),
        ssl=parsed.scheme == "https",
    )
    to_delete: list[str] = []
    preserved: list[str] = []
    for collection in client.list_collections():
        name = collection.name if hasattr(collection, "name") else str(collection)
        (to_delete if is_runtime_collection(name) else preserved).append(name)
    return {
        "target": f"{parsed.hostname}:{parsed.port or 8000}",
        "to_delete": sorted(to_delete),
        "preserved": sorted(preserved),
    }


async def probe_chroma(chroma_url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_probe_chroma_sync, chroma_url)


def _probe_artifacts_sync(artifact_root: Path, *, max_entries: int = 50) -> dict[str, Any]:
    root = validate_artifact_root(artifact_root)
    if not root.exists():
        return {"root": str(root), "exists": False, "entries": [], "total_files": 0, "total_bytes": 0}
    entries: list[dict[str, Any]] = []
    total_files = 0
    total_bytes = 0
    for child in sorted(root.iterdir()):
        if child.name == ".gitkeep":
            continue
        files = 0
        size = 0
        if child.is_dir():
            for f in child.rglob("*"):
                if f.is_file():
                    files += 1
                    size += f.stat().st_size
        elif child.is_file():
            files, size = 1, child.stat().st_size
        total_files += files
        total_bytes += size
        if len(entries) < max_entries:
            entries.append({"name": child.name, "files": files, "bytes": size})
    return {
        "root": str(root),
        "exists": True,
        "entries": entries,
        "truncated": total_files > 0 and len(entries) >= max_entries,
        "total_files": total_files,
        "total_bytes": total_bytes,
    }


async def probe_artifacts(artifact_root: Path) -> dict[str, Any]:
    return await asyncio.to_thread(_probe_artifacts_sync, artifact_root)


async def build_reset_plan(
    *,
    database_engine: AsyncEngine = engine,
    artifact_root: Path | None = None,
) -> ResetPlan:
    """探测四类资源，产出「将删什么」的结构化清单。不做任何删除。"""
    plan = ResetPlan(database_target=database_target(settings.database_url))

    try:
        plan.tables, plan.unknown_tables, plan.missing_tables = await probe_database(database_engine)
        plan.total_rows = sum(t.rows or 0 for t in plan.tables)
    except Exception as exc:
        plan.probe_errors.append(f"mysql: {type(exc).__name__}: {exc}")

    try:
        plan.redis = await probe_redis(settings.redis_url)
    except Exception as exc:
        plan.probe_errors.append(f"redis: {type(exc).__name__}: {exc}")

    try:
        plan.chroma = await probe_chroma(settings.chroma_url)
    except Exception as exc:
        plan.probe_errors.append(f"chroma: {type(exc).__name__}: {exc}")

    try:
        plan.artifacts = await probe_artifacts(artifact_root or Path(settings.artifact_dir))
    except Exception as exc:
        plan.probe_errors.append(f"artifacts: {type(exc).__name__}: {exc}")

    return plan


def format_reset_plan(plan: ResetPlan) -> str:
    """人类可读清单，供切换生产前肉眼复核。"""
    lines = [
        "=" * 72,
        "RESET DRY-RUN 清单（未执行任何删除）",
        "=" * 72,
        f"MySQL 目标 : {plan.database_target}",
        f"将 DROP 表 : {len(plan.tables)} 张，合计 {plan.total_rows} 行",
    ]
    for t in plan.tables:
        flag = "" if t.known else "  <== ORM 未定义！"
        rows = "?" if t.rows is None else str(t.rows)
        lines.append(f"    - {t.name:<28} {rows:>8} 行{flag}")
    if plan.unknown_tables:
        lines.append(f"  ⚠ ORM 未定义的表: {plan.unknown_tables} —— 这可能不是 SeedAI 的库，禁止执行")
    if plan.missing_tables:
        lines.append(f"  · ORM 有但库中缺失（重建时会创建）: {plan.missing_tables}")

    if plan.redis:
        lines += [
            "",
            f"Redis 目标 : {plan.redis.get('target')}",
            f"    将清空 {plan.redis.get('keys')} 个 key（{plan.redis.get('action')}）",
        ]
    if plan.chroma:
        lines += [
            "",
            f"Chroma 目标: {plan.chroma.get('target')}",
            f"    将删除: {plan.chroma.get('to_delete') or '(无)'}",
            f"    将保留: {plan.chroma.get('preserved') or '(无)'}",
        ]
    if plan.artifacts:
        a = plan.artifacts
        mb = (a.get("total_bytes") or 0) / 1048576
        lines += ["", f"产物目录  : {a.get('root')}", f"    {a.get('total_files')} 个文件 / {mb:.2f} MB"]
        for e in a.get("entries", []):
            lines.append(f"    - {e['name']:<28} {e['files']:>6} 文件 {e['bytes'] / 1048576:>8.2f} MB")
        if a.get("truncated"):
            lines.append("    …（条目已截断）")

    if plan.probe_errors:
        lines += ["", "⚠ 探测失败项（清单不完整，禁止据此执行）:"]
        lines += [f"    - {e}" for e in plan.probe_errors]

    lines += [
        "",
        f"结论: {'✅ 清单完整，可据此决定是否执行' if plan.safe_to_execute else '❌ 清单不可信，禁止执行'}",
        f"执行方式: reset_all(execute=True, confirmation={CONFIRMATION_PHRASE!r})",
        "=" * 72,
    ]
    return "\n".join(lines)


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


async def reset_all(
    *,
    execute: bool = False,
    allow_production: bool = False,
    confirmation: str = "",
    database_engine: AsyncEngine = engine,
    artifact_root: Path | None = None,
    seed_super_admin: bool = False,
    reseed_system_rules: bool = True,
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
        report.plan = await build_reset_plan(
            database_engine=database_engine, artifact_root=artifact_root
        )
        if report.plan.unknown_tables:
            report.warnings.append(
                f"库中存在 ORM 未定义的表 {report.plan.unknown_tables}，禁止执行重置"
            )
        for err in report.plan.probe_errors:
            report.warnings.append(f"探测失败：{err}")
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

        # 系统规则（刚性、零容错）随 schema 重建而回归：幂等重插 MySQL + 重建向量集合。
        # 整库 DROP 后无法「跳过表」，故采用「脚本内重插」保证规则不被清空（可审计/可回滚）。
        if reseed_system_rules:
            report.current_stage = "seed_system_rules"
            try:
                report.system_rules_seeded = await seed_system_rules()
            except Exception as exc:  # noqa: BLE001
                logger.warning("系统规则 seed 失败(已忽略): %s", exc, exc_info=True)
                report.warnings.append(f"系统规则 seed 失败: {type(exc).__name__}: {exc}")
        else:
            report.warnings.append("未重插系统规则；需要时执行 app.db.seed_system_rules.seed_system_rules()")

        # 建超管默认不做（拆到 app.db.seed，M11d 独立执行）——
        # 「重置一下看看」不该顺手造出一个高权限账号。
        if seed_super_admin:
            report.current_stage = "seed_super_admin"
            report.super_admin_created = await ensure_super_admin()
        else:
            report.warnings.append("未建超管；需要时执行 app.db.seed.ensure_super_admin()")

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
            # 恢复阶段也尽量把系统规则补回（幂等，失败不影响恢复结论）。
            if reseed_system_rules:
                try:
                    report.system_rules_seeded = await seed_system_rules()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("恢复阶段系统规则 seed 失败: %s", exc)
        except BaseException as recovery_error:
            logger.exception("全量重置失败后的 schema 恢复也失败")
            report.warnings.append(f"schema 恢复失败: {type(recovery_error).__name__}")
        raise ResetExecutionError(report, exc) from exc


__all__ = [
    "CONFIRMATION_PHRASE",
    "PRESERVED_CHROMA_COLLECTIONS",
    "RUNTIME_COLLECTIONS",
    "ResetExecutionError",
    "ResetPlan",
    "ResetReport",
    "ResetSafetyError",
    "TablePlan",
    "build_reset_plan",
    "clear_artifacts",
    "clear_chroma",
    "database_target",
    "drop_all_tables",
    "ensure_super_admin",
    "flush_redis",
    "format_reset_plan",
    "is_remote_database_target",
    "is_runtime_collection",
    "probe_artifacts",
    "probe_chroma",
    "probe_database",
    "probe_redis",
    "recreate_schema",
    "reset_all",
    "seed_system_rules",
    "validate_artifact_root",
    "validate_reset_request",
]
