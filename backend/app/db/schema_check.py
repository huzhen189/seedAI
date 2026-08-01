"""生产 schema 终态校验（源自 ORM 真相模型，零硬编码漂移）。

历史版本把「必需表 / 列 / 索引 / 枚举」硬编码为常量，v3 模型重写后这些常量全部
滞后于真实模型，导致 check_schema 在健康的 v3 库上也 false-alarm（缺 16 张表、content_path
等 v2 列、错误枚举值），进而让 reset_all 在 drop→recreate 之后误判 schema 未通过。

修复原则：期望完全从 ``Base.metadata``（ORM 真相模型）推导，模型即契约。
这样 check_schema 永远与当前模型自洽；reset_all 的 drop→recreate→check 闭环也不会被
陈旧的硬编码清单卡住。如需额外的业务级护栏（如统计表不得指向内容表外键），应在模型层
用显式约束表达，而不是在两份清单里重复维护。

M11a 伴随修改：reset_all 的 dry-run 不再调用 check_schema；仅执行路径在重建后校验，
因此这里必须对齐 v3 模型。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Final

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import engine
from app.models import Base


logger = logging.getLogger("app.db.schema_check")

# 向后兼容（tests/db/test_metadata.py 仍引用）：模型即契约，必需表 = 当前 ORM 全部表。
REQUIRED_TABLES: Final[frozenset[str]] = frozenset(Base.metadata.tables.keys())


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


def _enum_values(
    inspector: Any, table: str, column: str, expected: set[str]
) -> set[str]:
    """识别某列在库中的实际枚举取值。

    优先读列类型的 .enums（MySQL/PostgreSQL 原生 ENUM）；否则回退到解析 CHECK 约束。
    """
    columns = {item["name"]: item for item in inspector.get_columns(table)}
    column_info = columns.get(column)
    if column_info is None:
        return set()
    enums = getattr(column_info["type"], "enums", None)
    if enums:
        return set(enums)
    constraints = inspector.get_check_constraints(table)
    sql = " ".join(str(item.get("sqltext", "")) for item in constraints)
    return {value for value in expected if f"'{value}'" in sql or f'"{value}"' in sql}


def _check_schema_sync(connection: Connection) -> SchemaReport:
    """以 ``Base.metadata`` 为唯一真相源，逐项比对库结构。"""
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    issues: list[SchemaIssue] = []

    for table_name, table in sorted(Base.metadata.tables.items()):
        if table_name not in existing:
            issues.append(SchemaIssue("missing_table", table_name, "缺少模型定义的表"))
            continue

        # 列
        actual_cols = {c["name"] for c in inspector.get_columns(table_name)}
        for col_name in table.columns.keys():
            if col_name not in actual_cols:
                issues.append(
                    SchemaIssue("missing_column", f"{table_name}.{col_name}", "缺少关键字段")
                )

        # 索引
        actual_idx = {i["name"] for i in inspector.get_indexes(table_name)}
        for idx in table.indexes:
            if idx.name and idx.name not in actual_idx:
                issues.append(
                    SchemaIssue("missing_index", f"{table_name}.{idx.name}", "缺少关键索引")
                )

        # 枚举
        for col in table.columns.values():
            enums = getattr(col.type, "enums", None)
            if not enums:
                continue
            expected = set(enums)
            actual = _enum_values(inspector, table_name, col.name, expected)
            if actual != expected:
                issues.append(
                    SchemaIssue(
                        "enum_mismatch",
                        f"{table_name}.{col.name}",
                        f"期望 {sorted(expected)}，实际识别为 {sorted(actual)}",
                    )
                )

    # 库中存在模型未定义的表 = 漂移，可能是别的系统共用库，禁止据此执行 reset
    for extra in sorted(existing - set(Base.metadata.tables.keys())):
        issues.append(
            SchemaIssue("extra_table", extra, "库中存在模型未定义的表（漂移，禁止 reset）")
        )

    return SchemaReport(
        dialect=connection.dialect.name,
        tables_checked=tuple(sorted(set(Base.metadata.tables.keys()) & existing)),
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
