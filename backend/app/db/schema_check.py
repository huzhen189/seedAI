from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Final

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import engine


logger = logging.getLogger("app.db.schema_check")

REQUIRED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "users",
        "projects",
        "conversations",
        "messages",
        "tasks",
        "tool_calls",
        "sir_snapshots",
        "session_audits",
        "agent_runs",
        "memory_storage_log",
        "feedback",
        "usage_ledger",
        "recycle_bin",
        "purge_jobs",
        "vector_collections",
        "user_model_keys",
        "paused_turns",
        "metrics_daily",
        "metrics_events",
        "qc_scores",
        "flow_checks",
        "output_guard_log",
        "degradations",
        "intent_decisions",
        "model_calls",
        "kb_change_log",
    }
)

STATISTICS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "metrics_daily",
        "metrics_events",
        "qc_scores",
        "flow_checks",
        "output_guard_log",
        "degradations",
        "intent_decisions",
        "model_calls",
        "kb_change_log",
    }
)

REQUIRED_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "users": frozenset(
        {
            "id",
            "tier",
            "token_budget_daily",
            "max_concurrent_sessions",
            "preferred_exec_model",
        }
    ),
    "projects": frozenset({"id", "user_id", "status", "config", "trashed_at", "deleted_at"}),
    "conversations": frozenset({"id", "project_id", "user_id", "status"}),
    "messages": frozenset(
        {"id", "conversation_id", "project_id", "turn_no", "content", "content_path", "metrics"}
    ),
    "tasks": frozenset({"id", "conversation_id", "status", "source", "version"}),
    "usage_ledger": frozenset(
        {"id", "user_id", "conversation_id", "input_tokens", "output_tokens", "cost_usd"}
    ),
    "qc_scores": frozenset(
        {"id", "user_id", "dimension", "score", "model_used", "auto"}
    ),
    "user_model_keys": frozenset({"id", "user_id", "provider", "api_key_enc", "status"}),
    "vector_collections": frozenset(
        {"id", "scope", "owner_id", "collection", "embedding_model", "dim", "status"}
    ),
}

REQUIRED_INDEXES: Final[dict[str, frozenset[str]]] = {
    "projects": frozenset({"ix_projects_user_created"}),
    "conversations": frozenset(
        {"ix_conversations_project_created", "ix_conversations_user_status_updated"}
    ),
    "messages": frozenset(
        {"ix_messages_conversation_created", "ix_messages_conversation_turn"}
    ),
    "tasks": frozenset({"ix_tasks_conversation_status", "ix_tasks_parent"}),
    "metrics_events": frozenset(
        {"ix_metrics_events_user_occurred", "ix_metrics_events_type_occurred"}
    ),
    "qc_scores": frozenset(
        {"ix_qc_scores_user_dimension_created", "ix_qc_scores_conversation_created"}
    ),
}

ENUMS: Final[dict[tuple[str, str], frozenset[str]]] = {
    ("projects", "status"): frozenset(
        {"draft", "active", "trashed", "purging", "deleted"}
    ),
    ("conversations", "status"): frozenset({"active", "archived", "trashed"}),
    ("tasks", "status"): frozenset({"pending", "running", "done", "failed", "cancelled"}),
    ("agent_runs", "status"): frozenset({"running", "completed", "failed", "aborted"}),
    ("user_model_keys", "status"): frozenset({"active", "disabled", "invalid"}),
    ("vector_collections", "status"): frozenset(
        {"ready", "building", "archived", "dropped"}
    ),
    ("qc_scores", "dimension"): frozenset(
        {
            "relevance",
            "completeness",
            "accuracy",
            "safety",
            "efficiency",
            "experience",
            "overall",
        }
    ),
}


@dataclass(frozen=True)
class SchemaIssue:
    code: str
    object_name: str
    detail: str


@dataclass(frozen=True)
class SchemaReport:
    dialect: str
    tables_checked: tuple[str, ...]
    issues: tuple[SchemaIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dialect": self.dialect,
            "tables_checked": list(self.tables_checked),
            "issues": [asdict(issue) for issue in self.issues],
        }


class SchemaValidationError(RuntimeError):
    def __init__(self, report: SchemaReport) -> None:
        details = "; ".join(
            f"[{issue.code}] {issue.object_name}: {issue.detail}" for issue in report.issues
        )
        super().__init__(f"数据库 schema 终态校验失败: {details}")
        self.report = report


