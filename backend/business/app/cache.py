"""Redis 缓存(Cache-Aside 读)+ 写错误队列(Write-Behind 兜底)。

M0 骨架:封装核心原语,业务层(CRUD)按需调用。
- cache_get/cache_set:用户常用数据,默认 30min TTL。
- enqueue_write_error/retry_write_errors:写 MySQL 失败时入队,定时检查器重试。
"""
import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

from .config import settings

logger = logging.getLogger("business.cache")

_pool: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(settings.redis_url, decode_responses=True, protocol=2)
    return _pool


# ---------- 用户缓存(Cache-Aside) ----------
async def cache_user_get(user_id: int) -> Optional[dict]:
    """读缓存;miss 返回 None,由调用方回源 DB 并回填。"""
    try:
        r = await get_redis()
        raw = await r.get(f"cache:user:{user_id}")
        return json.loads(raw) if raw else None
    except Exception as e:  # Redis 不可用时降级回源
        logger.warning("cache_user_get failed: %s", e)
        return None


async def cache_user_set(user_id: int, data: dict) -> None:
    try:
        r = await get_redis()
        await r.set(
            f"cache:user:{user_id}",
            json.dumps(data, default=str),
            ex=settings.cache_user_ttl,
        )
    except Exception as e:
        logger.warning("cache_user_set failed: %s", e)


async def cache_user_invalidate(user_id: int) -> None:
    try:
        r = await get_redis()
        await r.delete(f"cache:user:{user_id}")
    except Exception as e:
        logger.warning("cache_user_invalidate failed: %s", e)


# ---------- 写错误队列(Write-Behind 兜底) ----------
async def enqueue_write_error(payload: dict) -> None:
    """MySQL 写失败:入错误队列(带临时值),供定时检查器重试。"""
    try:
        r = await get_redis()
        await r.rpush("queue:error", json.dumps(payload, default=str))
    except Exception as e:
        logger.error("enqueue_write_error failed (data lost risk): %s", e)


async def pop_write_errors(limit: int = 50) -> list[dict]:
    """取出最多 limit 条错误待重试(配合确认机制,这里简单 LPOP)。"""
    out: list[dict] = []
    try:
        r = await get_redis()
        for _ in range(limit):
            raw = await r.lpop("queue:error")
            if not raw:
                break
            out.append(json.loads(raw))
    except Exception as e:
        logger.warning("pop_write_errors failed: %s", e)
    return out
