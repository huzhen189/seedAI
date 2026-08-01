"""Site 域服务入口：编排 SiteWorkflow 的 Spec→Produce→Verify→Preview。"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.turn_context import TurnContext
from app.domains.site.workflow import site_workflow
from app.models import Artifact, Project

logger = logging.getLogger("app.domains.site.service")


class SiteService:
    async def create_or_edit(self, session: AsyncSession, context: TurnContext) -> tuple[Artifact, str]:
        """建站/改站主入口(S6 调用)。

        流程：取项目 → build_spec(合并需求) → produce(生成 HTML) → verify(校验/最多一次修复)
        → preview(落不可变 Artifact)。任一前置失败(项目不存在/越权)直接抛错,由上层转失败响应。

        Args:
            session: 数据库会话(本方法内所有写操作在同一事务,提交由调用方控制)。
            context: 本轮 TurnContext(含 project_id / user_id)。
        Returns:
            ``(Artifact, 文本摘要)``。
        """
        project = await session.get(Project, context.session.project_id)
        if project is None or project.user_id != context.user.user_id:
            logger.warning("[site] 项目不存在或越权 project=%s user=%s", context.session.project_id, context.user.user_id)
            raise ValueError("目标项目不存在或无权访问")

        logger.info("[site] 开始建站 project=%s turn=%s", project.id, context.turn_id)
        spec = await site_workflow.build_spec(session, project, context)
        html = site_workflow.produce(spec)

        ok, reason = site_workflow.verify(html)
        if not ok:
            # 规范 §8.2 Repair：确定性错误只做一次定向修复。
            logger.warning("[site] 首轮校验未过 reason=%s,尝试定向修复", reason)
            html = site_workflow.produce({**spec, "_repair": True})
            ok, reason = site_workflow.verify(html)
            if not ok:
                logger.error("[site] 定向修复后仍校验失败 reason=%s", reason)
                raise ValueError(f"站点产物校验未通过：{reason}")

        artifact, text = await site_workflow.preview(session, project, context, html)
        logger.info("[site] 建站完成 project=%s artifact=%s", project.id, artifact.id)
        return artifact, text


site_service = SiteService()


site_service = SiteService()
