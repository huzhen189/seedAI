"""Trace / TraceEvent / Feedback / UsageLog Repository(不缓存, 仅 SQL 封装)。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Feedback, QcScore, Trace, TraceEvent, UsageLog
from .base import BaseRepo

logger = logging.getLogger("business.repo")


class TraceRepo(BaseRepo[Trace]):
    model = Trace
    cache_prefix = "trace"
    cache_ttl = 0
    cache_enabled = False

    async def get_by_trace_id(self, db: AsyncSession, trace_id: str) -> Optional[Trace]:
        return await self.get_by(db, trace_id=trace_id)

    async def finish(self, db: AsyncSession, trace: Trace, status: str, total_tokens: int = 0) -> Trace:
        return await self.update(db, trace, status=status, total_tokens=total_tokens, finished_at=datetime.utcnow())


class TraceEventRepo(BaseRepo[TraceEvent]):
    model = TraceEvent
    cache_prefix = "trace_evt"
    cache_ttl = 0
    cache_enabled = False

    async def list_by_trace(self, db: AsyncSession, trace_id: str) -> list[TraceEvent]:
        stmt = (
            select(TraceEvent)
            .where(TraceEvent.trace_id == trace_id)
            .order_by(TraceEvent.seq.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


class FeedbackRepo(BaseRepo[Feedback]):
    model = Feedback
    cache_prefix = "fb"
    cache_ttl = 3000
    cache_enabled = True

    async def get_by_trace(self, db: AsyncSession, trace_id: str) -> Optional[Feedback]:
        return await self.get_by(db, trace_id=trace_id)

    async def upsert(self, db: AsyncSession, user_id: int, trace_id: str,
                      conv_id: int | None, rating: int, comment: str | None = None,
                      dimensions: dict | None = None) -> Feedback:
        existing = await self.get_by_trace(db, trace_id)
        if existing:
            return await self.update(
                db, existing, rating=rating, comment=comment, dimensions=dimensions)
        return await self.create(
            db, user_id=user_id, trace_id=trace_id,
            conversation_id=conv_id, rating=rating, comment=comment, dimensions=dimensions,
        )


class QcScoreRepo(BaseRepo[QcScore]):
    model = QcScore
    cache_prefix = "qc"
    cache_ttl = 0
    cache_enabled = False

    async def get_by_trace(self, db: AsyncSession, trace_id: str) -> Optional[QcScore]:
        return await self.get_by(db, trace_id=trace_id)

    async def upsert(self, db: AsyncSession, trace_id: str, model_id: str | None,
                     conversation_id: int | None, result: dict) -> QcScore:
        existing = await self.get_by_trace(db, trace_id)
        if existing:
            return await self.update(
                db, existing,
                model_id=model_id,
                conversation_id=conversation_id,
                overall=result.get("overall", 0.0),
                result=result,
                needs_review=bool(result.get("needs_review", False)),
                safety_risk=result.get("safety_risk", "low"),
                partial=bool(result.get("partial", False)),
            )
        return await self.create(
            db,
            trace_id=trace_id,
            conversation_id=conversation_id,
            model_id=model_id,
            overall=result.get("overall", 0.0),
            result=result,
            needs_review=bool(result.get("needs_review", False)),
            safety_risk=result.get("safety_risk", "low"),
            partial=bool(result.get("partial", False)),
        )


class UsageLogRepo(BaseRepo[UsageLog]):
    model = UsageLog
    cache_prefix = "ulog"
    cache_ttl = 0
    cache_enabled = False


# 模块级单例
trace_repo = TraceRepo()
trace_event_repo = TraceEventRepo()
feedback_repo = FeedbackRepo()
qc_score_repo = QcScoreRepo()
usage_log_repo = UsageLogRepo()
