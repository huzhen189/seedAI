"""唯一 SSE 事件适配器：Redis Stream 为优先后端，内存后端只服务本地开发。"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.core.contracts import StreamEvent


TERMINAL_TYPES = {"done", "error"}


class StreamBroker:
    def __init__(self) -> None:
        self._memory_events: dict[str, list[StreamEvent]] = defaultdict(list)
        # 每个订阅者持有独立队列(扇出)。共用一个队列会让并发订阅者互相抢事件,
        # 导致断线续传与多端同看时各自只收到部分帧。
        self._memory_subscribers: dict[str, set[asyncio.Queue[StreamEvent]]] = defaultdict(set)
        self._seq: dict[str, int] = defaultdict(int)
        self._redis: Any | None = None
        self._redis_disabled = settings.dev_memory_queue

    async def _redis_client(self) -> Any | None:
        if self._redis_disabled:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as redis

            client = redis.from_url(settings.redis_url, decode_responses=True)
            await client.ping()
            self._redis = client
            return client
        except Exception:
            self._redis_disabled = True
            return None

    async def publish(self, *, stream_id: str, turn_id: str, trace_id: str, type: str, data: dict[str, Any]) -> StreamEvent:
        self._seq[stream_id] += 1
        raw = {
            "turn_id": turn_id,
            "trace_id": trace_id,
            "seq": self._seq[stream_id],
            "timestamp": datetime.now(UTC).isoformat(),
            "type": type,
            "data": data,
        }
        redis = await self._redis_client()
        if redis is not None:
            redis_id = await redis.xadd(
                f"ai:stream:{stream_id}",
                {"payload": json.dumps(raw, ensure_ascii=False, separators=(",", ":"))},
                maxlen=5000,
                approximate=True,
            )
            await redis.expire(f"ai:stream:{stream_id}", 7200)
            return self._to_event(stream_id, redis_id, raw)
        event_id = f"memory-{self._seq[stream_id]}"
        event = self._to_event(stream_id, event_id, raw)
        self._memory_events[stream_id].append(event)
        for queue in tuple(self._memory_subscribers[stream_id]):
            queue.put_nowait(event)
        return event

    async def replay(self, stream_id: str, after: str | None = None) -> list[StreamEvent]:
        redis = await self._redis_client()
        if redis is not None:
            entries = await redis.xrange(f"ai:stream:{stream_id}", min=f"({after}" if after else "-", max="+")
            return [self._from_redis(stream_id, event_id, fields) for event_id, fields in entries]
        events = self._memory_events.get(stream_id, [])
        if after is None:
            return list(events)
        return [event for event in events if event.seq > self._memory_seq(after)]

    async def subscribe(self, stream_id: str, after: str | None = None) -> AsyncIterator[StreamEvent]:
        """先回放积压、再挂实时订阅；两段之间必须交接游标。

        回放与实时若各自从头开始，同一批事件会被投递两次(客户端表现为 seq 回退重来)。
        Redis 分支交接 event_id 游标，内存分支按 seq 兜底去重；实时阶段统一再做一次
        seq 单调过滤，确保任何后端都不会重复下发。
        """
        last_seq = self._memory_seq(after) if after else 0
        cursor = after or "0-0"

        # 内存后端必须在回放前注册队列，否则回放与订阅之间产生的事件会整批丢失。
        redis = await self._redis_client()
        queue: asyncio.Queue[StreamEvent] | None = None
        if redis is None:
            queue = asyncio.Queue()
            self._memory_subscribers[stream_id].add(queue)

        try:
            for event in await self.replay(stream_id, after):
                yield event
                last_seq = max(last_seq, event.seq)
                cursor = event.event_id
                if event.type in TERMINAL_TYPES:
                    return

            if redis is not None:
                while True:
                    streams = await redis.xread({f"ai:stream:{stream_id}": cursor}, block=15000, count=100)
                    if not streams:
                        continue
                    for _, entries in streams:
                        for event_id, fields in entries:
                            cursor = event_id
                            event = self._from_redis(stream_id, event_id, fields)
                            if event.seq <= last_seq:
                                continue
                            last_seq = event.seq
                            yield event
                            if event.type in TERMINAL_TYPES:
                                return
            else:
                assert queue is not None
                while True:
                    event = await queue.get()
                    if event.seq <= last_seq:
                        continue
                    last_seq = event.seq
                    yield event
                    if event.type in TERMINAL_TYPES:
                        return
        finally:
            if queue is not None:
                self._memory_subscribers[stream_id].discard(queue)

    @staticmethod
    def _memory_seq(after: str | None) -> int:
        """把内存游标 memory-N 解析成 seq 基线。

        只认 memory- 前缀：Redis 游标形如 1754035613000-0，末段是分片下标而非 seq，
        误当基线会把合法事件过滤掉。Redis 分支的正确性由 event_id 游标保证。
        """
        if not after or not after.startswith("memory-"):
            return 0
        try:
            return int(after.removeprefix("memory-"))
        except ValueError:
            return 0

    @staticmethod
    def _to_event(stream_id: str, event_id: str, raw: dict[str, Any]) -> StreamEvent:
        return StreamEvent(
            stream_id=stream_id,
            turn_id=raw["turn_id"],
            trace_id=raw["trace_id"],
            event_id=event_id,
            seq=raw["seq"],
            timestamp=raw["timestamp"],
            type=raw["type"],
            data=raw["data"],
        )

    @classmethod
    def _from_redis(cls, stream_id: str, event_id: str, fields: dict[str, str]) -> StreamEvent:
        return cls._to_event(stream_id, event_id, json.loads(fields["payload"]))


broker = StreamBroker()
