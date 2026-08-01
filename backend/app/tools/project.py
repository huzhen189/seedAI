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
        project = await session.get(Project, project_id)
        if project is None:
            return ToolResult.fail(ErrorEnvelope(
                code="project_recycle_not_found", category="not_found",
                what="找不到项目", why=f"project_id={project_id}",
                next="确认项目 ID", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key)
        project.status = "trashed"
        await session.flush()
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
        if not confirmed:
            return ToolResult.fail(ErrorEnvelope(
                code="project_purge_requires_confirm", category="confirmation",
                what="永久删除需显式双确认", why="confirmed=False",
                next="前端需二次确认 + step-up 认证后重试", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key)
        project = await session.get(Project, project_id)
        if project is None:
            return ToolResult.fail(ErrorEnvelope(
                code="project_purge_not_found", category="not_found",
                what="找不到项目", why=f"project_id={project_id}",
                next="确认项目 ID", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key)
        # 物理清理由异步 purge job 分步执行；此处仅标记状态机进入 purging。
        project.status = "purging"
        await session.flush()
        return ToolResult.ok({"project_id": project.id, "status": "purging",
                              "note": "物理清理由异步 purge job 执行"},
                             idempotency_key=idempotency_key or f"purge:{project_id}")


def tool_metas() -> list[ToolMeta]:
    return [t.meta for t in (ProjectRecycleTool(), ProjectPurgeTool())]
