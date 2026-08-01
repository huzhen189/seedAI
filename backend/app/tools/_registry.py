"""ToolRegistry：原子工具的唯一注册表（规范 §9.2）。

- 所有 Skill 只能调用此处声明且通过启动校验的 Tool。
- ``ToolMeta`` 携带 §9.2 末段要求声明的全部 profile：风险、沙箱、出口、文件系统、
  脱敏、大小上限、超时、重试、归属解析、幂等、审批、reconcile 与未知超时。
- ``validate_startup`` 在进程启动时强制校验，未通过则应用不得带着未声明/不合规的
  Tool 上线。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.contracts import Domain, RiskLevel
from app.tools.base import BaseTool


@dataclass
class ToolMeta:
    """单个原子工具的静态契约（启动后不可变）。"""

    tool_id: str
    risk: RiskLevel
    domain: Domain
    description: str
    # §9.2：ToolRegistry 必须声明的 7 个 profile
    sandbox_profile: str = "default"
    egress_profile: str = "none"
    filesystem_profile: str = "project_workspace"
    redaction_profile: str = "strict"
    max_input_bytes: int = 1_048_576
    max_output_bytes: int = 8_388_608
    timeout_seconds: int = 30
    # §9.2：有副作用 Tool 必须声明 reconcile 与 unknown 处理
    retry_policy: dict[str, Any] = field(default_factory=dict)
    owner_resolver: str = "project_owner"
    idempotency: bool = False
    requires_approval: bool = False
    reconcile_strategy: str = "none"
    unknown_timeout_seconds: int = 60
    manual_resolution_policy: str = "escalate"
    # 实现绑定（懒加载，避免 import 期触发业务模块）
    factory: "Callable[[], BaseTool] | None" = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolMeta] = {}

    def register(self, meta: ToolMeta) -> None:
        if meta.tool_id in self._tools:
            raise ValueError(f"tool {meta.tool_id!r} 已注册")
        self._tools[meta.tool_id] = meta

    def get(self, tool_id: str) -> ToolMeta:
        meta = self._tools.get(tool_id)
        if meta is None:
            raise KeyError(f"未注册的 tool: {tool_id!r}")
        return meta

    def build(self, tool_id: str) -> BaseTool:
        meta = self.get(tool_id)
        if meta.factory is None:
            raise RuntimeError(f"tool {tool_id!r} 未绑定实现(factory)")
        return meta.factory()

    def all(self) -> list[ToolMeta]:
        return list(self._tools.values())

    def validate_startup(self) -> list[str]:
        """返回违规清单；空列表表示所有已注册 Tool 合规（§9.2 启动校验）。"""
        errors: list[str] = []
        valid_risks = {RiskLevel.LOW, RiskLevel.MID, RiskLevel.HIGH, RiskLevel.CRITICAL}
        for meta in self._tools.values():
            if meta.risk not in valid_risks:
                errors.append(f"{meta.tool_id}: 无效 risk={meta.risk}")
            if meta.risk in (RiskLevel.HIGH, RiskLevel.CRITICAL) and not meta.requires_approval:
                errors.append(f"{meta.tool_id}: {meta.risk.value} 工具必须 requires_approval=True")
            if meta.risk in (RiskLevel.MID, RiskLevel.HIGH, RiskLevel.CRITICAL) and not meta.idempotency:
                errors.append(f"{meta.tool_id}: {meta.risk.value} 工具必须 idempotency=True")
            if meta.reconcile_strategy == "none" and meta.risk in (
                RiskLevel.MID, RiskLevel.HIGH, RiskLevel.CRITICAL
            ):
                errors.append(f"{meta.tool_id}: 有副作用工具必须声明 reconcile_strategy")
            if not meta.factory:
                errors.append(f"{meta.tool_id}: 未绑定实现(factory 为 None)")
        return errors


# 进程内单例；由 app 启动期调用 build_default_registry() 填充。
_REGISTRY = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _REGISTRY


def build_default_registry() -> ToolRegistry:
    """注册规范 §9.2 的全部 16 个原子 Tool。重复调用安全（先清空）。"""
    from app.tools import memory, project, research, site  # 懒加载工具实现

    _REGISTRY._tools.clear()

    for meta in (
        site.tool_metas()
        + research.tool_metas()
        + memory.tool_metas()
        + project.tool_metas()
    ):
        _REGISTRY.register(meta)
    return _REGISTRY
