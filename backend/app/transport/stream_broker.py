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
        self._memory_queues: dict[str, asyncio.Queue[StreamEvent]] = defaultdict(asyncio.Queue)
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
        await self._memory_queues[stream_id].put(event)
        return event

    async def replay(self, stream_id: str, after: str | None = None) -> list[StreamEvent]:
        redis = await self._redis_client()
        if redis is not None:
            entries = await redis.xrange(f"ai:stream:{stream_id}", min=f"({after}" if after else "-", max="+")
            return [self._from_redis(stream_id, event_id, fields) for event_id, fields in entries]
        events = self._memory_events.get(stream_id, [])
        if after is None:
            return list(events)
        try:
            seq = int(after.rsplit("-", 1)[-1])
        except ValueError:
            seq = -1
        return [event for event in events if event.seq > seq]

    async def subscribe(self, stream_id: str, after: str | None = None) -> AsyncIterator[StreamEvent]:
        for event in await self.replay(stream_id, after):
            yield event
            if event.type in TERMINAL_TYPES:
                return
        redis = await self._redis_client()
        if redis is not None:
            cursor = after or "0-0"
            while True:
                streams = await redis.xread({f"ai:stream:{stream_id}": cursor}, block=15000, count=100)
                if not streams:
                    continue
                for _, entries in streams:
                    for event_id, fields in entries:
                        cursor = event_id
                        event = self._from_redis(stream_id, event_id, fields)
                        yield event
                        if event.type in TERMINAL_TYPES:
                            return
        else:
            queue = self._memory_queues[stream_id]
            while True:
                event = await queue.get()
                yield event
                if event.type in TERMINAL_TYPES:
                    return

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
