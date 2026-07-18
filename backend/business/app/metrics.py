"""轻量监控指标栈(M0 · 自研,对应文档 §3.6)。

- 进程/请求指标来自 FastAPI 中间件 → Redis(原子计数 + 滑动窗口)。
- 模型用量:record_model_usage 原子自增。
- 管理页通过 /admin/metrics(SSE) 实时订阅。
MVP 不引 Prometheus;后期可平滑替换为 /metrics 暴露文本格式。
"""

import logging
import time

from .cache import get_redis


logger = logging.getLogger("business.metrics")

# 进程启动时间(用于 uptime)
START_TIME = time.time()


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
    """给管理页的实时指标快照。"""
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
        return {
            "uptime_s": int(time.time() - START_TIME),
            "requests_total": int(total or 0),
            "requests_error": int(err or 0),
            "requests_per_min": int(rpm),
            "model_usage": {k: int(v) for k, v in (usage or {}).items()},
        }
    except Exception as e:
        logger.warning("snapshot failed: %s", e)
        return {"uptime_s": int(time.time() - START_TIME), "error": str(e)}
