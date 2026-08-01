"""M11a：reset_all dry-run 结构化清单（build_reset_plan / format_reset_plan）与护栏。

M11a 之前 dry-run 只产出「空报告 + 一句警告」。现在 reset_all 在 dry-run 时真实探测
四类资源（MySQL 表+行数 / Redis / Chroma 集合去留 / 产物目录），产出可被审计的清单。
本文件覆盖：运行时集合分类、清单可执行性判定、人类可读渲染、artifact 根目录护栏，
以及 build_reset_plan 对 SQLite 引擎的真实探测（Redis/Chroma 探测 fail-soft，不阻断）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.db.reset_all import (
    ResetPlan,
    ResetSafetyError,
    TablePlan,
    build_reset_plan,
    format_reset_plan,
    is_runtime_collection,
    validate_artifact_root,
)

from .conftest import isolated_database


def test_is_runtime_collection_classifies_correctly() -> None:
    assert is_runtime_collection("u_123_mem") is True
    assert is_runtime_collection("p_987_design") is True
    assert is_runtime_collection("cache_generate") is True
    assert is_runtime_collection("cache_gen") is True
    assert is_runtime_collection("kb_design") is False
    assert is_runtime_collection("components") is False
    assert is_runtime_collection("unknown_collection") is False


def test_reset_plan_safe_to_execute() -> None:
    ok = ResetPlan(tables=[TablePlan(name="users", rows=1)], unknown_tables=[], probe_errors=[])
    assert ok.safe_to_execute is True

    with_unknown = ResetPlan(unknown_tables=["some_old_table"])
    assert with_unknown.safe_to_execute is False

    with_probe_error = ResetPlan(probe_errors=["mysql: Boom"])
    assert with_probe_error.safe_to_execute is False


def test_format_reset_plan_renders_human_readable() -> None:
    plan = ResetPlan(
        database_target="mysql://127.0.0.1:3306/seed_ai",
        tables=[
            TablePlan(name="users", rows=3),
            TablePlan(name="z_legacy", rows=None, known=False),
        ],
        total_rows=3,
        redis={"target": "127.0.0.1:6379/0", "keys": 12, "action": "FLUSHDB"},
        chroma={
            "target": "1.2.3.4:8000",
            "to_delete": ["cache_gen"],
            "preserved": ["components"],
        },
        artifacts={"root": "/x/artifacts", "exists": True, "total_files": 2, "total_bytes": 2048},
        unknown_tables=["z_legacy"],
    )
    out = format_reset_plan(plan)
    assert "RESET DRY-RUN" in out
    assert "users" in out
    assert "z_legacy" in out
    assert "FLUSHDB" in out
    assert "cache_gen" in out
    assert "components" in out
    assert "❌" in out  # 存在 ORM 未定义表 → 不可执行


def test_validate_artifact_root_rejects_non_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ResetSafetyError, match="artifacts"):
        validate_artifact_root(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    resolved = validate_artifact_root(artifact_root)
    assert resolved.name == "artifacts"


def test_build_reset_plan_probes_sqlite() -> None:
    async def scenario() -> None:
        async with isolated_database() as (engine, _):
            plan = await build_reset_plan(database_engine=engine)
            assert plan.database_target  # 非空
            assert plan.total_rows >= 0
            # SQLite 里建的表都应是 ORM 已知表 → 无 unknown
            assert plan.unknown_tables == []
            assert isinstance(plan.safe_to_execute, bool)

    asyncio.run(scenario())
