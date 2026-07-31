from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SessionAudit

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.session_audits")


class SessionAuditsRepo(BaseRepo[SessionAudit]):
    model = SessionAudit

    async def by_turn(
        self, session: AsyncSession, conversation_id: int, turn_no: int
    ) -> list[SessionAudit]:
        if conversation_id <= 0 or turn_no < 0:
            raise ValueError("conversation_id 必须为正整数且 turn_no 不得为负")
        return await self.list(
            session, conversation_id=conversation_id, turn_no=turn_no, limit=1000
        )
