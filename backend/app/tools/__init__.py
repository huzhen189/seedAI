"""原子工具包（规范 §9.2）。

``ToolRegistry`` 是工具的唯一注册表：所有 Skill 只能调用此处声明、且通过启动校验的
Tool；启动时校验风险、审批、幂等、沙箱、reconcile 与 Schema（§9.2 末段）。

所有 Tool 返回 ``ToolResult``，绝不抛裸异常；mid/high/critical 工具必须先写 W0
operation ledger 并使用稳定业务幂等键。
"""

from app.tools._registry import (
    ToolMeta,
    ToolRegistry,
    build_default_registry,
    get_registry,
)
from app.tools.base import (
    BaseTool,
    ToolContext,
    ToolResult,
    ToolStatus,
    make_failed,
    make_ok,
    make_unknown,
)
from app.tools.byok import ResolvedByok, resolve_byok

__all__ = [
    "ToolMeta",
    "ToolRegistry",
    "build_default_registry",
    "get_registry",
    "BaseTool",
    "ToolContext",
    "ToolResult",
    "ToolStatus",
    "make_failed",
    "make_ok",
    "make_unknown",
    "ResolvedByok",
    "resolve_byok",
]
