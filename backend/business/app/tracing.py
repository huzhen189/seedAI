"""对话追踪与回放(③-a):Trace / TraceEvent 落库,供管理后台回放与质量指标。

设计(文档 §3.13 采纳方案1 MySQL 双表):一次 SSE 会话 = 一个 Trace,结构化事件按 seq
追加 TraceEvent 可重放。token 事件量大,不逐条落库,仅记录聚合 token 数;node/think/plan/
error/done/aborted/degraded 等结构化事件逐条落库,足以重建流程时间线与质量统计。
所有写操作均吞异常,绝不阻断主生成流。
"""

import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Feedback, Trace, TraceEvent, UsageLog


logger = logging.getLogger("business.tracing")


async def create_trace(
    db: AsyncSession,
    user_id: int,
    conversation_id: int | None,
    trace_id: str,
    model_id: str | None,
) -> int:
    """新建一条 Trace(running 状态),返回自增 id。"""
    try:
        t = Trace(
            user_id=user_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            model_id=model_id,
            status="running",
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t.id
    except Exception as e:
        logger.warning("create_trace failed: %s", e)
        return 0


async def append_trace_event(
    db: AsyncSession,
    trace_id: str,
    seq: int,
    event_type: str,
    stage: str | None = None,
    payload: dict | None = None,
) -> None:
    """追加一条 TraceEvent(结构化事件),供回放 / 质量统计。"""
    try:
        db.add(
            TraceEvent(
                trace_id=trace_id,
                seq=seq,
                event_type=event_type,
                stage=stage,
                payload=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            )
        )
        await db.commit()
    except Exception as e:
        logger.warning("append_trace_event failed: %s", e)


async def finish_trace(
    db: AsyncSession, trace_id: str, status: str, total_tokens: int = 0
) -> None:
    """结束 Trace:写最终状态 / 完成时间 / 聚合 token 数。"""
    try:
        t = (
            await db.execute(select(Trace).where(Trace.trace_id == trace_id))
        ).scalar_one_or_none()
        if t is not None:
            t.status = status
            t.total_tokens = total_tokens
            t.finished_at = datetime.utcnow()
            await db.commit()
    except Exception as e:
        logger.warning("finish_trace failed: %s", e)


async def log_usage(
    db: AsyncSession,
    user_id: int,
    trace_id: str,
    provider: str | None,
    model: str | None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
) -> None:
    """记录一次生成的用量账本(成本归集)。"""
    try:
        db.add(
            UsageLog(
                user_id=user_id,
                trace_id=trace_id,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=cost,
            )
        )
        await db.commit()
    except Exception as e:
        logger.warning("log_usage failed: %s", e)


async def save_feedback(
    db: AsyncSession,
    user_id: int,
    trace_id: str,
    conversation_id: int | None,
    rating: int,
    comment: str | None,
) -> Feedback | None:
    """落一条用户评价(1—10 分 + 评论);同 trace 重复提交则更新。"""
    try:
        existing = (
            await db.execute(select(Feedback).where(Feedback.trace_id == trace_id))
        ).scalar_one_or_none()
        if existing is not None:
            existing.rating = rating
            existing.comment = comment
            existing.user_id = user_id
            existing.conversation_id = conversation_id
        else:
            existing = Feedback(
                user_id=user_id,
                trace_id=trace_id,
                conversation_id=conversation_id,
                rating=rating,
                comment=comment,
            )
            db.add(existing)
        await db.commit()
        await db.refresh(existing)
        return existing
    except Exception as e:
        logger.warning("save_feedback failed: %s", e)
        return None
