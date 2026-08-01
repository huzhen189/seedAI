"""M2 运行真相表的隔离 schema 验证器。

旧业务链仍依赖既有数据库结构，不能在 M2 将新表/新字段变成全局启动硬门。该验证器
供 M2 隔离数据库与后续迁移/切换门禁使用；M11 切换时再提升为生产 schema 的强制检查。

索引/唯一约束以 ORM 真相模型（Base.metadata）为准推导，避免 v2 硬编码清单漂移——
与 schema_check.py 的修复保持一致：模型即契约。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Final

from sqlalchemy import UniqueConstraint, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.schema_check import SchemaIssue, SchemaReport
from app.models import Base


M2_REQUIRED_TABLES: Final[frozenset[str]] = frozenset(
    {"turns", "turn_checkpoints", "approvals", "artifacts", "deployments", "outbox_events"}
)
M2_REQUIRED_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "turns": frozenset(
        {
            "turn_id",
            "user_id",
            "conversation_id",
            "client_msg_id",
            "request_digest",
            "stream_id",
            "trace_id",
            "status",
            "run_epoch",
            "fencing_token",
            "lock_version",
        }
    ),
    "turn_checkpoints": frozenset({"turn_id", "run_epoch", "schema_version"}),
    "approvals": frozenset(
        {"approval_id", "turn_id", "action", "args_hash", "risk_level", "status", "expires_at"}
    ),
    "deployments": frozenset(
        {"project_id", "artifact_id", "manifest_digest", "environment", "status"}
    ),
    "outbox_events": frozenset(
        {"event_key", "aggregate_type", "aggregate_id", "event_type", "payload", "status"}
    ),
}


def _check_m2_schema_sync(connection: Connection) -> SchemaReport:
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    issues: list[SchemaIssue] = []

    for table in sorted(M2_REQUIRED_TABLES - existing):
        issues.append(SchemaIssue("missing_table", table, "M2 运行真相表缺失"))

    for table, required in M2_REQUIRED_COLUMNS.items():
        if table not in existing:
            continue
        actual = {column["name"] for column in inspector.get_columns(table)}
        for column in sorted(required - actual):
            issues.append(SchemaIssue("missing_column", f"{table}.{column}", "M2 关键字段缺失"))

    # 索引与唯一约束以 ORM 真相模型为准，避免 v2 硬编码清单漂移
    for table_name in sorted(M2_REQUIRED_TABLES & existing):
        table = Base.metadata.tables.get(table_name)
        if table is None:
            continue
        actual_idx = {i["name"] for i in inspector.get_indexes(table_name)}
        for idx in table.indexes:
            if idx.name and idx.name not in actual_idx:
                issues.append(
                    SchemaIssue("missing_index", f"{table_name}.{idx.name}", "M2 关键索引缺失")
                )
        actual_uniq = {
            constraint.get("name")
            for constraint in inspector.get_unique_constraints(table_name)
            if constraint.get("name")
        }
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            name = constraint.name
            if name and name not in actual_uniq:
                issues.append(
                    SchemaIssue("missing_unique", f"{table_name}.{name}", "M2 唯一约束缺失")
                )

    return SchemaReport(
        dialect=connection.dialect.name,
        tables_checked=tuple(sorted(M2_REQUIRED_TABLES & existing)),
        issues=tuple(issues),
    )


async def check_m2_runtime_schema(database_engine: AsyncEngine) -> SchemaReport:
    """检查 M2 的隔离 schema；失败时返回可审计报告，不隐式修改任何数据库。"""
    async with database_engine.connect() as connection:
        report = await connection.run_sync(_check_m2_schema_sync)
    if not report.ok:
        details = "; ".join(
            f"[{issue.code}] {issue.object_name}: {issue.detail}" for issue in report.issues
        )
        raise RuntimeError(f"M2 运行 schema 校验失败: {details}")
    return report


def report_as_dict(report: SchemaReport) -> dict[str, object]:
    """为 acceptance artifact/CI 提供 JSON 安全的报告。"""
    return {
        "ok": report.ok,
        "dialect": report.dialect,
        "tables_checked": list(report.tables_checked),
        "issues": [asdict(issue) for issue in report.issues],
    }
