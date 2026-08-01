"""原子工具基础契约（规范 §5.6 / §9.2）。

所有 Tool 返回 ``ToolResult``，绝不抛裸异常。``ToolContext`` 在执行时由 S6 执行器
注入；``BYOK`` 解析结果挂在其 ``.byok`` 字段，供需要自定义 provider 的 Tool 在单次
调用作用域内使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.contracts import ErrorEnvelope, SCHEMA_VERSION


class ToolStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class ToolResult:
    schema_version: str = SCHEMA_VERSION
    status: ToolStatus = ToolStatus.SUCCEEDED
    data: dict[str, Any] = field(default_factory=dict)
    error: ErrorEnvelope | None = None
    idempotency_key: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, data: dict[str, Any] | None = None, *, idempotency_key: str | None = None,
           metrics: dict[str, Any] | None = None) -> "ToolResult":
        return cls(
            data=data or {},
            idempotency_key=idempotency_key,
            metrics=metrics or {},
        )

    @classmethod
    def fail(cls, error: ErrorEnvelope, *, idempotency_key: str | None = None,
             metrics: dict[str, Any] | None = None) -> "ToolResult":
        return cls(
            status=ToolStatus.FAILED,
            error=error,
            idempotency_key=idempotency_key,
            metrics=metrics or {},
        )

    @classmethod
    def unknown(cls, error: ErrorEnvelope, *, idempotency_key: str | None = None,
                metrics: dict[str, Any] | None = None) -> "ToolResult":
        return cls(
            status=ToolStatus.UNKNOWN,
            error=error,
            idempotency_key=idempotency_key,
            metrics=metrics or {},
        )


@dataclass
class ToolContext:
    """单次 Tool 调用的执行上下文（由 S6 执行器构造）。"""

    user_id: int
    project_id: int | None = None
    conversation_id: int | None = None
    trace_id: str | None = None
    # BYOK 解析结果：仅当该 Tool 需要自定义 provider 时由执行器填充（单次调用作用域）。
    byok: "ResolvedByok | None" = None
    extra: dict[str, Any] = field(default_factory=dict)


class BaseTool:
    """原子工具基类。子类必须实现 ``meta`` 与 ``run``。"""

    meta: "ToolMeta"  # 由子类赋值

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} {getattr(self.meta, 'tool_id', '?')}>"


def make_ok(data: dict[str, Any] | None = None, *, idempotency_key: str | None = None,
            metrics: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult.ok(data, idempotency_key=idempotency_key, metrics=metrics)


def make_failed(error: ErrorEnvelope, *, idempotency_key: str | None = None,
                metrics: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult.fail(error, idempotency_key=idempotency_key, metrics=metrics)


def make_unknown(error: ErrorEnvelope, *, idempotency_key: str | None = None,
                 metrics: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult.unknown(error, idempotency_key=idempotency_key, metrics=metrics)
