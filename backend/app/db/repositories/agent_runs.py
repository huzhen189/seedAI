from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.agent_runs")


class AgentRunsRepo(BaseRepo[AgentRun]):
    model = AgentRun

    async def running_for_conversation(
        self, session: AsyncSession, conversation_id: int
    ) -> list[AgentRun]:
        if conversation_id <= 0:
            raise ValueError("conversation_id 必须为正整数")
        return await self.list(
            session, conversation_id=conversation_id, status="running", limit=100
        )
