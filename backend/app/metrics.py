"""轻量监控指标栈(M0 · 自研,对应文档 §3.6)。

- 进程/请求指标来自 FastAPI 中间件 → Redis(原子计数 + 滑动窗口)。
- 模型用量:record_model_usage 原子自增。
- 管理页通过 /admin/metrics(SSE) 实时订阅。
MVP 不引 Prometheus;后期可平滑替换为 /metrics 暴露文本格式。
"""

import asyncio
import ctypes
import json
import logging
import os as _os
import platform as _platform
import shutil as _shutil
import socket as _socket
import string as _string
import sys as _sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse as _urlparse

from .cache import get_redis
from app.config import ENV_FILE, settings

# 主机(操作系统)指标: 优先 psutil(若已安装), 否则纯标准库回退。
try:
    import psutil as _psutil
except Exception:  # pragma: no cover - psutil 可选
    _psutil = None


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
            "system": await _system_status(),
        }
    except Exception as e:
        logger.warning("snapshot failed: %s", e)
        return {
            "uptime_s": int(time.time() - START_TIME),
            "error": str(e),
            "system": await _system_status(),
        }


async def _system_status() -> dict:
    """采集运行主机(操作系统)指标, 管理页『服务器系统状态』区块使用。

    设计原则: 与 _db_status 解耦(独立异常边界); 任一子系统失败只标记 off 不影响其他;
    优先 psutil, 否则纯标准库回退; 全部 CPU/内存 采集强制 0.5s 超时, 防拖垮 /admin/metrics SSE。
    数值单位: 百分比为 0~100 浮点; bytes 为绝对字节(前端用 _human_bytes 展示)。
    """
    try:
        # 并行采集 CPU 百分比(需要采样间隔)与内存, 各加 0.5s 超时保护
        cpu_task = asyncio.to_thread(_cpu_percent)
        mem_task = asyncio.to_thread(_mem_info)
        cpu_pct, cpu_load, cpu_cores = await asyncio.wait_for(cpu_task, timeout=0.6)
        mem = await asyncio.wait_for(mem_task, timeout=0.6)
    except Exception as e:
        logger.warning("system_status cpu/mem probe failed: %s", e)
        cpu_pct, cpu_load, cpu_cores = None, None, None
        mem = {"ok": False, "error": str(e)[:200]}

    try:
        disk = _disk_info()
    except Exception as e:
        logger.warning("system_status disk probe failed: %s", e)
        disk = {"ok": False, "error": str(e)[:200]}

    try:
        boot_ts = _boot_time()
    except Exception:
        boot_ts = None

    return {
        "platform": _platform_name(),
        "hostname": _safe(lambda: _socket.gethostname()),
        "kernel": _safe(lambda: _platform.release()),
        "arch": _safe(lambda: _platform.machine() or _platform.architecture()[0]),
        "python_version": _sys.version.split()[0],
        "cpu_cores": cpu_cores,
        "cpu_percent": cpu_pct,
        "load_avg": cpu_load,          # (1m,5m,15m) 或 None
        "mem": mem,
        "disk": disk,
        "boot_time": boot_ts,          # epoch 秒, 前端算开机时长
        "ts": int(time.time()),
    }


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def _platform_name() -> dict:
    """友好的操作系统名称 + 系统分类(linux/windows/darwin)。"""
    sys_name = _platform.system().lower()  # 'linux' / 'windows' / 'darwin'
    if sys_name == "linux":
        name = _linux_pretty_name() or "Linux"
    elif sys_name == "windows":
        name = f"Windows {_platform.version()}"
    elif sys_name == "darwin":
        name = f"macOS {_platform.mac_ver()[0]}"
    else:
        name = _platform.system() or "未知"
    return {"name": name, "family": sys_name}


def _linux_pretty_name() -> str:
    """读 /etc/os-release 的 PRETTY_NAME(如 'CentOS Linux 7 (Core)')。"""
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return ""


def _cpu_percent():
    """返回 (cpu_percent, load_avg_or_None, logical_cores)。"""
    if _psutil is not None:
        cpu_pct = _psutil.cpu_percent(interval=0.3)
        cores = _psutil.cpu_count(logical=True)
        load = _psutil.getloadavg()  # (1m,5m,15m)
        return float(cpu_pct), list(load), cores
    # 纯标准库回退
    cores = _safe(lambda: _os.cpu_count()) or 1
    cpu_pct = _cpu_percent_fallback()
    load = _load_avg_fallback()
    return cpu_pct, load, cores


