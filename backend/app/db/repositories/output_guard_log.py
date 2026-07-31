from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutputGuardLog

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.output_guard_log")


class OutputGuardLogRepo(BaseRepo[OutputGuardLog]):
    model = OutputGuardLog

    async def by_decision(
        self, session: AsyncSession, decision: str, *, limit: int = 100
    ) -> list[OutputGuardLog]:
        if not decision.strip():
            raise ValueError("decision 不得为空")
        return await self.list(session, decision=decision, limit=limit)
