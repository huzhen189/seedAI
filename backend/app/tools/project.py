"""项目生命周期原子工具（规范 §9.2 + §8.4）。

project_recycle（mid，可逆）/ project_purge（critical，双确认 + step-up）。当前直接
操作 Project 状态机；purge 的物理清理由异步 job 完成（不在单次 Tool 调用内同步完成）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import Domain, ErrorEnvelope, RiskLevel
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

        委托 ``project_ops.execute(action="trash")`` 权威内核——单一实现、统一归属校验
        （``user_id`` 必须匹配项目 owner，杜绝越权回收）、统一 W0 账本与 operation_key 语义；
        工具本身只做薄封装 + 治理层（幂等键），不重复实现状态机。
        恢复走项目服务方法(不在本 Tool 内)。幂等：重复回收结果一致。

        Args:
            project_id: 目标项目 id（必须是 ctx.user_id 拥有的项目）。
        Returns:
            ``ToolResult.ok({project_id, status: trashed})``；不存在/越权/状态非法返回 failed。
        """
        logger.debug("[project_recycle] project=%s user=%s", project_id, ctx.user_id)
        from app.domains.project.ops import project_ops

        outcome = await project_ops.execute(
            session, action="trash", project_id=project_id,
            user_id=ctx.user_id, trace_id=ctx.trace_id or "",
        )
        if outcome.status == "succeeded":
            logger.info("[project_recycle] 已回收 project=%s -> trashed", project_id)
            return ToolResult.ok(
                {"project_id": project_id, "status": "trashed"},
                idempotency_key=idempotency_key or f"recycle:{project_id}",
                metrics={"ops": outcome.details},
            )
        logger.warning("[project_recycle] 失败 project=%s code=%s", project_id, outcome.error_code)
        return ToolResult.fail(
            ErrorEnvelope(
                code=outcome.error_code or "project_recycle_failed", category="project",
                what=outcome.text, why=outcome.error_code or "",
                next="确认项目状态与归属", retryable=False, retry_scope="none"),
            idempotency_key=idempotency_key)


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

        前置：必须由前端显式二次确认(``confirmed=True``)与 step-up 认证；否则直接 failed，
        绝不误删。确认后委托 ``project_ops.execute(action="purge")`` 权威内核——
        只做「冻结 + 建 job」(``project_ops.purge`` 内部 CAS trashed→purging 并校验状态机)，
        **物理清理由异步 purge job 分步执行**,不在单次 Tool 调用内同步完成(避免长事务锁)。

        ⚠️ 与 gate 路径 ``_execute_approved_action`` 共用同一 ``project_ops`` 内核：
        审批端点已通过两步审批(等效 confirmed),直接调 ``project_ops.execute``；
        而经本 Tool 入场时仍需显式 ``confirmed``(前端双确认) + ``call_tool`` 审批闸门
        (``requires_approval=True``) 双重门禁,保证无论入口如何都收敛到同一实现与账本语义。

        Args:
            project_id: 目标项目 id（必须是 ctx.user_id 拥有的项目）。
            confirmed: 是否已显式双确认。
        Returns:
            ``ToolResult.ok({project_id, status: purging, note})``；
            未确认/不存在/未先入回收站返回 failed。
        """
        logger.debug("[project_purge] project=%s user=%s confirmed=%s", project_id, ctx.user_id, confirmed)
        if not confirmed:
            logger.warning("[project_purge] 未双确认,拒绝 project=%s", project_id)
            return ToolResult.fail(ErrorEnvelope(
                code="project_purge_requires_confirm", category="confirmation",
                what="永久删除需显式双确认", why="confirmed=False",
                next="前端需二次确认 + step-up 认证后重试", retryable=False, retry_scope="none"),
                idempotency_key=idempotency_key)
        from app.domains.project.ops import project_ops

        outcome = await project_ops.execute(
            session, action="purge", project_id=project_id,
            user_id=ctx.user_id, trace_id=ctx.trace_id or "",
        )
        if outcome.status == "succeeded":
            logger.info("[project_purge] 已标记 project=%s -> purging(物理清理由异步 job 执行)", project_id)
            return ToolResult.ok(
                {"project_id": project_id, "status": "purging", "note": "物理清理由异步 purge job 执行"},
                idempotency_key=idempotency_key or f"purge:{project_id}",
                metrics=outcome.details)
        logger.warning("[project_purge] 失败 project=%s code=%s", project_id, outcome.error_code)
        return ToolResult.fail(
            ErrorEnvelope(
                code=outcome.error_code or "project_purge_failed", category="project",
                what=outcome.text, why=outcome.error_code or "",
                next="确认项目已先入回收站", retryable=False, retry_scope="none"),
            idempotency_key=idempotency_key)


def tool_metas() -> list[ToolMeta]:
    return [t.meta for t in (ProjectRecycleTool(), ProjectPurgeTool())]
