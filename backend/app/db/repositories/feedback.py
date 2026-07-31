from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Feedback, Message

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.feedback")


class FeedbackRepo(BaseRepo[Feedback]):
    model = Feedback

    async def by_message(self, session: AsyncSession, message_id: int) -> Feedback | None:
        if message_id <= 0:
            raise ValueError("message_id 必须为正整数")
        return await self.get_by(session, message_id=message_id)

    async def by_user(
        self, session: AsyncSession, user_id: int, *, limit: int = 100
    ) -> list[Feedback]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(session, user_id=user_id, limit=limit)

    async def get_by_trace(self, session: AsyncSession, trace_id: str) -> Feedback | None:
        if not trace_id.strip():
            raise ValueError("trace_id 不得为空")
        return await self.get_by(session, trace_id=trace_id)

    async def upsert(
        self,
        session: AsyncSession,
        user_id: int,
        trace_id: str,
        conv_id: int | None,
        rating: int,
        comment: str | None = None,
        dimensions: dict[str, object] | None = None,
    ) -> Feedback:
        if user_id <= 0 or not trace_id.strip():
            raise ValueError("user_id 和 trace_id 必须有效")
        if not 1 <= rating <= 10:
            raise ValueError("rating 必须在 1..10 之间")
        existing = await self.get_by_trace(session, trace_id)
        if existing is not None:
            return await self.update(
                session,
                existing,
                rating=rating,
                comment=comment,
                dimensions=dimensions,
            )
        result = await session.execute(
            select(Message)
            .where(Message.trace_id == trace_id, Message.role == "assistant")
            .order_by(Message.id.desc())
            .limit(1)
        )
        message = result.scalar_one_or_none()
        if message is None:
            raise LookupError("未找到该 trace 的 assistant 消息，无法建立 feedback 外键")
        conversation_id = conv_id or message.conversation_id
        if conversation_id != message.conversation_id:
            raise ValueError("conv_id 与 trace 对应消息不一致")
        return await self.create(
            session,
            user_id=user_id,
            trace_id=trace_id,
            conversation_id=conversation_id,
            message_id=message.id,
            rating=rating,
            comment=comment,
            dimensions=dimensions,
        )


feedback_repo = FeedbackRepo()
