from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FlowCheck

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.flow_checks")


class FlowChecksRepo(BaseRepo[FlowCheck]):
    model = FlowCheck

    async def failed_for_user(
        self, session: AsyncSession, user_id: int, *, limit: int = 100
    ) -> list[FlowCheck]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(session, user_id=user_id, passed=False, limit=limit)
