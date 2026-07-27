"""§4 角色重构 · roles 包入口。

注意:RoleOrchestrator 不在此处急切导入(它由 core/queue.py 在执行时惰性 import),
以避免仅使用 handoff / RoleAgent 时也拉起 core.orchestrator 的重依赖链(httpx 等)。
"""

from __future__ import annotations

from .handoff import (
    ROLE_ORCHESTRATOR_ENABLED,
    ROLE_FOR_SKILL,
    ROLE_LABEL,
    ROLE_ORDER,
    ROLE_ARTIFACT,
    RoleHandoff,
    map_skill_to_role,
    build_handoff,
    build_upstream_context,
)
from .base import RoleAgent
from .product import ProductAgent
from .design import DesignAgent
from .dev import DevAgent
from .qa import QAAgent

__all__ = [
    "ROLE_ORCHESTRATOR_ENABLED",
    "ROLE_FOR_SKILL",
    "ROLE_LABEL",
    "ROLE_ORDER",
    "ROLE_ARTIFACT",
    "RoleHandoff",
    "map_skill_to_role",
    "build_handoff",
    "build_upstream_context",
    "RoleAgent",
    "ProductAgent",
    "DesignAgent",
    "DevAgent",
    "QAAgent",
]
