"""对话追踪与回放(③-a):Trace / TraceEvent 落库(走 Repository 层)。

设计:一次 SSE 会话 = 一个 Trace,结构化事件按 seq 追加 TraceEvent。
token 事件不逐条落库,仅记录聚合 token 数;结构化事件逐条落库。
所有写操作均吞异常,绝不阻断主生成流。
"""

import json
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from .models import Feedback, TraceEvent, UsageLog
from .repos.trace_repos import feedback_repo, trace_event_repo, trace_repo, usage_log_repo


logger = logging.getLogger("business.tracing")


async def create_trace(
    db: AsyncSession, user_id: int, conversation_id: int | None,
    trace_id: str, model_id: str | None,
) -> int:
    try:
        t = await trace_repo.create(
            db, user_id=user_id, conversation_id=conversation_id,
            trace_id=trace_id, model_id=model_id, status="running",
        )
        return t.id
    except Exception as e:
        logger.warning("create_trace failed: %s", e)
        return 0


async def append_trace_event(
    db: AsyncSession, trace_id: str, seq: int, event_type: str,
    stage: str | None = None, payload: dict | None = None,
) -> None:
    try:
        await trace_event_repo.create(
            db, trace_id=trace_id, seq=seq, event_type=event_type,
            stage=stage,
            payload=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        )
    except Exception as e:
        logger.warning("append_trace_event failed: %s", e)


async def finish_trace(
    db: AsyncSession, trace_id: str, status: str, total_tokens: int = 0,
) -> None:
    try:
        t = await trace_repo.get_by(db, trace_id=trace_id)
        if t is not None:
            await trace_repo.finish(db, t, status, total_tokens)
    except Exception as e:
        logger.warning("finish_trace failed: %s", e)


async def log_usage(
    db: AsyncSession, user_id: int, trace_id: str,
    provider: str | None, model: str | None,
    prompt_tokens: int = 0, completion_tokens: int = 0, cost: float = 0.0,
) -> None:
    try:
        await usage_log_repo.create(
            db, user_id=user_id, trace_id=trace_id,
            provider=provider, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost=cost,
        )
    except Exception as e:
        logger.warning("log_usage failed: %s", e)


# save_feedback 已通过 proxy.py 的 feedback_repo.upsert 替代, 保留兼容导出
async def save_feedback(
    db: AsyncSession, user_id: int, trace_id: str,
    conversation_id: int | None, rating: int, comment: str | None,
) -> Feedback | None:
    try:
        return await feedback_repo.upsert(
            db, user_id, trace_id, conversation_id, rating, comment,
        )
    except Exception as e:
        logger.warning("save_feedback failed: %s", e)
        return None
