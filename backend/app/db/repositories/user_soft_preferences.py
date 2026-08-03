"""用户软偏好仓储（仅向量召回 rerank，不进 prompt）。

按 (user_id, tag) 唯一键幂等 UPSERT：同一场景标签的软偏好只更新 content/weight。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories._base import BaseRepo
from app.models import UserSoftPreference


class UserSoftPreferenceRepo(BaseRepo[UserSoftPreference]):
    model = UserSoftPreference

    async def upsert_many(self, session: AsyncSession, items: list[dict]) -> list[UserSoftPreference]:
        out: list[UserSoftPreference] = []
        for it in items:
            existing = await self.get_by(session, user_id=it["user_id"], tag=it["tag"])
            if existing is not None:
                out.append(
                    await self.update(
                        session,
                        existing,
                        content=it["content"],
                        weight=it.get("weight", 50),
                    )
                )
            else:
                out.append(
                    await self.insert(
                        session,
                        user_id=it["user_id"],
                        tag=it["tag"],
                        content=it["content"],
                        weight=it.get("weight", 50),
                    )
                )
        return out

    async def list_for_user(self, session: AsyncSession, user_id: int) -> list[UserSoftPreference]:
        return await self.list(session, user_id=user_id)


__all__ = ["UserSoftPreferenceRepo"]
