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

import logging

logger = logging.getLogger("app.tools.registry")


@dataclass(frozen=True)
class ToolMeta:
    """单个原子工具的静态契约（**冻结 dataclass，运行期真不可变**）。

    ⚠️ 为什么必须 ``frozen=True``：``meta`` 是各 Tool 类的**类属性**，
    ``registry.build()`` 每次 new 出来的实例共享同一个 ToolMeta 对象。
    只要有一处写了 ``tool.meta.timeout_seconds = 5``，就会全局污染所有调用方
    （包括 ``call_tool`` 的超时/重试/审批判定）。此前只在 docstring 里写了
    「启动后不可变」但无任何约束，现由 dataclass 在赋值时直接抛
    ``FrozenInstanceError``，把约定升级成硬保证。

    §9.2 末段要求 ToolRegistry 必须声明全部 profile,这里用 dataclass 字段承载：
      - 基础身份: tool_id / risk / domain / description；
      - 7 个 profile: sandbox_profile(沙箱)/ egress_profile(出口)/
        filesystem_profile(文件)/ redaction_profile(脱敏)/ max_input_bytes/
        max_output_bytes / timeout_seconds；
      - 副作用治理: retry_policy / owner_resolver / idempotency(幂等)/
        requires_approval(审批)/ reconcile_strategy(对账)/ unknown_timeout_seconds/
        manual_resolution_policy(人工处置)；
      - factory: 实现绑定(懒加载,避免 import 期触发业务模块)。
    """

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
        """注册一个 ToolMeta。重复 tool_id 直接抛错(注册期容错)。"""
        if meta.tool_id in self._tools:
            raise ValueError(f"tool {meta.tool_id!r} 已注册")
        self._tools[meta.tool_id] = meta
        logger.debug("[registry] 注册 tool=%s risk=%s domain=%s", meta.tool_id, meta.risk.value, meta.domain.value)

    def get(self, tool_id: str) -> ToolMeta:
        meta = self._tools.get(tool_id)
        if meta is None:
            raise KeyError(f"未注册的 tool: {tool_id!r}")
        return meta

    def build(self, tool_id: str) -> BaseTool:
        """按 tool_id 构造一个工具实例(调用 factory,惰性触达业务模块)。"""
        meta = self.get(tool_id)
        if meta.factory is None:
            raise RuntimeError(f"tool {tool_id!r} 未绑定实现(factory)")
        return meta.factory()

    def all(self) -> list[ToolMeta]:
        return list(self._tools.values())

    def validate_startup(self) -> list[str]:
        """返回违规清单；空列表表示所有已注册 Tool 合规（§9.2 启动校验）。

        校验规则(任一命中即记一条违规)：
          1. risk 必须是 LOW/MID/HIGH/CRITICAL 之一；
          2. HIGH/CRITICAL 必须 requires_approval=True；
          3. MID/HIGH/CRITICAL 必须 idempotency=True；
          4. 有副作用(MID/HIGH/CRITICAL)必须声明 reconcile_strategy；
          5. 必须绑定实现(factory 非 None)。
        """
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
        if errors:
            logger.error("[registry] 启动校验发现 %d 处违规: %s", len(errors), errors)
        else:
            logger.info("[registry] 启动校验通过: 共 %d 个 Tool 合规", len(self._tools))
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
