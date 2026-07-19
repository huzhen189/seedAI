"""统一分析统计栈(对应文档 §3.12 质量指标版)。

所有统计走 Redis 原子操作, 不依赖外部监控(Prometheus/Grafana):
- 意图分类: 命中率/准确率/有效率 per intent
- API 响应时间: p50/p90/p99 per endpoint
- Skill 成效: 成功/失败/中断 per skill + per intent
- 生成链路: 各阶段耗时分布(Planner/Coder/Reviewer/Preview)
- 前端性能: page_load / ttfb / dom_ready(客户端上报)

所有函数 fail-silent(fail-open), Redis 不可用时仅记录警告不抛异常。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Optional

from .cache import get_redis

logger = logging.getLogger("business.analytics")

# latency sorted set 保留最近 N 条样本
LATENCY_MAX_SAMPLES = 500

# Redis key 前缀
P_INTENT_HIT = "an:intent:hit"       # hset: {intent → count}
P_INTENT_TOTAL = "an:intent:total"    # hset: {intent → count}
P_SKILL_OK = "an:skill:ok"            # hset: {skill → count}
P_SKILL_FAIL = "an:skill:fail"        # hset: {skill → count}
P_SKILL_ABORT = "an:skill:abort"      # hset: {skill → count}
P_LATENCY = "an:latency"              # zset: score=ms, member=tid
P_FRONTEND = "an:frontend"            # zset: score=ms, member=tid


async def record_intent_result(intent: str, matched: bool) -> None:
    """记录意图分类结果: matched=True 表示用户确认/继续, False 表示用户取消/切换/不支持。"""
    try:
        r = await get_redis()
        await r.hincrby(P_INTENT_TOTAL, intent, 1)
        if matched:
            await r.hincrby(P_INTENT_HIT, intent, 1)
    except Exception as e:
        logger.warning("analytics record_intent_result failed: %s", e)


async def record_skill_outcome(skill: str, status: str, elapsed_ms: float) -> None:
    """记录 skill 执行结果: ok|fail|abort。"""
    try:
        r = await get_redis()
        if status == "ok":
            await r.hincrby(P_SKILL_OK, skill, 1)
        elif status == "fail":
            await r.hincrby(P_SKILL_FAIL, skill, 1)
        elif status == "abort":
            await r.hincrby(P_SKILL_ABORT, skill, 1)
        # 耗时分布
        zkey = f"{P_LATENCY}:{skill}"
        await r.zadd(zkey, {uuid.uuid4().hex: elapsed_ms})
        # 裁剪到 500 条
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_skill_outcome failed: %s", e)


async def record_api_latency(path: str, elapsed_ms: float) -> None:
    """记录 API 接口响应时间。"""
    try:
        r = await get_redis()
        zkey = f"{P_LATENCY}:api:{path}"
        await r.zadd(zkey, {uuid.uuid4().hex: elapsed_ms})
        # 按分钟 heatmap
        minute = int(time.time() // 60)
        mkey = f"{P_LATENCY}:api:{path}:{minute}"
        await r.hincrbyfloat(mkey, "sum", elapsed_ms)
        await r.hincrby(mkey, "count", 1)
        await r.expire(mkey, 900)  # 15min TTL
        # 裁剪
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_api_latency failed: %s", e)


async def record_frontend_perf(metric: str, value_ms: float) -> None:
    """客户端上报前端性能(page_load / ttfb / dom_ready)。"""
    try:
        r = await get_redis()
        ts = int(time.time())
        entry = json.dumps({"metric": metric, "ms": value_ms, "ts": ts}, ensure_ascii=False)
        await r.lpush("an:frontend:latest", entry)
        await r.ltrim("an:frontend:latest", 0, 199)  # 保留 200 条
        zkey = f"{P_FRONTEND}:{metric}"
        await r.zadd(zkey, {uuid.uuid4().hex: value_ms})
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_frontend_perf failed: %s", e)


async def record_gen_stage(stage: str, elapsed_ms: float) -> None:
    """记录生成各阶段耗时(Planner/Coder/Reviewer/Preview)。"""
    try:
        r = await get_redis()
        zkey = f"{P_LATENCY}:gen:{stage}"
        await r.zadd(zkey, {uuid.uuid4().hex: elapsed_ms})
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_gen_stage failed: %s", e)


# ---------- 查询(给 /admin/analytics 端点) ----------


def _percentile_from_zsetcount(count: int) -> tuple[int, int, int]:
    """从 zset 元素总数推算 p50/p90/p99 位置(基于 rank)。"""
    p50 = max(0, int(count * 0.50) - 1)
    p90 = max(0, int(count * 0.90) - 1)
    p99 = max(0, int(count * 0.99) - 1)
    return p50, p90, p99


async def _zset_percentiles(r, zkey: str) -> dict:
    """返回 {p50, p90, p99, avg, samples} 毫秒。"""
    count = await r.zcard(zkey)
    if count == 0:
        return {"p50": 0, "p90": 0, "p99": 0, "avg": 0, "samples": 0}
    p50_r, p90_r, p99_r = _percentile_from_zsetcount(count)
    # 并行取三个 percentile(redis-py 的 zrange 不支持一次多 rank, 逐个取)
    results = await r.zrange(zkey, p50_r, p50_r, withscores=True)
    p50 = results[0][1] if results else 0
    results = await r.zrange(zkey, p90_r, p90_r, withscores=True)
    p90 = results[0][1] if results else 0
    results = await r.zrange(zkey, p99_r, p99_r, withscores=True)
    p99 = results[0][1] if results else 0
    # 平均值(总分数 / 数量)
    all_scores = await r.zrange(zkey, 0, -1, withscores=True)
    avg = round(sum(s[1] for s in all_scores) / count, 1)
    return {"p50": round(p50, 1), "p90": round(p90, 1), "p99": round(p99, 1), "avg": avg, "samples": count}


async def _hset_percentages(r, okey: str, tkey: str) -> dict:
    """从两个 hset 计算每种 intent/skill 的成功率。"""
    ok = await r.hgetall(okey)
    tot = await r.hgetall(tkey)
    result: dict = {}
    all_keys = set(list(ok.keys()) + list(tot.keys()))
    for k in all_keys:
        o = int(ok.get(k, 0))
        t_val = int(tot.get(k, 0))
        result[k] = {
            "ok": o,
            "total": t_val,
            "rate": round(o / max(t_val, 1), 3),
        }
    return result


async def analytics_snapshot() -> dict:
    """全量分析快照(由 /admin/analytics 调用)。"""
    try:
        r = await get_redis()
        # 意图分类
        intent_stats = await _hset_percentages(r, P_INTENT_HIT, P_INTENT_TOTAL)
        # Skill 成效
        skill_ok = await r.hgetall(P_SKILL_OK)
        skill_fail = await r.hgetall(P_SKILL_FAIL)
        skill_abort = await r.hgetall(P_SKILL_ABORT)
        skills: dict = {}
        all_skills = set(list(skill_ok.keys()) + list(skill_fail.keys()) + list(skill_abort.keys()))
        for k in sorted(all_skills):
            o = int(skill_ok.get(k, 0))
            f = int(skill_fail.get(k, 0))
            a = int(skill_abort.get(k, 0))
            t_val = o + f + a
            skills[k] = {
                "ok": o,
                "fail": f,
                "abort": a,
                "total": t_val,
                "success_rate": round(o / max(t_val, 1), 3),
            }
        # 生成阶段耗时
        gen_stages: dict = {}
        for stage in ("enter_planner", "enter_coder", "enter_reviewer", "previewing"):
            gen_stages[stage] = await _zset_percentiles(r, f"{P_LATENCY}:gen:{stage}")
        # 热门 API 延迟(top 10)
        # 扫描所有 an:latency:api:* zset keys
        keys = await r.keys(f"{P_LATENCY}:api:*")
        api_latency: dict = {}
        for k in sorted(keys):
            path = k.replace(f"{P_LATENCY}:api:", "")
            if ":" in path:  # 跳过分针 key
                continue
            cnt = await r.zcard(k)
            if cnt >= 3:  # 至少 3 个样本才显示
                api_latency[path] = await _zset_percentiles(r, k)
        # 前端性能
        fe_perf: dict = {}
        for m in ("page_load", "ttfb", "dom_ready"):
            fe_perf[m] = await _zset_percentiles(r, f"{P_FRONTEND}:{m}")
        # 生成成功率(从 Trace 表)
        from sqlalchemy import select
        from .db import SessionLocal
        from .models import Trace
        async with SessionLocal() as s:
            traces = (await s.execute(select(Trace))).scalars().all()
            total = len(traces)
            done = sum(1 for t in traces if t.status == "done")
            gen_rate = round(done / max(total, 1), 3)
        return {
            "intent_stats": intent_stats,
            "skill_outcomes": skills,
            "gen_stages": gen_stages,
            "api_latency": api_latency,
            "frontend_perf": fe_perf,
            "generation_rate": {"total": total, "done": done, "rate": gen_rate},
        }
    except Exception as e:
        logger.warning("analytics_snapshot failed: %s", e)
        return {"error": str(e)}
