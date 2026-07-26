"""错误队列对账器(Write-Behind 兜底)。

定时取出 queue:error 中的失败写操作并重试落 MySQL。
"""

import asyncio
import logging

from .cache import pop_write_errors

logger = logging.getLogger("business.reconciler")

_running = False


async def _retry_one(payload: dict) -> bool:
    """按 payload 类型重试写 MySQL。

    payload 结构:
      {"type": "persist_chat", "trace_id": ..., "user_id": ..., ...}
    """
    kind = payload.get("type", "")
    if kind == "persist_chat":
        try:
            from .db import SessionLocal
            from .proxy import _persist_conversation
            from .tracing import finish_trace
            async with SessionLocal() as session:
                await finish_trace(session, payload["trace_id"], payload.get("terminal_status", "error"),
                                   max(0, len(payload.get("assistant_text", "")) // 4))
                await _persist_conversation(
                    session,
                    user_id=payload["user_id"],
                    conversation_id=payload["conversation_id"],
                    model=payload.get("model", "unknown"),
                    user_text=payload.get("user_text", ""),
                    assistant_text=payload.get("assistant_text", ""),
                    trace_id=payload["trace_id"],
                    preview_url=payload.get("preview_url"),
                )
            logger.info("reconciler 重试成功 trace=%s", payload.get("trace_id"))
            return True
        except Exception as e:
            logger.warning("reconciler 重试失败 trace=%s: %s", payload.get("trace_id"), e)
            return False
    logger.warning("reconciler 未知 payload 类型: %s", kind)
    return True  # 跳过未知类型，避免永久堆积


async def run_reconciler(interval: float = 30.0) -> None:
    global _running
    if _running:
        return
    _running = True
    logger.info("reconciler started (interval=%.0fs)", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            items = await pop_write_errors(limit=50)
            for it in items:
                try:
                    ok = await _retry_one(it)
                    if not ok:
                        # 仍失败:可入 DLQ(死信),M0 略
                        logger.warning("retry failed, drop (no DLQ yet): %s", it)
                except Exception as e:
                    logger.error("reconciler item error: %s", e)
    finally:
        _running = False


def start_reconciler():
    """在 FastAPI startup 中调用,挂后台任务。"""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(run_reconciler())
    except RuntimeError:
        logger.warning("no running loop, reconciler not started")
