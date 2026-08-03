"""长期语义记忆元数据仓储（MySQL 真相行）。

向量库只存 (source_type, source_id) + 标题；命中后经 source_id 回查本表取 summary（标题+正文）。
source_type == "message" 时 source_id 即本表 memories.id；其余情况 source_id 指向
project_events / user_soft_preferences 行（见 s1 回查路由）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories._base import BaseRepo
from app.models import Memory


class MemoryRepo(BaseRepo[Memory]):
    model = Memory

    async def insert_many(self, session: AsyncSession, rows: list[dict]) -> list[Memory]:
        out: list[Memory] = []
        for row in rows:
            out.append(await self.insert(session, **row))
        return out

    async def list_for_user(
        self, session: AsyncSession, user_id: int, *, limit: int = 100
    ) -> list[Memory]:
        return await self.list(session, limit=limit, user_id=user_id)

    async def list_for_project(
        self, session: AsyncSession, project_id: int, *, limit: int = 100
    ) -> list[Memory]:
        return await self.list(session, limit=limit, project_id=project_id)

    async def list_for_conversation(
        self, session: AsyncSession, conversation_id: int, *, limit: int = 100
    ) -> list[Memory]:
        return await self.list(session, limit=limit, conversation_id=conversation_id)

    async def mark_ready(self, session: AsyncSession, record_id: int) -> None:
        existing = await self.get(session, record_id)
        if existing is not None:
            await self.update(session, existing, embedding_status="ready")


__all__ = ["MemoryRepo"]
