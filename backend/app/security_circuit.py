"""Provider 调用熔断器（§15.3 优雅停机与熔断）。

设计约束：
- 每个 provider 一个独立状态机：closed → open → half_open → closed。
- 连续超时/5xx 达到阈值后熔断（open）；cooldown 后进入 half_open 试一次；
  成功则闭合，失败则重新 open。
- 熔断只做"跳过该 provider"，保留上层故障转移（如 qwen→deepseek）；
  当全部 provider 电路 open 时抛 `CircuitOpenError`（结构化错误），
  绝不静默切换到平台付费 Key（符合 §14.3）。
- 计数器用 threading.Lock 保护，sync/async 调用均安全。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Final

logger = logging.getLogger("app.security.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """所有 provider 电路均 open，无法发起调用。"""

    def __init__(self, provider: str, opened_at: float | None = None) -> None:
        self.provider = provider
        self.opened_at = opened_at
        super().__init__(
            f"provider {provider} 熔断中，暂时拒绝调用"
            + (f"（自 {opened_at:.0f}）" if opened_at else "")
        )


@dataclass
class CircuitConfig:
    failure_threshold: int = 5          # 连续失败达到即 open
    cooldown_seconds: float = 30.0      # open 后多久进入 half_open
    half_open_max_calls: int = 1        # half_open 允许试探次数


class CircuitBreaker:
    """单 provider 熔断器。"""

    def __init__(self, name: str, config: CircuitConfig | None = None) -> None:
        self.name = name
        self.config = config or CircuitConfig()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0
        self._total_failures = 0
        self._total_success = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "opened_at": self._opened_at,
                "half_open_calls": self._half_open_calls,
                "total_failures": self._total_failures,
                "total_success": self._total_success,
            }

    def allow(self) -> bool:
        """是否允许本次调用（线程安全）。"""
        with self._lock:
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.OPEN:
                if (
                    self._opened_at is not None
                    and (time.monotonic() - self._opened_at) >= self.config.cooldown_seconds
                ):
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info("熔断器 %s 进入 half_open", self.name)
                else:
                    return False
            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_calls < self.config.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._total_success += 1
            self._consecutive_failures = 0
            if self._state is not CircuitState.CLOSED:
                logger.info("熔断器 %s 恢复 closed", self.name)
            self._state = CircuitState.CLOSED
            self._opened_at = None
            self._half_open_calls = 0

    def record_failure(self, *_: object, **__: object) -> None:
        with self._lock:
            self._total_failures += 1
            self._consecutive_failures += 1
            if self._state is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning("熔断器 %s half_open 试探失败，重新 open", self.name)
                return
            if (
                self._state is CircuitState.CLOSED
                and self._consecutive_failures >= self.config.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "熔断器 %s 触发 open（连续失败 %d）",
                    self.name,
                    self._consecutive_failures,
                )


class CircuitBreakerRegistry:
    """按名称管理熔断器（线程安全单例集合）。"""

    def __init__(self, default_config: CircuitConfig | None = None) -> None:
        self._default_config = default_config or CircuitConfig()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            br = self._breakers.get(name)
            if br is None:
                br = CircuitBreaker(name, CircuitConfig(**asdict(self._default_config)))
                self._breakers[name] = br
            return br

    def snapshot_all(self) -> list[dict]:
        with self._lock:
            return [br.snapshot() for br in self._breakers.values()]

    def all_open(self, names: list[str]) -> bool:
        """给定 provider 列表是否全部 open（用于决定是否抛 CircuitOpenError）。"""
        if not names:
            return False
        with self._lock:
            selected = [self._breakers[n] for n in names if n in self._breakers]
        if not selected:
            return False
        return all(b.state is CircuitState.OPEN for b in selected)


# 全局默认注册表：失败阈值 5、cooldown 30s、half_open 试 1 次（规范 §15.3 标准做法）。
_default_registry = CircuitBreakerRegistry(
    CircuitConfig(failure_threshold=5, cooldown_seconds=30.0, half_open_max_calls=1)
)


def get_breaker(name: str) -> CircuitBreaker:
    return _default_registry.get(name)


def get_registry() -> CircuitBreakerRegistry:
    return _default_registry


__all__: Final[list[str]] = [
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitConfig",
    "CircuitOpenError",
    "CircuitState",
    "get_breaker",
    "get_registry",
]
