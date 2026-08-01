"""Site 域服务入口：编排 SiteWorkflow 的 Spec→Produce→Verify→Preview。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.turn_context import TurnContext
from app.domains.site.workflow import site_workflow
from app.models import Artifact, Project


class SiteService:
    async def create_or_edit(self, session: AsyncSession, context: TurnContext) -> tuple[Artifact, str]:
        project = await session.get(Project, context.session.project_id)
        if project is None or project.user_id != context.user.user_id:
            raise ValueError("目标项目不存在或无权访问")

        spec = await site_workflow.build_spec(session, project, context)
        html = site_workflow.produce(spec)

        ok, reason = site_workflow.verify(html)
        if not ok:
            # 规范 §8.2 Repair：确定性错误只做一次定向修复。
            html = site_workflow.produce({**spec, "_repair": True})
            ok, reason = site_workflow.verify(html)
            if not ok:
                raise ValueError(f"站点产物校验未通过：{reason}")

        artifact, text = await site_workflow.preview(session, project, context, html)
        return artifact, text


site_service = SiteService()
