"""生成任务队列 + 进度分发(1-C: Redis 队列 + Worker 池)。

进度持久化改造(支撑「离线继续 + 重连回放」):
- 进度不再只走易失 PubSub,而是写入 **可回放的 Redis Stream** `gen:stream:<trace_id>`。
  Worker 每产出一个事件就 `XADD`;订阅端先 `XRANGE` 回放历史,再 `XREAD BLOCK` 续接实时,
  天然支持「客户端断线 → Worker 继续跑 → 重连从断点(或从头)回放」。
- 内存兜底(MemoryBackend)同样保存历史列表,支持按索引回放。

选择逻辑(get_queue):
- 环境变量 DEV_MEMORY_QUEUE=1 或 REDIS_URL 以 memory:// 开头 → MemoryBackend
- 否则 → RedisBackend(懒加载 redis 库,缺库时回退 MemoryBackend 并告警)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Any, Dict, Optional

from .config import settings
from .events import TERMINAL_EVENTS
from .router import detect_intent, skill_for
from .runner import run_skill


_JOB_QUEUE = "queue:generate"
_STREAM_PREFIX = "gen:stream:"  # + trace_id -> Redis Stream(可回放进度)

logger = logging.getLogger(__name__)


class QueueBackend:
    """队列抽象。子类实现具体存储。"""

    async def open_channel(self, trace_id: str):
        """建立进度通道句柄(在 enqueue 之前调用,避免丢首帧)。返回 subscribe 使用的键。"""
        raise NotImplementedError

    async def stream_exists(self, trace_id: str) -> bool:
        """该 trace_id 的进度流是否已存在(用于 /generate 判断是否续接而非重新入队)。"""
        raise NotImplementedError

    async def subscribe(
        self, trace_id: str, after: Optional[str] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """迭代进度事件,直到终止事件。

        - after=None:从流起点全量回放(新任务首次连接 / 重连全量回放);
        - after=<id>:仅回放该 id 之后的增量(断点续传)。
        """
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
        self._progress: Dict[str, asyncio.Queue] = {}  # 实时转发队列
        self._history: Dict[str, list] = {}  # trace_id -> [event, ...] 历史(可回放)
        self._cancel: set = set()

    async def open_channel(self, trace_id: str):
        self._history.setdefault(trace_id, [])
        return trace_id

    async def stream_exists(self, trace_id: str) -> bool:
        return trace_id in self._history

    async def subscribe(self, trace_id: str, after: Optional[str] = None):
        history = self._history.get(trace_id, [])
        # 回放历史(after 为 None 全量;为索引字符串时回放其后部分)
        start = 0
        if after is not None:
            try:
                start = int(after) + 1
            except ValueError:
                start = 0
        for ev in history[start:]:
            yield ev
            if ev.get("event") in TERMINAL_EVENTS:
                return
        # 历史已含终止事件 -> 直接结束(无需再等实时)
        if history and history[-1].get("event") in TERMINAL_EVENTS:
            return
        # 续接实时队列
        q = self._progress.setdefault(trace_id, asyncio.Queue())
        while True:
            ev = await q.get()
            yield ev
            if ev.get("event") in TERMINAL_EVENTS:
                break

    async def enqueue(self, job: Dict[str, Any]) -> None:
        await self._jobs.put(job)

    async def dequeue(self) -> Dict[str, Any]:
        return await self._jobs.get()

    async def publish(self, trace_id: str, event: Dict[str, Any]) -> None:
        self._history.setdefault(trace_id, []).append(event)
        q = self._progress.get(trace_id)
        if q is not None:
            await q.put(event)

    async def is_cancelled(self, trace_id: str) -> bool:
        return trace_id in self._cancel

    async def set_cancel(self, trace_id: str) -> None:
        self._cancel.add(trace_id)


class RedisBackend(QueueBackend):
    def __init__(self):
        import redis
        import redis.asyncio as aioredis

        # 关键:不能只做 TCP 探测。redis-py 默认走 RESP3 握手会先发 `HELLO` 命令,
        # 而部分云 Redis(老版本/代理)不支持 HELLO,会直接返回
        #   unknown command `HELLO` ... -> /generate 运行时 500。
        # 这里用「同步客户端 + protocol=2(避开 HELLO)」做一次真实 PING 握手,
        # 连不上 / 协议不兼容就抛 ConnectionError,让 get_queue 回退到 MemoryBackend。
        try:
            sync_r = redis.from_url(
                settings.redis_url,
                protocol=2,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            sync_r.ping()
            sync_r.close()
        except Exception as e:  # 含连接失败、HELLO/AUTH 不兼容等
            raise ConnectionError(f"Redis 不可用或不兼容: {e}") from e

        # 异步客户端:强制 protocol=2 + 心跳保活 + 显式 socket_timeout。
        # health_check_interval=30 每 30s 发 PING 维持连接;
        # socket_keepalive 启用 TCP keepalive;retry_on_timeout 读超时自动重试;
        # socket_timeout=10 给足 xread block 的缓冲(block=3000,超时后 1s 重连)。
        self._r = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            protocol=2,
            health_check_interval=30,
            socket_keepalive=True,
            retry_on_timeout=True,
            socket_timeout=10,
        )

    def _key(self, trace_id: str) -> str:
        return f"{_STREAM_PREFIX}{trace_id}"

    async def open_channel(self, trace_id: str):
        # Stream 以 trace_id 为键,open 即确保后续 publish/subscribe 指向同一键。
        return trace_id

    async def stream_exists(self, trace_id: str) -> bool:
        try:
            return await self._r.exists(self._key(trace_id)) == 1
        except Exception:
            return False

    async def subscribe(self, trace_id: str, after: Optional[str] = None):
        key = self._key(trace_id)
        last_id = after or "0"
        # 1) 回放历史(增量或全量)
        if after is None:
            hist = await self._r.xrange(key, "-", "+")
        else:
            # 排他区间:(after, +] —— 只回放断点之后的事件
            hist = await self._r.xrange(key, f"({after}", "+")
        for entry_id, fields in hist:
            event = json.loads(fields.get("event", "{}"))
            yield event
            last_id = entry_id
            if event.get("event") in TERMINAL_EVENTS:
                return
        if hist:
            last_id = hist[-1][0]
        # 2) 续接实时(阻塞等待新事件,带连接断开重连)
        import redis.asyncio as aioredis

        while True:
            try:
                resp = await self._r.xread({key: last_id}, block=3000, count=100)
            except (aioredis.TimeoutError, aioredis.ConnectionError, OSError) as e:
                # 公网 NAT/防火墙掐断长连接时触发;重建客户端从 last_id 续接
                logger.warning("subscribe xread 断连, %s 秒后重连: %s", 1, e)
                await asyncio.sleep(1)
                with suppress(Exception):
                    await self._r.aclose()
                self._r = aioredis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    protocol=2,
                    health_check_interval=30,
                    socket_keepalive=True,
                    retry_on_timeout=True,
                    socket_timeout=10,
                )
                continue
            if not resp:
                # 超时无新数据:再探一次,避免长空闲误判;若流已含终止事件则退出
                continue
            for _k, entries in resp:
                for entry_id, fields in entries:
                    event = json.loads(fields.get("event", "{}"))
                    yield event
                    last_id = entry_id
                    if event.get("event") in TERMINAL_EVENTS:
                        return

    async def enqueue(self, job: Dict[str, Any]) -> None:
        await self._r.lpush(_JOB_QUEUE, json.dumps(job, ensure_ascii=False))

    async def dequeue(self) -> Dict[str, Any]:
        _, raw = await self._r.brpop(_JOB_QUEUE, timeout=0)
        return json.loads(raw)

    async def publish(self, trace_id: str, event: Dict[str, Any]) -> None:
        # 持久化进度到可回放 Stream;XTRIM 限长避免无限膨胀
        await self._r.xadd(
            self._key(trace_id),
            {"event": json.dumps(event, ensure_ascii=False)},
            maxlen=5000,
            approximate=True,
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

    use_memory = os.getenv("DEV_MEMORY_QUEUE") == "1" or settings.redis_url.startswith("memory://")
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
    """Worker 池:消费 queue:generate,运行 run_skill,把每个事件 publish 到对应进度流(持久化)。"""
    q = get_queue()

    # 用 asyncio 任务池模拟并发 Worker
    async def _one():
        while True:
            try:
                job = await q.dequeue()
            except Exception as e:
                logger.warning("Worker dequeue 失败, 1s 后重试: %s", e)
                await asyncio.sleep(1)
                continue
            trace_id = job.get("trace_id")
            model_id = job.get("model_id")
            messages = job.get("messages", [])
            skill = job.get("skill")

            async def _cancelled(trace_id=trace_id):
                return await q.is_cancelled(trace_id) if trace_id else False

            try:
                intent = detect_intent(messages, model_id)
                skill_name = skill or skill_for(intent["level1"], intent["level2"]) or "explain"

                if intent["level1"] == "unsupported":
                    # 记录 unsupported 统计(给业务端 metrics 用)
                    async for event in run_skill(
                        "explain", model_id, messages,
                        trace_id=trace_id, is_cancelled=_cancelled,
                        intent_info=intent,
                    ):
                        await q.publish(trace_id, event)
                    # 额外发一个 unsupported 事件
                    await q.publish(
                        trace_id,
                        {"event": "unsupported", "data": {
                            "input": (messages[-1].get("content", "") if messages else "")[:200],
                        }},
                    )
                    await q.publish(trace_id, {"event": "done", "data": {}})
                    continue

                async for event in run_skill(
                    skill_name, model_id, messages,
                    trace_id=trace_id, is_cancelled=_cancelled,
                    intent_info=intent,
                ):
                    await q.publish(trace_id, event)
            except Exception as e:  # 防御:避免 Worker 崩溃
                await q.publish(trace_id, {"event": "error", "data": str(e)})
                await q.publish(trace_id, {"event": "done", "data": {}})

    await asyncio.gather(*[_one() for _ in range(concurrency)])
