from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigLoadError, RuntimeConfig, Settings, runtime_config


def test_runtime_config_matches_frozen_contract() -> None:
    assert set(runtime_config.models.slots) == {
        "intent_lite",
        "intent_strong",
        "exec_standard",
        "exec_pro",
        "exec_ultra",
    }
    assert runtime_config.models.embedding.model_id == "text-embedding-v3"
    assert runtime_config.models.embedding.dimensions == 1024
    assert runtime_config.models.slot("intent_lite").tenant_selectable is False
    assert runtime_config.models.slot("exec_standard").stages == ["S6"]
    assert runtime_config.models.slot("exec_ultra").billing == "metered"


def test_router_thresholds_and_execution_guards() -> None:
    thresholds = runtime_config.router.thresholds
    execution = runtime_config.router.execution

    assert thresholds.primary_high == 0.85
    assert thresholds.primary_low == 0.5
    assert thresholds.secondary == 0.7
    assert thresholds.ambiguity_margin == 0.08
    assert execution.serial_task_dag is True
    assert execution.max_plan_revisions == 2
    assert execution.max_tasks == 20
    assert execution.task_timeout_seconds == 300
    assert execution.tool_retry_attempts == 3


def test_quota_decisions_are_explicit() -> None:
    for tier_name in ("free", "pro", "max"):
        tier = runtime_config.quota.tier(tier_name)
        assert tier.token_budget_daily == 5_000_000
        assert tier.project_token_budget_daily == 5_000_000
        assert tier.rpm == 60
        assert tier.max_concurrent_sessions == 5
        assert tier.session_token_budget == 2_000_000
    assert runtime_config.quota.warning_ratio == 0.8


def test_production_settings_fail_closed_without_required_secrets() -> None:
    with pytest.raises(ValueError, match="生产配置校验失败"):
        Settings(
            _env_file=None,
            env="production",
            jwt_secret="short",
            database_url="sqlite+aiosqlite:///./unsafe.db",
            provider_encryption_key="",
            qwen_api_key="",
            qwen_embedding_key="",
            hy3_api_key="",
            deepseek_api_key="",
            cos_secret_id="",
            cos_secret_key="",
        )


def test_model_base_url_allows_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, env="test")
    monkeypatch.setenv("HY3_BASE_URL", "https://example.invalid/v1")

    assert settings.model_base_url("exec_standard") == "https://example.invalid/v1"


def test_missing_yaml_file_fails_with_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="配置文件不存在"):
        RuntimeConfig.load(tmp_path)
