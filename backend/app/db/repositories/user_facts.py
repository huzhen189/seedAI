"""用户强事实仓储（L2 零容错 KV 层）。

按 (user_id, category, key_name) 唯一键幂等 UPSERT：同一事实反复提及只更新 value，
行数受"不同事实种类数"约束，不随发言次数膨胀（防爆关键，见方案 §8.2）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories._base import BaseRepo
from app.models import UserFact


class UserFactRepo(BaseRepo[UserFact]):
    model = UserFact

    async def upsert_many(self, session: AsyncSession, items: list[dict]) -> list[UserFact]:
        """幂等写入多条用户强事实。items 元素字段：
        user_id, category, key_name, value, source?, confidence?
        """
        out: list[UserFact] = []
        for it in items:
            existing = await self.get_by(
                session,
                user_id=it["user_id"],
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
                        confidence=it.get("confidence", 90),
                    )
                )
            else:
                out.append(
                    await self.insert(
                        session,
                        user_id=it["user_id"],
                        category=it["category"],
                        key_name=it["key_name"],
                        value=it["value"],
                        source=it.get("source", "extracted"),
                        confidence=it.get("confidence", 90),
                    )
                )
        return out

    async def list_for_user(self, session: AsyncSession, user_id: int) -> list[UserFact]:
        return await self.list(session, user_id=user_id)


__all__ = ["UserFactRepo"]