def _enum_values(inspector: Any, table: str, column: str) -> set[str]:
    columns = {item["name"]: item for item in inspector.get_columns(table)}
    column_info = columns.get(column)
    if column_info is None:
        return set()
    enums = getattr(column_info["type"], "enums", None)
    if enums:
        return set(enums)
    constraints = inspector.get_check_constraints(table)
    sql = " ".join(str(item.get("sqltext", "")) for item in constraints)
    expected = ENUMS[(table, column)]
    return {value for value in expected if f"'{value}'" in sql or f'"{value}"' in sql}


def _check_schema_sync(connection: Connection) -> SchemaReport:
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    issues: list[SchemaIssue] = []

    for table in sorted(REQUIRED_TABLES - existing):
        issues.append(SchemaIssue("missing_table", table, "缺少必需表"))
    if "frontend_events" in existing:
        issues.append(
            SchemaIssue(
                "forbidden_table",
                "frontend_events",
                "前端事件必须统一写入 metrics_events，不应存在独立表",
            )
        )

    for table, required in REQUIRED_COLUMNS.items():
        if table not in existing:
            continue
        actual = {column["name"] for column in inspector.get_columns(table)}
        for column in sorted(required - actual):
            issues.append(SchemaIssue("missing_column", f"{table}.{column}", "缺少关键字段"))

    for table, required in REQUIRED_INDEXES.items():
        if table not in existing:
            continue
        actual = {index["name"] for index in inspector.get_indexes(table)}
        for index_name in sorted(required - actual):
            issues.append(SchemaIssue("missing_index", f"{table}.{index_name}", "缺少关键索引"))

    for (table, column), expected in ENUMS.items():
        if table not in existing:
            continue
        actual = _enum_values(inspector, table, column)
        if actual != set(expected):
            issues.append(
                SchemaIssue(
                    "enum_mismatch",
                    f"{table}.{column}",
                    f"期望 {sorted(expected)}，实际识别为 {sorted(actual)}",
                )
            )

    for table in sorted(STATISTICS_TABLES & existing):
        foreign_keys = inspector.get_foreign_keys(table)
        if foreign_keys:
            issues.append(
                SchemaIssue("statistics_fk", table, "统计表不得包含指向内容表的外键")
            )

    if "usage_ledger" in existing:
        foreign_keys = inspector.get_foreign_keys("usage_ledger")
        fk_columns = {
            column
            for foreign_key in foreign_keys
            for column in foreign_key.get("constrained_columns", [])
        }
        missing_fk_columns = {"user_id", "conversation_id"} - fk_columns
        if missing_fk_columns:
            issues.append(
                SchemaIssue(
                    "missing_fk",
                    "usage_ledger",
                    f"缺少外键字段: {sorted(missing_fk_columns)}",
                )
            )
        for foreign_key in foreign_keys:
            if set(foreign_key.get("constrained_columns", [])) & {
                "user_id",
                "conversation_id",
            }:
                options = foreign_key.get("options") or {}
                if str(options.get("ondelete", "")).upper() != "CASCADE":
                    issues.append(
                        SchemaIssue(
                            "fk_ondelete",
                            "usage_ledger",
                            "user_id/conversation_id 外键必须 ON DELETE CASCADE",
                        )
                    )

    return SchemaReport(
        dialect=connection.dialect.name,
        tables_checked=tuple(sorted(REQUIRED_TABLES & existing)),
        issues=tuple(issues),
    )


async def check_schema(
    database_engine: AsyncEngine = engine, *, raise_on_error: bool = True
) -> SchemaReport:
    try:
        async with database_engine.connect() as connection:
            report = await connection.run_sync(_check_schema_sync)
    except Exception as exc:
        logger.exception("数据库 schema 检查无法执行")
        raise RuntimeError(f"数据库 schema 检查无法执行: {exc}") from exc
    if not report.ok:
        logger.error("数据库 schema 校验失败: %s", report.as_dict())
        if raise_on_error:
            raise SchemaValidationError(report)
    else:
        logger.info("数据库 schema 校验通过，共检查 %s 张表", len(report.tables_checked))
    return report
