"""项目强事实仓储（L2 零容错 KV 层）。

按 (project_id, category, key_name) 唯一键幂等 UPSERT。语义同 user_facts。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories._base import BaseRepo
from app.models import ProjectFact


class ProjectFactRepo(BaseRepo[ProjectFact]):
    model = ProjectFact

    async def upsert_many(self, session: AsyncSession, items: list[dict]) -> list[ProjectFact]:
        out: list[ProjectFact] = []
        for it in items:
            existing = await self.get_by(
                session,
                project_id=it["project_id"],
                category=it["category"],
                key_name=it["key_name"],
            )
            if existing is not None:
                out.append(
                    await self.update(
                        session,
                        existing,
                        value=it["value"],
                        source=it.get("source", "extracted"),
                    )
                )
            else:
                out.append(
                    await self.insert(
                        session,
                        project_id=it["project_id"],
                        category=it["category"],
                        key_name=it["key_name"],
                        value=it["value"],
                        source=it.get("source", "extracted"),
                    )
                )
        return out

    async def list_for_project(self, session: AsyncSession, project_id: int) -> list[ProjectFact]:
        return await self.list(session, project_id=project_id)


__all__ = ["ProjectFactRepo"]
