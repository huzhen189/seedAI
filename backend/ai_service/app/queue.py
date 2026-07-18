"""生成任务队列 + 进度分发(1-C: Redis 队列 + Worker 池)。

- RedisBackend : 生产/真实路径。任务入 `queue:generate`(列表),Worker `BRPOP` 消费;
  进度经 Redis PubSub 频道 `gen:progress:<trace_id>` 发布;取消经 `cancel:<trace_id>` 标记。
- MemoryBackend: 开发兜底(无 redis 时)。进程内 asyncio.Queue,保证装 Docker 前也能本地验证闭环。

选择逻辑(get_queue):
- 环境变量 DEV_MEMORY_QUEUE=1 或 REDIS_URL 以 memory:// 开头 → MemoryBackend
- 否则 → RedisBackend(懒加载 redis 库,缺库时回退 MemoryBackend 并告警)
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncGenerator, Dict, Optional

from .config import settings
from .events import TERMINAL_EVENTS
from .runner import run_skill

_JOB_QUEUE = "queue:generate"


class QueueBackend:
    """队列抽象。子类实现具体存储。"""

    async def open_channel(self, trace_id: str):
        """建立进度订阅通道(在 enqueue 之前调用,避免丢首帧)。返回供 subscribe 使用的句柄。"""
        raise NotImplementedError

    async def subscribe(self, handle) -> AsyncGenerator[Dict[str, Any], None]:
        """迭代进度事件,直到终止事件。handle 来自 open_channel。"""
        raise NotImplementedError

    async def enqueue(self, job: Dict[str, Any]) -> None:
        raise NotImplementedError

    async def dequeue(self) -> Dict[str, Any]:
        raise NotImplementedError

    async def publish(self, trace_id: str, event: Dict[str, Any]) -> None:
        raise NotImplementedError

    async def is_cancelled(self, trace_id: str) -> bool:
        raise NotImplementedError

    async def set_cancel(self, trace_id: str) -> None:
        raise NotImplementedError


class MemoryBackend(QueueBackend):
    def __init__(self):
        self._jobs: asyncio.Queue = asyncio.Queue()
        self._progress: Dict[str, asyncio.Queue] = {}
        self._cancel: set = set()

    async def open_channel(self, trace_id: str):
        return self._progress.setdefault(trace_id, asyncio.Queue())

    async def subscribe(self, handle: asyncio.Queue) -> AsyncGenerator[Dict[str, Any], None]:
        while True:
            event = await handle.get()
            yield event
            if event.get("event") in TERMINAL_EVENTS:
                break

    async def enqueue(self, job: Dict[str, Any]) -> None:
        await self._jobs.put(job)

    async def dequeue(self) -> Dict[str, Any]:
        return await self._jobs.get()

    async def publish(self, trace_id: str, event: Dict[str, Any]) -> None:
        q = self._progress.get(trace_id)
        if q is not None:
            await q.put(event)

    async def is_cancelled(self, trace_id: str) -> bool:
        return trace_id in self._cancel

    async def set_cancel(self, trace_id: str) -> None:
        self._cancel.add(trace_id)


class RedisBackend(QueueBackend):
    def __init__(self):
        import redis.asyncio as aioredis

        self._r = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def open_channel(self, trace_id: str):
        ps = self._r.pubsub()
        await ps.subscribe(f"gen:progress:{trace_id}")
        return ps

    async def subscribe(self, ps) -> AsyncGenerator[Dict[str, Any], None]:
        try:
            async for msg in ps.listen():
                if msg.get("type") != "message":
                    continue
                event = json.loads(msg["data"])
                yield event
                if event.get("event") in TERMINAL_EVENTS:
                    break
        finally:
            await ps.unsubscribe()
            await ps.aclose()

    async def enqueue(self, job: Dict[str, Any]) -> None:
        await self._r.lpush(_JOB_QUEUE, json.dumps(job, ensure_ascii=False))

    async def dequeue(self) -> Dict[str, Any]:
        _, raw = await self._r.brpop(_JOB_QUEUE, timeout=0)
        return json.loads(raw)

    async def publish(self, trace_id: str, event: Dict[str, Any]) -> None:
        await self._r.publish(
            f"gen:progress:{trace_id}", json.dumps(event, ensure_ascii=False)
        )

    async def is_cancelled(self, trace_id: str) -> bool:
        return await self._r.exists(f"cancel:{trace_id}") == 1

    async def set_cancel(self, trace_id: str) -> None:
        await self._r.set(f"cancel:{trace_id}", "1", ex=3600)


_backend: Optional[QueueBackend] = None


def get_queue() -> QueueBackend:
    """按环境选择队列后端(单例)。"""
    global _backend
    if _backend is not None:
        return _backend

    use_memory = (
        os.getenv("DEV_MEMORY_QUEUE") == "1"
        or settings.redis_url.startswith("memory://")
    )
    if use_memory:
        _backend = MemoryBackend()
        return _backend

    try:
        _backend = RedisBackend()
    except Exception as e:  # 缺 redis 库或连不上 → 退内存兜底,保证可跑
        import logging

        logging.warning("Redis 不可用,回退内存队列: %s", e)
        _backend = MemoryBackend()
    return _backend


async def worker_loop(concurrency: int = 1):
    """Worker 池:消费 queue:generate,运行 run_skill,把每个事件 publish 到对应进度频道。"""
    q = get_queue()
    # 用 asyncio 任务池模拟并发 Worker
    async def _one():
        while True:
            job = await q.dequeue()
            trace_id = job.get("trace_id")
            model_id = job.get("model_id")
            messages = job.get("messages", [])
            skill = job.get("skill")

            async def _cancelled():
                return await q.is_cancelled(trace_id) if trace_id else False

            try:
                async for event in run_skill(
                    skill or "generate_site",
                    model_id,
                    messages,
                    trace_id=trace_id,
                    is_cancelled=_cancelled,
                ):
                    await q.publish(trace_id, event)
            except Exception as e:  # 防御:避免 Worker 崩溃
                await q.publish(trace_id, {"event": "error", "data": str(e)})
                await q.publish(trace_id, {"event": "done", "data": {}})

    await asyncio.gather(*[_one() for _ in range(concurrency)])
