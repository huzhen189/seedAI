"""记忆类原子工具（规范 §9.2）。

mem_recall / mem_store 当前**无记忆后端**可用（``app/memory`` 为空）：契约已登记，
运行时在无后端时明确返回 failed——mem_recall 返回 unavailable，mem_store 返回
"需 Storage Gate 决策且后端待接"，绝不静默写入或伪造记忆。
"""

from __future__ import annotations

from typing import Any

from app.core.contracts import Domain, ErrorEnvelope, RiskLevel
from app.tools._registry import ToolMeta
from app.tools.base import BaseTool, ToolContext, ToolResult

import logging

logger = logging.getLogger("app.tools.memory")

_UNAVAILABLE = "backend_unavailable"


class MemRecallTool(BaseTool):
    meta = ToolMeta(
        tool_id="mem_recall",
        risk=RiskLevel.LOW,
        domain=Domain.CHAT,
        description="结构化记忆读取。", sandbox_profile="read_only",
        idempotency=False, reconcile_strategy="none", factory=lambda: MemRecallTool(),
    )

    async def run(self, ctx: ToolContext, *, scope: str, key: str) -> ToolResult:
        """结构化记忆读取(§9.2 mem_recall, low)。

        本环境 memory 层未初始化,返回 failed(不静默)。投产时从记忆后端按
        (scope, key) 读取用户级/项目级结构化记忆。
        """
        logger.debug("[mem_recall] scope=%s key=%s (memory 未初始化,返回 unavailable)", scope, key)
        return ToolResult.fail(ErrorEnvelope(
            code=_UNAVAILABLE, category="memory",
            what="mem_recall 暂无可读取的记忆后端", why="memory 层未初始化",
            next="待记忆后端就绪后可用", retryable=False, retry_scope="none"))


class MemStoreTool(BaseTool):
    meta = ToolMeta(
        tool_id="mem_store",
        risk=RiskLevel.MID,
        domain=Domain.CHAT,
        description="仅接受 Storage Gate 决策后的数据。", sandbox_profile="default",
        idempotency=True, reconcile_strategy="storage_gate", unknown_timeout_seconds=30,
        factory=lambda: MemStoreTool(),
    )

    async def run(self, ctx: ToolContext, *, scope: str, key: str, value: Any) -> ToolResult:
        """结构化记忆写入(§9.2 mem_store, mid)。

        本环境无 Storage Gate 决策且后端未初始化,**禁止直接落库**,返回 failed(不静默)。
        投产时仅接受经过 Storage Gate 决策(脱敏/审批)后的数据写入记忆后端。
        """
        logger.debug("[mem_store] scope=%s key=%s (Storage Gate + 后端均待接,返回 unavailable)", scope, key)
        return ToolResult.fail(ErrorEnvelope(
            code=_UNAVAILABLE, category="memory",
            what="mem_store 需 Storage Gate 决策且后端待接",
            why="memory 层未初始化，禁止直接落库",
            next="待记忆后端 + Storage Gate 就绪后可用", retryable=False, retry_scope="none"))


def tool_metas() -> list[ToolMeta]:
    return [t.meta for t in (MemRecallTool(), MemStoreTool())]
