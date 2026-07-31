from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, QcScore

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.qc_scores")


class QcScoresRepo(BaseRepo[QcScore]):
    model = QcScore

    async def by_conversation(
        self, session: AsyncSession, conversation_id: int
    ) -> list[QcScore]:
        if conversation_id <= 0:
            raise ValueError("conversation_id 必须为正整数")
        return await self.list(session, conversation_id=conversation_id, limit=1000)

    async def by_user_and_dimension(
        self, session: AsyncSession, user_id: int, dimension: str, *, limit: int = 100
    ) -> list[QcScore]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(
            session, user_id=user_id, dimension=dimension, limit=limit
        )

    async def get_by_trace(self, session: AsyncSession, trace_id: str) -> QcScore | None:
        if not trace_id.strip():
            raise ValueError("trace_id 不得为空")
        return await self.get_by(session, trace_id=trace_id, sub_task_id=None)

    async def get_by_trace_sub(
        self, session: AsyncSession, trace_id: str, sub_task_id: str | None
    ) -> QcScore | None:
        if not trace_id.strip():
            raise ValueError("trace_id 不得为空")
        return await self.get_by(session, trace_id=trace_id, sub_task_id=sub_task_id)

    async def upsert(
        self,
        session: AsyncSession,
        trace_id: str,
        model_id: str | None,
        conversation_id: int | None,
        result: dict[str, object],
        sub_task_id: str | None = None,
    ) -> QcScore:
        if not trace_id.strip():
            raise ValueError("trace_id 不得为空")
        raw_overall = result.get("overall", 0.0)
        if not isinstance(raw_overall, (int, float, str)):
            raise ValueError("QC overall 必须是数值")
        try:
            overall = float(raw_overall)
        except ValueError as exc:
            raise ValueError("QC overall 必须是数值") from exc
        score = round(overall * 10 if overall <= 10 else overall)
        score = max(0, min(100, score))
        user_id = 0
        if conversation_id is not None:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise LookupError(f"会话 id={conversation_id} 不存在")
            user_id = conversation.user_id
        values: dict[str, Any] = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "dimension": "overall",
            "score": score,
            "model_used": model_id,
            "auto": True,
            "trace_id": trace_id,
            "sub_task_id": sub_task_id,
            "model_id": model_id,
            "overall": overall,
            "result": result,
            "needs_review": bool(result.get("needs_review", False)),
            "safety_risk": str(result.get("safety_risk", "low")),
            "partial": bool(result.get("partial", False)),
        }
        existing = await self.get_by_trace_sub(session, trace_id, sub_task_id)
        if existing is not None:
            return await self.update(session, existing, **values)
        return await self.create(session, **values)


qc_score_repo = QcScoresRepo()
