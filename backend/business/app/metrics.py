"""轻量监控指标栈(M0 · 自研,对应文档 §3.6)。

- 进程/请求指标来自 FastAPI 中间件 → Redis(原子计数 + 滑动窗口)。
- 模型用量:record_model_usage 原子自增。
- 管理页通过 /admin/metrics(SSE) 实时订阅。
MVP 不引 Prometheus;后期可平滑替换为 /metrics 暴露文本格式。
"""

import logging
import time
from datetime import datetime, timedelta

from .cache import get_redis
from .config import settings


logger = logging.getLogger("business.metrics")

# 进程启动时间(用于 uptime)
START_TIME = time.time()


def _seconds_to_midnight() -> int:
    """距离当天结束(次日 00:00)的秒数;用于每日配额的 Redis key 过期。"""
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds()) + 1


async def consume_daily_quota(user_id: int, plan: str) -> tuple[bool, int]:
    """消费一次每日生成配额(①-b)。

    返回 (是否允许, 剩余次数):
      - 未超额: (True, 剩余次数)
      - 已超额: (False, 0)
      - Redis 不可用: fail-open (True, -1),不阻断正常生成
    key = quota:daily:{user_id},首次自增时设置当天过期,跨日自动清零。
    """
    limit = settings.plan_daily_quota.get(str(plan), settings.free_daily_quota)
    try:
        r = await get_redis()
        key = f"quota:daily:{user_id}"
        used = await r.incr(key)
        if used == 1:
            # 首次计数,过期时间设到当天结束(避免跨日累加)
            await r.expire(key, _seconds_to_midnight())
        if used > limit:
            return False, 0
        return True, limit - used
    except Exception as e:
        logger.warning("consume_daily_quota failed (fail-open): %s", e)
        return True, -1


async def record_model_usage(user_id: int, model_id: str) -> None:
    try:
        r = await get_redis()
        await r.hincrby("stats:model_usage", model_id, 1)
        await r.hincrby("stats:model_usage_by_user", str(user_id), 1)
    except Exception as e:
        logger.warning("record_model_usage failed: %s", e)


async def record_request(path: str, status_code: int, elapsed_ms: float) -> None:
    try:
        r = await get_redis()
        await r.incr("stats:requests:total")
        await r.incr(f"stats:requests:{path}")
        if status_code >= 400:
            await r.incr("stats:requests:error")
        # 滑动窗口:最近 1 分钟请求数
        minute = int(time.time() // 60)
        await r.incr(f"stats:rpm:{minute}")
        await r.expire(f"stats:rpm:{minute}", 120)
    except Exception as e:
        logger.warning("record_request failed: %s", e)


async def snapshot() -> dict:
    """给管理页的实时指标快照(含三库健康状态)。"""
    try:
        r = await get_redis()
        pipe = r.pipeline()
        pipe.get("stats:requests:total")
        pipe.get("stats:requests:error")
        pipe.hgetall("stats:model_usage")
        pipe.get("stats:rpm:now")
        total, err, usage, _ = await pipe.execute()
        minute = int(time.time() // 60)
        rpm = await r.get(f"stats:rpm:{minute}") or 0
        db = await _db_status()
        return {
            "uptime_s": int(time.time() - START_TIME),
            "requests_total": int(total or 0),
            "requests_error": int(err or 0),
            "requests_per_min": int(rpm),
            "model_usage": {k: int(v) for k, v in (usage or {}).items()},
            "db": db,
        }
    except Exception as e:
        logger.warning("snapshot failed: %s", e)
        return {"uptime_s": int(time.time() - START_TIME), "error": str(e)}


async def _db_status() -> dict:
    """MySQL / Redis 连通性 + 连接池状态(每 2s 由 /admin/metrics 调用)。"""
    result: dict = {}
    # MySQL
    try:
        from sqlalchemy import text

        from .db import engine

        pool = engine.pool
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        result["mysql"] = {
            "ok": True,
            "pool_size": pool.size(),
            "checked_in": getattr(pool, "checkedin", lambda: 0)(),
            "overflow": pool.overflow(),
        }
    except Exception as e:
        result["mysql"] = {"ok": False, "error": str(e)[:200]}

    # Redis
    try:
        r = await get_redis()
        await r.ping()
        result["redis"] = {"ok": True}
    except Exception as e:
        result["redis"] = {"ok": False, "error": str(e)[:200]}

    return result
