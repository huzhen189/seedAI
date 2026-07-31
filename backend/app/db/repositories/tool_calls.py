from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ToolCall

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.tool_calls")


class ToolCallsRepo(BaseRepo[ToolCall]):
    model = ToolCall

    async def by_message(
        self, session: AsyncSession, message_id: int, *, limit: int = 100
    ) -> list[ToolCall]:
        if message_id <= 0:
            raise ValueError("message_id 必须为正整数")
        return await self.list(session, message_id=message_id, limit=limit)

    async def by_idempotency_key(
        self, session: AsyncSession, idempotency_key: str
    ) -> ToolCall | None:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key 不得为空")
        return await self.get_by(session, idempotency_key=idempotency_key)
