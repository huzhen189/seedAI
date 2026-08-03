"""项目过程事件仓储（审计/过程记忆，不进 prompt）。

事件原文落库；经异步摘要入 memories(kind=proj_summary) 后间接进 L5 向量召回。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories._base import BaseRepo
from app.models import ProjectEvent


class ProjectEventRepo(BaseRepo[ProjectEvent]):
    model = ProjectEvent

    async def insert_event(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        kind: str,
        detail: str,
        conversation_id: int | None = None,
        source_message_id: int | None = None,
        payload: dict | None = None,
    ) -> ProjectEvent:
        return await self.insert(
            session,
            project_id=project_id,
            conversation_id=conversation_id,
            kind=kind,
            detail=detail,
            payload=payload or {},
            source_message_id=source_message_id,
        )

    async def list_for_project(
        self, session: AsyncSession, project_id: int, *, limit: int = 100
    ) -> list[ProjectEvent]:
        return await self.list(session, limit=limit, project_id=project_id)

    async def mark_ready(self, session: AsyncSession, record_id: int) -> None:
        existing = await self.get(session, record_id)
        if existing is not None:
            await self.update(session, existing, embedding_status="ready")


__all__ = ["ProjectEventRepo"]
