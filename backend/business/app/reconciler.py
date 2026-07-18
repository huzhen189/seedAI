"""错误队列对账器(Write-Behind 兜底)。

定时取出 queue:error 中的失败写操作并重试落 MySQL。
M0 用后台 asyncio 任务(简单 sleep 循环);M2 抽成独立 Worker(APScheduler)。
这里仅演示骨架:取出后打印,真正重试逻辑按业务 payload 实现。
"""

import asyncio
import logging

from .cache import pop_write_errors


logger = logging.getLogger("business.reconciler")

_running = False


async def _retry_one(payload: dict) -> bool:
    """按 payload 类型重试写 MySQL。M0 占位:成功返回 True。

    约定 payload 结构:
      {"kind": "upsert_user"|"append_message"|..., "data": {...}}
    """
    logger.info("reconcile retry payload=%s", payload.get("kind"))
    # TODO(M2):实现真实回写。当前骨架直接标记成功,避免堆积。
    return True


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
