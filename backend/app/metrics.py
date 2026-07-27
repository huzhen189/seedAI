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
from urllib.parse import urlparse as _urlparse

from .cache import get_redis
from .config import ENV_FILE, settings


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

        # API 延迟统计: 业务端(本服务中间件记录 an:latency:api:*) + 需求端(AI 核心 7102 记录 ai:api:latency:*)
        # 两组各自扫描全部接口, 供前端用 nav+tab 分成两个表单切换(R1)。
        latency = {
            "business": await _latency_stats(r, "an:latency:api:"),
            "ai_service": await _latency_stats(r, "ai:api:latency:"),
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


def _human_bytes(num: float) -> str:
    """字节数 → 人类可读(如 1.24 GB)。"""
    try:
        num = float(num)
    except Exception:
        return "-"
    if num <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while num >= 1024 and i < len(units) - 1:
        num /= 1024
        i += 1
    return f"{num:.2f} {units[i]}"


def _chroma_client():
    """业务端只读探测 Chroma 向量库(地址来源与 db.reset_db 一致: 优先 env, 回退根 .env)。"""
    import os

    import chromadb

    _cu = os.environ.get("CHROMA_URL")
    if not _cu and ENV_FILE.exists():
        try:
            for _l in ENV_FILE.read_text(encoding="utf-8").splitlines():
                _l = _l.strip()
                if _l.startswith("CHROMA_URL="):
                    _cu = _l.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    chroma_url = _cu or "http://chroma:8000"
    p = _urlparse(chroma_url)
    return chromadb.HttpClient(host=p.hostname or "localhost", port=p.port or 8000)


async def _latency_stats(r, prefix: str) -> dict:
    """扫描 Redis 指定前缀的延迟键, 计算 p50/p90/p99/avg/samples。

    r 为 None 时返回空(降级)。供指标面板展示两组接口延迟:
      - 业务端 an:latency:api:* → ZSET(member=uuid, score=耗时ms)
      - 需求端 ai:api:latency:*  → LIST(每元素=str(耗时ms))
    两种类型分别用 zrange(withscores) 与 lrange 读取。

    业务端 writer 同时写分钟粒度子键 an:latency:api:{path}:{minute}(HASH, 非采样),
    其 path 含 ':' 需跳过, 只统计纯接口路径的采样集合。
    """
    out: dict = {}
    if r is None:
        return out
    try:
        raw_keys = await r.keys(f"{prefix}*")
    except Exception:
        return out
    for full in raw_keys:
        key = full.decode() if isinstance(full, bytes) else full
        path = key[len(prefix):]
        # 跳过分钟粒度后缀子键(如 :api:{path}:{minute})
        if ":" in path:
            continue
        try:
            _raw_type = await r.type(key)
            ktype = _raw_type.decode() if isinstance(_raw_type, bytes) else _raw_type
        except Exception:
            ktype = "list"
        vals_f: list[float] = []
        try:
            if ktype == "zset":
                rows = await r.zrange(key, 0, -1, withscores=True)
                for _, score in rows:
                    try:
                        vals_f.append(float(score))
                    except Exception:
                        pass
            else:
                vals = await r.lrange(full, 0, 99)
                for v in vals:
                    try:
                        sv = v.decode() if isinstance(v, bytes) else v
                        vals_f.append(float(sv))
                    except Exception:
                        pass
        except Exception:
            continue
        if vals_f:
            vals_f.sort()
            n = len(vals_f)
            out[path] = {
                "p50": round(vals_f[int(n * 0.5)], 1),
                "p90": round(vals_f[int(n * 0.9)], 1),
                "p99": round(vals_f[int(n * 0.99)], 1),
                "avg": round(sum(vals_f) / n, 1),
                "samples": n,
            }
    return out


async def _db_status() -> dict:
    """三库(MySQL / Redis / Chroma)连通性 + 容量 + 连接状态(每 2s 由 /admin/metrics 调用)。

    返回结构统一含 capacity:{value, pct, detail} —— 供前端「具体数值 + 百分比」两行显示(R2)。
    """
    result: dict = {}

    # ── MySQL ──
    try:
        from sqlalchemy import text

        from .db import engine

        pool = engine.pool
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            # 取当前连接的数据库名作为绑定参数(修复 InvalidRequestError:
            # A value is required for bind parameter 'db' —— 原 SQL 用了命名
            # 参数 :db 却从未传值)。
            db_name = getattr(engine.url, "database", None) or ""
            size_row = (await conn.execute(
                text("SELECT SUM(data_length + index_length) AS bytes "
                     "FROM information_schema.tables WHERE table_schema = :db"),
                {"db": db_name},
            )).fetchone()
            data_bytes = int(size_row[0] or 0) if size_row else 0
            max_conn_row = (await conn.execute(text("SHOW VARIABLES LIKE 'max_connections'"))).fetchone()
            conn_row = (await conn.execute(text("SHOW GLOBAL STATUS LIKE 'Threads_connected'"))).fetchone()
            max_conn = int(max_conn_row[1]) if max_conn_row else 0
            threads = int(conn_row[1]) if conn_row else 0
            conn_pct = round(threads / max_conn * 100, 1) if max_conn else None
        result["mysql"] = {
            "ok": True,
            "pool_size": pool.size(),
            "checked_in": getattr(pool, "checkedin", lambda: 0)(),
            "overflow": pool.overflow(),
            "capacity": {
                "value": _human_bytes(data_bytes),
                "value_bytes": data_bytes,
                "pct": conn_pct,  # 连接占用百分比(第二行)
                "detail": f"连接 {threads}/{max_conn}",
            },
            "max_connections": max_conn,
            "threads_connected": threads,
        }
    except Exception as e:
        result["mysql"] = {"ok": False, "error": str(e)[:200]}

    # ── Redis ──
    try:
        r = await get_redis()
        await r.ping()
        info = await r.info("memory")
        used = int(info.get("used_memory", 0) or 0)
        maxmem = int(info.get("maxmemory", 0) or 0)
        used_pct = round(used / maxmem * 100, 1) if maxmem else None
        clients = await r.info("clients")
        keys = await r.dbsize()
        result["redis"] = {
            "ok": True,
            "capacity": {
                "value": _human_bytes(used),
                "value_bytes": used,
                "pct": used_pct,  # 内存占用百分比(第二行)
                "detail": f"上限 {_human_bytes(maxmem) if maxmem else '无限制'}",
            },
            "used_memory_human": info.get("used_memory_human"),
            "maxmemory_human": info.get("maxmemory_human"),
            "connected_clients": int(clients.get("connected_clients", 0)),
            "db_keys": int(keys),
        }
    except Exception as e:
        result["redis"] = {"ok": False, "error": str(e)[:200]}

    # ── Chroma(向量库, AI 服务托管, 业务端只读探测) ──
    try:
        c = _chroma_client()
        c.heartbeat()  # 探测连通性(返回 epoch ms)
        colls = c.list_collections()
        items = 0
        coll_info = []
        for col in colls:
            name = col.name if hasattr(col, "name") else str(col)
            try:
                cnt = c.get_collection(name).count()
            except Exception:
                cnt = 0
            items += cnt
            coll_info.append({"name": name, "count": cnt})
        result["chroma"] = {
            "ok": True,
            "capacity": {
                "value": f"{items} 向量",
                "value_bytes": items,
                "pct": None,  # 向量库无磁盘容量概念, 仅展示条目数
                "detail": f"{len(colls)} 个集合",
            },
            "collection_count": len(colls),
            "item_count": items,
            "collections": coll_info,
        }
    except Exception as e:
        result["chroma"] = {"ok": False, "error": str(e)[:200]}

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
