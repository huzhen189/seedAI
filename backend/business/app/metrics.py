"""轻量监控指标栈(M0 · 自研,对应文档 §3.6)。

- 进程/请求指标来自 FastAPI 中间件 → Redis(原子计数 + 滑动窗口)。
- 模型用量:record_model_usage 原子自增。
- 管理页通过 /admin/metrics(SSE) 实时订阅。
MVP 不引 Prometheus;后期可平滑替换为 /metrics 暴露文本格式。
"""

import json
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


async def record_model_tokens(model_id: str, tokens: int) -> None:
    """记录模型 token 消耗(v0.9.0 增强)。"""
    try:
        r = await get_redis()
        if tokens > 0:
            await r.hincrby("stats:model_tokens", model_id, tokens)
            await r.hincrby("stats:model_count", model_id, 1)
    except Exception:
        pass


async def record_api_latency(path: str, elapsed_ms: float) -> None:
    """记录 API 接口耗时(v0.9.0 运营数据)。"""
    try:
        r = await get_redis()
        await r.lpush(f"stats:latency:{path}", str(round(elapsed_ms, 1)))
        await r.ltrim(f"stats:latency:{path}", 0, 99)  # 保留最近100条
    except Exception:
        pass


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
    """给管理页的实时指标快照(含三库健康状态+v0.9.0增强)。"""
    try:
        r = await get_redis()
        pipe = r.pipeline()
        pipe.get("stats:requests:total")
        pipe.get("stats:requests:error")
        pipe.hgetall("stats:model_usage")
        pipe.hgetall("stats:model_tokens")
        pipe.hgetall("stats:model_count")
        total, err, usage, tokens_raw, count_raw = await pipe.execute()
        minute = int(time.time() // 60)
        rpm = await r.get(f"stats:rpm:{minute}") or 0
        db = await _db_status()

        # 模型用量增强: token数 + 次数 + 估算花费
        tokens = {k: int(v) for k, v in (tokens_raw or {}).items()}
        counts = {k: int(v) for k, v in (count_raw or {}).items()}
        model_usage = {}
        all_models = set(list(tokens.keys()) + list(counts.keys()) + list((usage or {}).keys()))
        # 估算花费(USD per 1M tokens, 粗略)
        COST_RATE = {"deepseek": 0.14, "qwen": 0.30, "hy3": 0.50}
        for m in all_models:
            t = tokens.get(m, 0)
            c = counts.get(m, 0)
            rate = COST_RATE.get(m, 0.20)
            model_usage[m] = {
                "tokens": t, "count": c,
                "est_cost": round(t / 1_000_000 * rate, 4),
                "raw_count": int((usage or {}).get(m, 0)),
            }

        # API 延迟统计
        latency = {}
        for path in ["/api/chat", "/auth/login", "/admin/metrics"]:
            vals = await r.lrange(f"stats:latency:{path}", 0, 49)
            vals_f = [float(v) for v in vals if v]
            if vals_f:
                vals_f.sort()
                n = len(vals_f)
                latency[path] = {
                    "p50": round(vals_f[int(n*0.5)], 1),
                    "p90": round(vals_f[int(n*0.9)], 1),
                    "p99": round(vals_f[int(n*0.99)], 1),
                    "avg": round(sum(vals_f)/n, 1),
                    "samples": n,
                }

        # AI 核心统计(从共享Redis读取)
        ai_stats = {}
        try:
            ai_total = int((await r.hget("an:generate:total", "count")) or 0)
            ai_v090 = {k: int(v) for k, v in ((await r.hgetall("an:v090:feature")) or {}).items()}
            ai_stats = {"generate_total": ai_total, "v090_features": ai_v090}
        except Exception:
            pass

        return {
            "uptime_s": int(time.time() - START_TIME),
            "requests_total": int(total or 0),
            "requests_error": int(err or 0),
            "requests_per_min": int(rpm),
            "model_usage": model_usage,
            "api_latency": latency,
            "ai_stats": ai_stats,
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


async def record_unsupported(user_id: int, text: str) -> None:
    """记录不支持意图到 Redis(供管理后台统计 + 用户回归分析)。

    - stats:unsupported:total → 原子自增总数
    - stats:unsupported_samples → 最近 50 条采样(文本截 200 字, 带时间戳)
    """
    try:
        r = await get_redis()
        await r.incr("stats:unsupported:total")
        sample = json.dumps(
            {"user": user_id, "text": text[:200], "ts": int(time.time())},
            ensure_ascii=False,
        )
        await r.lpush("stats:unsupported_samples", sample)
        await r.ltrim("stats:unsupported_samples", 0, 49)  # 保留最近 50 条
    except Exception as e:
        logger.warning("record_unsupported failed: %s", e)
