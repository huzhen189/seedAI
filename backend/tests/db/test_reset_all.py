from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.db.reset_all import (
    CONFIRMATION_PHRASE,
    ResetSafetyError,
    clear_artifacts,
    database_target,
    is_remote_database_target,
    is_runtime_collection,
    reset_all,
    validate_artifact_root,
    validate_reset_request,
)


def test_reset_is_dry_run_by_default() -> None:
    report = asyncio.run(reset_all())

    assert report.executed is False
    assert report.current_stage == "planned"
    assert report.dropped_tables == []
    assert report.redis_flushed is False
    # dry-run 首条警告固定为「未执行删除」，后续可能因探测到未知表/依赖不可达而追加
    assert report.warnings[0] == "dry-run：未执行任何删除操作"
    assert report.plan is not None


def test_production_execution_requires_both_flags_and_exact_phrase() -> None:
    with pytest.raises(ResetSafetyError, match="确认短语"):
        validate_reset_request(
            execute=True,
            allow_production=True,
            confirmation="wrong",
            environment="production",
        )

    with pytest.raises(ResetSafetyError, match="allow_production"):
        validate_reset_request(
            execute=True,
            allow_production=False,
            confirmation=CONFIRMATION_PHRASE,
            environment="production",
        )

    validate_reset_request(
        execute=True,
        allow_production=True,
        confirmation=CONFIRMATION_PHRASE,
        environment="production",
    )

    with pytest.raises(ResetSafetyError, match="远程数据库"):
        validate_reset_request(
            execute=True,
            allow_production=False,
            confirmation=CONFIRMATION_PHRASE,
            environment="dev",
            database_url="mysql+aiomysql://user:secret@db.example.com/seed_ai",
        )


def test_database_target_classification() -> None:
    assert is_remote_database_target("sqlite+aiosqlite:///:memory:") is False
    assert is_remote_database_target("mysql+aiomysql://mysql:3306/seed_ai") is False
    assert is_remote_database_target("mysql+aiomysql://127.0.0.1:3306/seed_ai") is False
    assert is_remote_database_target("mysql+aiomysql://1.12.219.195:3306/seed_ai") is True


def test_runtime_collection_filter_is_fail_closed() -> None:
    assert is_runtime_collection("u_123_mem") is True
    assert is_runtime_collection("p_987_design") is True
    assert is_runtime_collection("cache_generate") is True
    assert is_runtime_collection("kb_design") is False
    assert is_runtime_collection("kb_intent") is False
    assert is_runtime_collection("unknown_collection") is False


def test_artifact_cleanup_only_accepts_artifacts_directory(tmp_path: Path) -> None:
    with pytest.raises(ResetSafetyError, match="artifacts"):
        validate_artifact_root(tmp_path)

    artifact_root = tmp_path / "artifacts"
    nested = artifact_root / "12" / "34" / "v1"
    nested.mkdir(parents=True)
    (nested / "index.html").write_text("ok", encoding="utf-8")
    (artifact_root / ".gitkeep").write_text("", encoding="utf-8")

    removed = asyncio.run(clear_artifacts(artifact_root))

    assert removed == 1
    assert artifact_root.exists()
    assert (artifact_root / ".gitkeep").exists()
    assert list(artifact_root.iterdir()) == [artifact_root / ".gitkeep"]


def test_database_target_never_exposes_credentials() -> None:
    target = database_target("mysql+aiomysql://user:secret@example.com:3306/seed_ai")

    assert target == "mysql+aiomysql://example.com:3306/seed_ai"
    assert "user" not in target
    assert "secret" not in target
