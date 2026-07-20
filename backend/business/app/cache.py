"""Redis 缓存(Cache-Aside 读)+ 写错误队列(Write-Behind 兜底)。

M0 骨架:封装核心原语,业务层(CRUD)按需调用。
- cache_get/cache_set:用户常用数据,默认 30min TTL。
- enqueue_write_error/retry_write_errors:写 MySQL 失败时入队,定时检查器重试。
"""

import json
import logging
from typing import Optional

import redis.asyncio as aioredis

from .config import settings


logger = logging.getLogger("business.cache")

_pool: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        # health_check_interval=30:每 30s 发 PING 保活,防公网 NAT 掐断空闲连接
        # socket_keepalive:TCP 层 keepalive 双保险
        _pool = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            protocol=2,
            health_check_interval=30,
            socket_keepalive=True,
            socket_timeout=10,
        )
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
            if raw is None:
                break
            if isinstance(raw, (bytes, str)):
                out.append(json.loads(raw))
    except Exception as e:
        logger.warning("pop_write_errors failed: %s", e)
    return out


# ---------- 断点缓存(Checkpoint Cache/Write-Behind) ----------
# 生成中的断点先写 Redis(不阻塞 SSE), 流结束后 flush 到 MySQL。
# TTL 24h: 超过一天没恢复的断点自动过期, 由 MySQL 兜底。

CK_TTL = 86400  # 24 小时


async def ck_set(conv_id: int, stage: str, data: dict, progress_pct: int) -> None:
    """写断点到 Redis Hash(生成中高频调用, <1ms)。"""
    try:
        r = await get_redis()
        key = f"ck:conv:{conv_id}"
        await r.hset(key, mapping={
            "stage": stage,
            "data": json.dumps(data, ensure_ascii=False),
            "progress_pct": str(progress_pct),
            "status": "paused",
        })
        await r.expire(key, CK_TTL)
    except Exception as e:
        logger.warning("ck_set failed conv=%s: %s", conv_id, e)


async def ck_get(conv_id: int) -> dict | None:
    """读断点(Redis 优先)。返回 {stage, data, progress_pct, status} 或 None。"""
    try:
        r = await get_redis()
        key = f"ck:conv:{conv_id}"
        raw = await r.hgetall(key)
        if not raw:
            return None
        return {
            "stage": raw.get("stage", "?"),
            "data": json.loads(raw["data"]) if raw.get("data") else {},
            "progress_pct": int(raw.get("progress_pct", 0)),
            "status": raw.get("status", "paused"),
        }
    except Exception as e:
        logger.warning("ck_get failed conv=%s: %s", conv_id, e)
        return None


async def ck_delete(conv_id: int) -> None:
    """流结束(done/aborted)清理 Redis 断点缓存。"""
    try:
        r = await get_redis()
        await r.delete(f"ck:conv:{conv_id}")
    except Exception as e:
        logger.warning("ck_delete failed conv=%s: %s", conv_id, e)


# ---------- 通用请求级缓存(高频读路径) ----------
async def cache_get(key: str) -> str | None:
    """通用读缓存。"""
    try:
        r = await get_redis()
        return await r.get(key)
    except Exception:
        return None


async def cache_set(key: str, value: str, ttl: int = 300) -> None:
    """通用写缓存。"""
    try:
        r = await get_redis()
        await r.set(key, value, ex=ttl)
    except Exception as e:
        logger.warning("cache_set failed key=%s: %s", key, e)


async def cache_delete(key: str) -> None:
    """通用删缓存。"""
    try:
        r = await get_redis()
        await r.delete(key)
    except Exception as e:
        logger.warning("cache_delete failed key=%s: %s", key, e)


async def cache_invalidate(pattern: str) -> None:
    """按 pattern 批量清缓存。"""
    try:
        r = await get_redis()
        keys = await r.keys(pattern)
        if keys:
            await r.delete(*keys)
    except Exception as e:
        logger.warning("cache_invalidate failed pattern=%s: %s", pattern, e)
