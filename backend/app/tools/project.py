"""项目生命周期原子工具（规范 §9.2 + §8.4）。

project_recycle（mid，可逆）/ project_purge（critical，双确认 + step-up）。当前直接
操作 Project 状态机；purge 的物理清理由异步 job 完成（不在单次 Tool 调用内同步完成）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import Domain, ErrorEnvelope, RiskLevel
from app.models.content import Project
from app.tools._registry import ToolMeta
from app.tools.base import BaseTool, ToolContext, ToolResult

import logging

logger = logging.getLogger("app.tools.project")

_PURGE_CONFIRM_KEY = "confirm_purge_project"


class ProjectRecycleTool(BaseTool):
    meta = ToolMeta(
        tool_id="project_recycle",
        risk=RiskLevel.MID,
        domain=Domain.PROJECT,
        description="项目进入回收站，可逆且审计；恢复走服务方法。",
        idempotency=True, reconcile_strategy="status_reversible", unknown_timeout_seconds=60,
        factory=lambda: ProjectRecycleTool(),
    )

    async def run(self, ctx: ToolContext, *, session: AsyncSession, project_id: int,
                  idempotency_key: str | None = None) -> ToolResult:
        """项目软删除至回收站(§8.4 project_recycle, mid, 可逆)。

        把 Project.status 置为 ``trashed``(软删,非物理删),可逆且审计；
        恢复走项目服务方法(不在本 Tool 内)。幂等：重复回收结果一致。

        Args:
            project_id: 目标项目 id。
        Returns:
            ``ToolResult.ok({project_id, status: trashed})``；找不到返回 failed。
        """
        logger.debug("[project_recycle] project=%s", project_id)
        project = await session.get(Project, project_id)
        if project is None:
            logger.warning("[project_recycle] 未找到 project=%s", project_id)
            return ToolResult.fail(ErrorEnvelope(
                code="project_recycle_not_found", category="not_found",
                what="找不到项目", why=f"project_id={project_id}",
                next="确认项目 ID", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key)
        project.status = "trashed"
        await session.flush()
        logger.info("[project_recycle] 已回收 project=%s -> trashed", project_id)
        return ToolResult.ok({"project_id": project.id, "status": "trashed"},
                              idempotency_key=idempotency_key or f"recycle:{project_id}")


class ProjectPurgeTool(BaseTool):
    meta = ToolMeta(
        tool_id="project_purge",
        risk=RiskLevel.CRITICAL,
        domain=Domain.PROJECT,
        description="永久删除，双确认与 step-up authentication。",
        max_input_bytes=512,
        requires_approval=True, idempotency=True,
        reconcile_strategy="purge_job", unknown_timeout_seconds=300,
        manual_resolution_policy="purge_runbook",
        factory=lambda: ProjectPurgeTool(),
    )

    async def run(self, ctx: ToolContext, *, session: AsyncSession, project_id: int,
                  confirmed: bool = False, idempotency_key: str | None = None) -> ToolResult:
        """永久删除(§8.4 project_purge, CRITICAL, 双确认 + step-up)。

        前置：必须由前端显式二次确认(``confirmed=True``)与 step-up 认证；
        否则直接 failed,绝不误删。此处仅把 Project.status 置为 ``purging``,
        **物理清理由异步 purge job 分步执行**,不在单次 Tool 调用内同步完成(避免长事务锁)。

        Args:
            project_id: 目标项目 id。
            confirmed: 是否已显式双确认。
        Returns:
            ``ToolResult.ok({project_id, status: purging, note})``；
            未确认/找不到返回 failed。
        """
        logger.debug("[project_purge] project=%s confirmed=%s", project_id, confirmed)
        if not confirmed:
            logger.warning("[project_purge] 未双确认,拒绝 project=%s", project_id)
            return ToolResult.fail(ErrorEnvelope(
                code="project_purge_requires_confirm", category="confirmation",
                what="永久删除需显式双确认", why="confirmed=False",
                next="前端需二次确认 + step-up 认证后重试", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key)
        project = await session.get(Project, project_id)
        if project is None:
            logger.warning("[project_purge] 未找到 project=%s", project_id)
            return ToolResult.fail(ErrorEnvelope(
                code="project_purge_not_found", category="not_found",
                what="找不到项目", why=f"project_id={project_id}",
                next="确认项目 ID", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key)
        # 物理清理由异步 purge job 分步执行；此处仅标记状态机进入 purging。
        project.status = "purging"
        await session.flush()
        logger.info("[project_purge] 已标记 project=%s -> purging(物理清理由异步 job 执行)", project_id)
        return ToolResult.ok({"project_id": project.id, "status": "purging",
                              "note": "物理清理由异步 purge job 执行"},
                             idempotency_key=idempotency_key or f"purge:{project_id}")


def tool_metas() -> list[ToolMeta]:
    return [t.meta for t in (ProjectRecycleTool(), ProjectPurgeTool())]
