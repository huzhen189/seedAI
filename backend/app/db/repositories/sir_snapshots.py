from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SirSnapshot

from ._base import BaseRepo, RepositoryError


logger = logging.getLogger("app.db.repositories.sir_snapshots")


class SirSnapshotsRepo(BaseRepo[SirSnapshot]):
    model = SirSnapshot

    async def latest_for_conversation(
        self, session: AsyncSession, conversation_id: int
    ) -> SirSnapshot | None:
        if conversation_id <= 0:
            raise ValueError("conversation_id 必须为正整数")
        try:
            result = await session.execute(
                select(SirSnapshot)
                .where(SirSnapshot.conversation_id == conversation_id)
                .order_by(SirSnapshot.turn_no.desc(), SirSnapshot.id.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()
        except SQLAlchemyError as exc:
            logger.exception("读取最新 SIR 快照失败")
            raise RepositoryError("latest_for_conversation", "SirSnapshot", str(exc)) from exc
