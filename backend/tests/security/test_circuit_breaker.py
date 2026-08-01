"""熔断器单元测试（§15.3）。"""

from __future__ import annotations

import time

from app.security_circuit import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitConfig,
    CircuitOpenError,
    CircuitState,
    get_breaker,
)


def test_closed_then_open_after_threshold() -> None:
    b = CircuitBreaker("p1", CircuitConfig(failure_threshold=3, cooldown_seconds=30))
    assert b.state is CircuitState.CLOSED
    assert b.allow() is True
    for _ in range(3):
        b.record_failure()
    assert b.state is CircuitState.OPEN
    assert b.allow() is False


def test_open_cooldown_transitions_to_half_open_and_recovers() -> None:
    b = CircuitBreaker("p2", CircuitConfig(failure_threshold=2, cooldown_seconds=0.05))
    for _ in range(2):
        b.record_failure()
    assert b.state is CircuitState.OPEN
    time.sleep(0.08)
    # 冷却结束，第一次 allow 触发 half_open
    assert b.allow() is True
    assert b.state is CircuitState.HALF_OPEN
    # half_open 只放 1 次试探，第二次应拒绝
    assert b.allow() is False
    b.record_success()
    assert b.state is CircuitState.CLOSED
    assert b.allow() is True


def test_half_open_failure_reopens() -> None:
    b = CircuitBreaker("p3", CircuitConfig(failure_threshold=2, cooldown_seconds=0.05))
    for _ in range(2):
        b.record_failure()
    time.sleep(0.08)
    assert b.allow() is True  # half_open
    b.record_failure()
    assert b.state is CircuitState.OPEN


def test_success_resets_failures() -> None:
    b = CircuitBreaker("p4", CircuitConfig(failure_threshold=3, cooldown_seconds=30))
    b.record_failure()
    b.record_failure()
    b.record_success()
    assert b.state is CircuitState.CLOSED
    assert b.snapshot()["consecutive_failures"] == 0


def test_registry_singleton_and_snapshot() -> None:
    reg = CircuitBreakerRegistry(CircuitConfig(failure_threshold=5, cooldown_seconds=30))
    assert reg.get("x") is reg.get("x")
    reg.get("x").record_failure()
    snap = reg.snapshot_all()
    assert any(s["name"] == "x" for s in snap)


def test_get_breaker_global() -> None:
    b = get_breaker("global-unique-provider-xyz")
    assert b.state is CircuitState.CLOSED


def test_circuit_open_error_carries_provider() -> None:
    err = CircuitOpenError("qwen", opened_at=123.0)
    assert err.provider == "qwen"
    assert "qwen" in str(err)