def _cpu_percent_fallback():
    """Linux 用 /proc/stat 两次采样差算总占用; 其他平台返回 None(用负载近似)。"""
    try:
        if not _os.path.exists("/proc/stat"):
            return None
        def _read():
            with open("/proc/stat", encoding="utf-8") as f:
                line = f.readline()
            parts = list(map(int, line.split()[1:]))
            idle = parts[3]
            total = sum(parts)
            return total, idle
        t0, i0 = _read()
        time.sleep(0.3)
        t1, i1 = _read()
        total_d = t1 - t0
        idle_d = i1 - i0
        if total_d <= 0:
            return None
        return round((1 - idle_d / total_d) * 100, 1)
    except Exception:
        return None


def _load_avg_fallback():
    """Linux 读 /proc/loadavg; 其他平台 None。"""
    try:
        if _os.path.exists("/proc/loadavg"):
            with open("/proc/loadavg", encoding="utf-8") as f:
                parts = f.read().split()
            return [float(parts[0]), float(parts[1]), float(parts[2])]
    except Exception:
        pass
    return None


def _mem_info() -> dict:
    """内存使用情况: total/available/used(字节) + percent。"""
    if _psutil is not None:
        vm = _psutil.virtual_memory()
        return {
            "ok": True,
            "total": vm.total,
            "available": vm.available,
            "used": vm.used,
            "percent": float(vm.percent),
        }
    # 纯标准库回退(Linux /proc/meminfo)
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                val_kb = int(parts[1].strip().split()[0])  # kB
                info[key] = val_kb * 1024
        total = info.get("MemTotal")
        available = info.get("MemAvailable") or (total - info.get("MemFree", 0))
        used = total - available if total is not None else None
        percent = round(used / total * 100, 1) if (total and used is not None) else None
        if total is None:
            return {"ok": False, "error": "无内存数据"}
        return {
            "ok": True,
            "total": total,
            "available": available,
            "used": used,
            "percent": percent,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _disk_info() -> dict:
    """所有挂载分区的磁盘使用情况(跳过伪/可移动文件系统)。

    返回 { ok, partitions: [{device,mountpoint,fstype,total,used,free,percent}], total, used, free }。
    """
    if _psutil is not None:
        parts = _psutil.disk_partitions(all=False)
        items = []
        for p in parts:
            try:
                usage = _psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            items.append({
                "device": p.device,
                "mountpoint": p.mountpoint,
                "fstype": p.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": round(usage.percent, 1),
            })
    else:
        items = _disk_info_fallback()
    if not items:
        return {"ok": False, "error": "无磁盘数据", "partitions": []}
    t_all = sum(x["total"] for x in items)
    u_all = sum(x["used"] for x in items)
    f_all = sum(x["free"] for x in items)
    pct = round(u_all / t_all * 100, 1) if t_all else None
    return {
        "ok": True,
        "partitions": items,
        "total": t_all,
        "used": u_all,
        "free": f_all,
        "percent": pct,
    }


def _disk_info_fallback():
    """Linux 纯标准库回退: 解析 /proc/mounts + shutil.disk_usage。"""
    items = []
    try:
        mounts = []
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                cols = line.split()
                if len(cols) < 3:
                    continue
                dev, mp, fstype = cols[0], cols[1], cols[2]
                # 跳过明显伪文件系统
                if fstype in {
                    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup",
                    "cgroup2", "mqueue", "overlay", "squashfs", "debugfs",
                    "tracefs", "securityfs", "pstore", "bpf", "configfs",
                    "fusectl", "hugetlbfs", "ramfs", "autofs", "binfmt_misc",
                }:
                    continue
                if not mp.startswith("/") or mp in {"/dev", "/proc", "/sys"}:
                    continue
                mounts.append((dev, mp, fstype))
        for dev, mp, fstype in mounts:
            try:
                du = _shutil.disk_usage(mp)
            except Exception:
                continue
            items.append({
                "device": dev,
                "mountpoint": mp,
                "fstype": fstype,
                "total": du.total,
                "used": du.used,
                "free": du.free,
                "percent": round(du.used / du.total * 100, 1) if du.total else None,
            })
    except Exception:
        pass
    return items


def _boot_time():
    """系统启动时间(epoch 秒)。"""
    if _psutil is not None:
        return int(_psutil.boot_time())
    try:
        if _os.path.exists("/proc/stat"):
            with open("/proc/uptime", encoding="utf-8") as f:
                uptime = float(f.readline().split()[0])
            return int(time.time() - uptime)
    except Exception:
        pass
    return None


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
