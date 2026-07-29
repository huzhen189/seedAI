"""统一分析统计栈。

所有统计走 Redis 原子操作, 不依赖外部监控:
- 意图分类: 两级命中率(level1:level2, 含 industry)
- API 响应时间: p50/p90/p99 + 按分钟请求数
- Skill 成效: 成功/失败/中断 per skill + per level2
- 错误分类: 429限流/模型不可用/超时/上游错误/未分类
- 模型用量: per-model 成功/失败 + 按意图分布
- 用户活跃: DAU(按日去重) + 人均生成次数
- 生成链路: 各阶段耗时 + per-intent 分布
- 前端性能: page_load/ttfb/dom_ready"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from .cache import get_redis

logger = logging.getLogger("business.analytics")

LATENCY_MAX_SAMPLES = 500

P_INTENT_HIT = "an:intent:hit"
P_INTENT_TOTAL = "an:intent:total"
P_SKILL_OK = "an:skill:ok"
P_SKILL_FAIL = "an:skill:fail"
P_SKILL_ABORT = "an:skill:abort"
P_LATENCY = "an:latency"
P_FRONTEND = "an:frontend"
P_ERROR = "an:error"
P_USER = "an:user:dau"
P_MODEL = "an:model"
P_INTENT_DECISION = "an:intent:decision"  # 意图管道决策结果分布(block/confirm/options/route/fallback/unsupported)
P_FEEDBACK = "an:feedback"    # 用户评价(v0.8.5): 提交/均分/含六维子星占比
P_API_CALLS = "an:api:calls"  # 业务接口调用(STAT-2): 总次数/成功/失败/状态码分段
P_ORCH = "an:orch"            # AI 核心编排统计(STAT-1, 由 ai_service 写入同 Redis)
P_SUB = "an:subtask"          # AI 核心子任务统计(STAT-1)
P_GEN = "an:generate"         # AI 核心总生成请求数(STAT, 由 ai_service 写入同 Redis)


async def record_intent_result(level1: str, level2: str, matched: bool) -> None:
    try:
        r = await get_redis()
        key_total = f"{P_INTENT_TOTAL}:{level1}:{level2}"
        key_hit = f"{P_INTENT_HIT}:{level1}:{level2}"
        await r.hincrby(key_total, "count", 1)
        if matched:
            await r.hincrby(key_hit, "count", 1)
        await r.hincrby(f"{P_INTENT_TOTAL}:{level1}", "count", 1)
        if matched:
            await r.hincrby(f"{P_INTENT_HIT}:{level1}", "count", 1)
    except Exception as e:
        logger.warning("analytics record_intent_result failed: %s", e)


async def record_intent_decision(decision: str, skill: str = "", risk: str = "low") -> None:
    """统计意图管道决策结果(block/confirm/options/route/fallback/unsupported)。
    供管理后台「系统分析」展示安全拦截/二次确认/多选项触发频次与命中 skill。"""
    try:
        r = await get_redis()
        await r.hincrby(P_INTENT_DECISION, decision, 1)
        if skill:
            await r.hincrby(f"{P_INTENT_DECISION}:skill", skill, 1)
        if risk in ("high", "critical"):
            await r.hincrby(f"{P_INTENT_DECISION}:risk", risk, 1)
    except Exception as e:
        logger.warning("analytics record_intent_decision failed: %s", e)


async def record_skill_outcome(skill: str, status: str, elapsed_ms: float) -> None:
    try:
        r = await get_redis()
        if status == "ok":
            await r.hincrby(P_SKILL_OK, skill, 1)
        elif status == "fail":
            await r.hincrby(P_SKILL_FAIL, skill, 1)
        elif status == "abort":
            await r.hincrby(P_SKILL_ABORT, skill, 1)
        zkey = f"{P_LATENCY}:{skill}"
        await r.zadd(zkey, {uuid.uuid4().hex: elapsed_ms})
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_skill_outcome failed: %s", e)


async def record_model_detail(model: str, success: bool, intent: str = "unknown") -> None:
    """per-model 成功/失败计数 + 按意图分布"""
    try:
        r = await get_redis()
        await r.hincrby(f"{P_MODEL}:total", model, 1)
        if success:
            await r.hincrby(f"{P_MODEL}:ok", model, 1)
        else:
            await r.hincrby(f"{P_MODEL}:fail", model, 1)
        await r.hincrby(f"{P_MODEL}:by_intent:{model}", intent, 1)
    except Exception as e:
        logger.warning("analytics record_model_detail failed: %s", e)


async def record_error(error_type: str) -> None:
    """按错误类型计数: rate_limited/model_unavailable/upstream/timeout/unknown"""
    try:
        r = await get_redis()
        await r.hincrby(P_ERROR, error_type, 1)
        minute = int(time.time() // 60)
        await r.hincrby(f"{P_ERROR}:{minute}", error_type, 1)
        await r.expire(f"{P_ERROR}:{minute}", 3600)
    except Exception as e:
        logger.warning("analytics record_error failed: %s", e)


async def record_user_active(user_id: int) -> None:
    """DAU: 按天去重的活跃用户"""
    try:
        r = await get_redis()
        today = datetime.utcnow().strftime("%Y%m%d")
        await r.sadd(f"{P_USER}:{today}", str(user_id))
        await r.expire(f"{P_USER}:{today}", 86400 * 7)  # 保留7天
        await r.hincrby(f"{P_USER}:gen_count", str(user_id), 1)
    except Exception as e:
        logger.warning("analytics record_user_active failed: %s", e)


async def record_api_latency(path: str, elapsed_ms: float) -> None:
    try:
        r = await get_redis()
        zkey = f"{P_LATENCY}:api:{path}"
        await r.zadd(zkey, {uuid.uuid4().hex: elapsed_ms})
        minute = int(time.time() // 60)
        mkey = f"{P_LATENCY}:api:{path}:{minute}"
        await r.hincrbyfloat(mkey, "sum", elapsed_ms)
        await r.hincrby(mkey, "count", 1)
        await r.expire(mkey, 900)
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_api_latency failed: %s", e)


async def record_api_call(path: str, status_code: int) -> None:
    """业务接口调用统计(STAT-2): 总次数 + 成功(2xx/3xx)/失败(>=4xx) + 状态码分段(2xx/3xx/4xx/5xx)。

    供管理后台「系统分析」展示每个业务接口的调用量、成功率与延迟(与 record_api_latency 配合)。"""
    try:
        r = await get_redis()
        p = path.rstrip("/") or path
        await r.hincrby(f"{P_API_CALLS}:total", p, 1)
        if status_code >= 400:
            await r.hincrby(f"{P_API_CALLS}:fail", p, 1)
        else:
            await r.hincrby(f"{P_API_CALLS}:ok", p, 1)
        bucket = f"{status_code // 100}xx"
        await r.hincrby(f"{P_API_CALLS}:status:{p}", bucket, 1)
        minute = int(time.time() // 60)
        await r.hincrby(f"{P_API_CALLS}:rpm:{minute}", p, 1)
        await r.expire(f"{P_API_CALLS}:rpm:{minute}", 900)
    except Exception as e:
        logger.warning("analytics record_api_call failed: %s", e)


async def record_frontend_perf(metric: str, value_ms: float) -> None:
    try:
        r = await get_redis()
        ts = int(time.time())
        entry = json.dumps({"metric": metric, "ms": value_ms, "ts": ts}, ensure_ascii=False)
        await r.lpush("an:frontend:latest", entry)
        await r.ltrim("an:frontend:latest", 0, 199)
        zkey = f"{P_FRONTEND}:{metric}"
        await r.zadd(zkey, {uuid.uuid4().hex: value_ms})
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_frontend_perf failed: %s", e)


async def record_frontend_access(route: str, uid: str | None = None) -> None:
    """前端页面访问统计(STAT-3): 按路由累计访问次数(PV) + 匿名 UV。

    uid 为前端生成的匿名访客 ID(localStorage 持久化); 传入时计入 UV 集合
    (全局 an:frontend:uv + 当日 an:frontend:uv:{ymd}), 供「系统分析」展示独立访客数(R6)。
    """
    try:
        r = await get_redis()
        await r.hincrby(f"{P_FRONTEND}:access", route or "unknown", 1)
        if uid:
            ymd = time.strftime("%Y-%m-%d")
            await r.sadd(f"{P_FRONTEND}:uv", uid)
            await r.sadd(f"{P_FRONTEND}:uv:{ymd}", uid)
            await r.expire(f"{P_FRONTEND}:uv:{ymd}", 86400 * 8)
    except Exception as e:
        logger.warning("analytics record_frontend_access failed: %s", e)


async def record_frontend_click(label: str) -> None:
    """前端点击统计(STAT-3): 按 data-track 标签累计点击次数。"""
    try:
        r = await get_redis()
        await r.hincrby(f"{P_FRONTEND}:click", (label or "unknown")[:60], 1)
    except Exception as e:
        logger.warning("analytics record_frontend_click failed: %s", e)


async def record_gen_stage(stage: str, elapsed_ms: float) -> None:
    try:
        r = await get_redis()
        zkey = f"{P_LATENCY}:gen:{stage}"
        await r.zadd(zkey, {uuid.uuid4().hex: elapsed_ms})
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_gen_stage failed: %s", e)


# ---------- 查询 ----------


async def _zset_percentiles(r, zkey: str) -> dict:
    count = await r.zcard(zkey)
    if count == 0:
        return {"p50": 0, "p90": 0, "p99": 0, "avg": 0, "samples": 0}
    p50_r = max(0, int(count * 0.5) - 1)
    p90_r = max(0, int(count * 0.9) - 1)
    p99_r = max(0, int(count * 0.99) - 1)
    r50 = await r.zrange(zkey, p50_r, p50_r, withscores=True)
    r90 = await r.zrange(zkey, p90_r, p90_r, withscores=True)
    r99 = await r.zrange(zkey, p99_r, p99_r, withscores=True)
    p50 = r50[0][1] if r50 else 0
    p90 = r90[0][1] if r90 else 0
    p99 = r99[0][1] if r99 else 0
    all_scores = await r.zrange(zkey, 0, -1, withscores=True)
    avg = round(sum(s[1] for s in all_scores) / count, 1) if all_scores else 0
    return {"p50": round(p50, 1), "p90": round(p90, 1), "p99": round(p99, 1), "avg": avg, "samples": count}


# AI 核心 v1.2.3 新增统计命名空间(ai:*), 与业务端 an:* 互不冲突, 此处只读聚合供「系统分析」展示。
AI_SCORING_DIMS = ("correctness", "completeness", "readability", "compliance", "efficiency", "craft", "safety")


async def _read_ai_core(r) -> dict:
    """只读聚合 AI 核心 v1.2.3 的 ai:* 统计(意图/QC/Reviewer/安全/LLM)。

    各子块独立 try/except: 任一块缺失或异常都不影响其余块与整体快照返回。
    """
    out: dict = {}

    # 4) 意图识别(cascade 分类)
    try:
        it_total = int((await r.hget("ai:intent:total", "count")) or 0)
        if it_total:
            out["intent"] = {
                "total": it_total,
                "decision_dist": {k: int(v) for k, v in (await r.hgetall("ai:intent:decision") or {}).items()},
                "source_dist": {k: int(v) for k, v in (await r.hgetall("ai:intent:source") or {}).items()},
                "success_dist": {k: int(v) for k, v in (await r.hgetall("ai:intent:success") or {}).items()},
                "confidence": await _zset_percentiles(r, "ai:intent:confidence"),
                "duration_ms": await _zset_percentiles(r, "ai:intent:duration"),
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics _read_ai_core intent failed: %s", e)

    # 5) 后置 QC(单裁判, v2.3.0) —— 数据来自 MySQL qc_scores 表(不再走 Redis)
    try:
        from sqlalchemy import select as _select
        from .db import SessionLocal as _S
        from .models import QcScore as _QcScore

        async with _S() as _s:
            rows = (await _s.execute(_select(_QcScore))).scalars().all()
        total = len(rows)
        if total:
            needs = sum(1 for r0 in rows if r0.needs_review)
            partial = sum(1 for r0 in rows if r0.partial)
            # 整体均分: 取各记录 overall 平均(单裁判下 overall 即整体评分)
            overalls = [r0.overall for r0 in rows if r0.overall]
            overall_avg = round(sum(overalls) / len(overalls), 2) if overalls else 0.0
            # 每维均值: 从 result JSON 的 dimensions[dim].mean 聚合
            dims_acc: dict = {}
            for r0 in rows:
                res = r0.result or {}
                for d, payload in (res.get("dimensions") or {}).items():
                    v = (payload or {}).get("mean")
                    if isinstance(v, (int, float)):
                        dims_acc.setdefault(d, []).append(float(v))
            dims = {d: {
                "p50": 0, "p90": 0, "p99": 0,
                "avg": round(sum(vs) / len(vs), 2),
                "samples": len(vs),
            } for d, vs in dims_acc.items()}
            safety_dist: dict = {}
            for r0 in rows:
                safety_dist[r0.safety_risk] = safety_dist.get(r0.safety_risk, 0) + 1
            out["qc"] = {
                "total": total,
                "overall": {"p50": 0, "p90": 0, "p99": 0, "avg": overall_avg, "samples": total},
                "needs_review": needs,
                "needs_review_rate": round(needs / total, 3),
                "partial": partial,
                "partial_rate": round(partial / total, 3),
                "safety_dist": safety_dist,
                "dimensions": dims,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics _read_ai_core qc failed: %s", e)

    # 6) 生成内 Reviewer 自审
    try:
        rev_total = int((await r.hget("ai:rev:total", "count")) or 0)
        if rev_total:
            needs = int((await r.hget("ai:rev:needs_review", "count")) or 0)
            per_skill: dict = {}
            for sk in await r.keys("ai:rev:skill:*"):
                name = sk.decode() if isinstance(sk, bytes) else sk
                name = name.replace("ai:rev:skill:", "")
                h = {kk: int(vv) for kk, vv in (await r.hgetall(sk) or {}).items()}
                t = h.get("passed", 0) + h.get("failed", 0)
                if t > 0:
                    per_skill[name] = {"passed": h.get("passed", 0), "failed": h.get("failed", 0),
                                       "total": t, "pass_rate": round(h.get("passed", 0) / t, 3)}
            out["reviewer"] = {
                "total": rev_total,
                "per_skill": per_skill,
                "needs_review": needs,
                "needs_review_rate": round(needs / rev_total, 3),
                "reason_dist": {k: int(v) for k, v in (await r.hgetall("ai:rev:reason") or {}).items()},
                "dimensions": {d: await _zset_percentiles(r, f"ai:rev:dim:{d}") for d in AI_SCORING_DIMS},
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics _read_ai_core reviewer failed: %s", e)

    # 7) 安全网关
    try:
        safe_total = int((await r.hget("ai:safe:total", "count")) or 0)
        if safe_total:
            out["safety"] = {
                "total": safe_total,
                "risk_dist": {k: int(v) for k, v in (await r.hgetall("ai:safe:risk") or {}).items()},
                "outcome_dist": {k: int(v) for k, v in (await r.hgetall("ai:safe:outcome") or {}).items()},
                "reason_dist": {k: int(v) for k, v in (await r.hgetall("ai:safe:reason") or {}).items()},
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics _read_ai_core safety failed: %s", e)

    # 8) LLM Provider
    try:
        llm_total = int((await r.hget("ai:llm:total", "count")) or 0)
        if llm_total:
            models: dict = {}
            for k in await r.keys("ai:llm:model:*"):
                key = k.decode() if isinstance(k, bytes) else k
                m = key.replace("ai:llm:model:", "")
                if ":" in m:
                    base = m.split(":", 1)[0]
                    entry = models.setdefault(base, {"total": 0, "ok": 0, "fail": 0,
                                                     "err_dist": {}, "duration_ms": {}, "tokens_in": 0, "tokens_out": 0})
                    if m.endswith(":duration"):
                        entry["duration_ms"] = await _zset_percentiles(r, key)
                    elif m.endswith(":tok_in"):
                        entry["tokens_in"] = int((await r.hget(key, "total")) or 0)
                    elif m.endswith(":tok_out"):
                        entry["tokens_out"] = int((await r.hget(key, "total")) or 0)
                    elif m.endswith(":err"):
                        entry["err_dist"] = {kk: int(vv) for kk, vv in (await r.hgetall(key) or {}).items()}
                    continue
                h = {kk: int(vv) for kk, vv in (await r.hgetall(key) or {}).items()}
                entry = models.setdefault(m, {"total": 0, "ok": 0, "fail": 0,
                                              "err_dist": {}, "duration_ms": {}, "tokens_in": 0, "tokens_out": 0})
                entry["total"] = h.get("total", 0)
                entry["ok"] = h.get("ok", 0)
                entry["fail"] = h.get("fail", 0)
                entry["success_rate"] = round(h.get("ok", 0) / max(h.get("total", 1), 1), 3)
            out["llm"] = {"total": llm_total, "models": models}
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics _read_ai_core llm failed: %s", e)

    # 9) 多意图 A+B 路由路径(v1.2.5)
    try:
        mi_total = int((await r.hget("ai:mi:total", "count")) or 0)
        if mi_total:
            mi_path = {k: int(v) for k, v in (await r.hgetall("ai:mi:path") or {}).items()}
            mi_escalated = int((await r.hget("ai:mi:escalated", "count")) or 0)
            hy = mi_path.get("hybrid", 0)
            ll = mi_path.get("llm", 0)
            ab = hy + ll
            ab_ratio = (
                {"hybrid": round(hy / ab, 3), "llm": round(ll / ab, 3)}
                if ab else {"hybrid": 0, "llm": 0}
            )
            out["multi_intent"] = {
                "total": mi_total,
                "path_dist": mi_path,
                "ab_ratio": ab_ratio,
                "escalated": mi_escalated,
                "escalate_rate": round(mi_escalated / mi_total, 3),
                "sub_task_count": await _zset_percentiles(r, "ai:mi:subtasks"),
                "duration_ms": await _zset_percentiles(r, "ai:mi:duration"),
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics _read_ai_core multi_intent failed: %s", e)

    return out


# ── 安全读取辅助(防止单个键类型不符 WRONGTYPE 拖垮整个统计快照) ──
async def _safe_keys(r, pattern: str) -> list:
    """安全扫描键: 扫描异常不中断整体(降级为空列表)。"""
    try:
        return await r.keys(pattern)
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics: 扫描键 %s 失败, 跳过: %s", pattern, e)
        return []


async def _safe_hgetall(r, key) -> dict:
    """安全读取 hash: 类型不符(WRONGTYPE)或其它异常时返回 {} 并告警, 不中断快照。"""
    try:
        return (await r.hgetall(key)) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics: 读取 hash 键 %s 失败(可能类型不符), 跳过: %s", key, e)
        return {}


async def _safe_get(r, key, default=0):
    """安全读取单值(scalar): 异常时返回 default。"""
    try:
        v = await r.get(key)
        return int(v) if v is not None else default
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics: 读取键 %s 失败, 跳过: %s", key, e)
        return default


async def _safe_zset_pct(r, key) -> dict:
    """安全读取 zset 分位数: 非 zset(WRONGTYPE)或异常时返回零值, 不中断快照。"""
    try:
        return await _zset_percentiles(r, key)
    except Exception as e:  # noqa: BLE001
        logger.warning("analytics: 读取 zset 键 %s 失败(可能类型不符), 跳过: %s", key, e)
        return {"p50": 0, "p90": 0, "p99": 0, "avg": 0, "samples": 0}


async def analytics_snapshot() -> dict:
    try:
        r = await get_redis()
        # 两级意图统计
        intent_keys = await _safe_keys(r, f"{P_INTENT_TOTAL}:*")
        intent_stats: dict = {}
        for k in sorted(intent_keys):
            key = k.decode() if isinstance(k, bytes) else k
            prefix = key.replace(P_INTENT_TOTAL + ":", "")
            tot = await _safe_get(r, key)
            hit_key = key.replace(P_INTENT_TOTAL, P_INTENT_HIT)
            hit = await _safe_get(r, hit_key)
            if tot > 0:
                intent_stats[prefix] = {"ok": hit, "total": tot, "rate": round(hit / tot, 3)}

        # Skill 成效
        skill_ok = await r.hgetall(P_SKILL_OK)
        skill_fail = await r.hgetall(P_SKILL_FAIL)
        skill_abort = await r.hgetall(P_SKILL_ABORT)
        skills: dict = {}
        all_skills = set(list(skill_ok.keys()) + list(skill_fail.keys()) + list(skill_abort.keys()))
        for k in sorted(all_skills):
            o, f, a = int(skill_ok.get(k, 0)), int(skill_fail.get(k, 0)), int(skill_abort.get(k, 0))
            t = o + f + a
            if t > 0:
                skills[k] = {"ok": o, "fail": f, "abort": a, "total": t, "success_rate": round(o / max(t, 1), 3)}

        # 错误分布
        errors = await r.hgetall(P_ERROR)
        error_stats = {k: int(v) for k, v in (errors or {}).items()}

        # 模型用量详情
        model_total = await r.hgetall(f"{P_MODEL}:total")
        model_ok = await r.hgetall(f"{P_MODEL}:ok")
        model_fail = await r.hgetall(f"{P_MODEL}:fail")
        model_stats: dict = {}
        for m_id in set(list(model_total.keys()) + list(model_ok.keys()) + list(model_fail.keys())):
            t = int(model_total.get(m_id, 0))
            o = int(model_ok.get(m_id, 0))
            f = int(model_fail.get(m_id, 0))
            if t > 0:
                model_stats[m_id] = {"total": t, "ok": o, "fail": f, "rate": round(o / t, 3)}

        # 生成阶段耗时
        gen_stages: dict = {}
        for stage in ("enter_planner", "enter_coder", "enter_reviewer", "previewing"):
            gen_stages[stage] = await _safe_zset_pct(r, f"{P_LATENCY}:gen:{stage}")

        # API 延迟
        api_keys = await _safe_keys(r, f"{P_LATENCY}:api:*")
        api_latency: dict = {}
        for k in sorted(api_keys):
            path = k.decode() if isinstance(k, bytes) else k
            path = path.replace(f"{P_LATENCY}:api:", "")
            if ":" in path:
                continue
            cnt = 0
            try:
                cnt = await r.zcard(k)
            except Exception as e:  # noqa: BLE001
                logger.warning("analytics: zcard 键 %s 失败(可能类型不符), 跳过: %s", path, e)
                continue
            if cnt >= 3:
                api_latency[path] = await _safe_zset_pct(r, k)

        # 业务接口调用统计(STAT-2): 调用量 / 成功率 / 延迟, 合并自 record_api_call + record_api_latency
        api_calls: dict = {}
        total_keys = await r.hgetall(f"{P_API_CALLS}:total")
        ok_keys = await r.hgetall(f"{P_API_CALLS}:ok")
        fail_keys = await r.hgetall(f"{P_API_CALLS}:fail")
        for p in sorted(total_keys.keys()):
            pt = int(total_keys.get(p, 0))
            if pt < 1:  # v0.9.0: 降低噪��过滤(原<3在开发阶段看不到数据)
                continue
            po = int(ok_keys.get(p, 0))
            pf = int(fail_keys.get(p, 0))
            api_calls[p] = {
                "total": pt,
                "ok": po,
                "fail": pf,
                "success_rate": round(po / max(pt, 1), 3),
                "latency": api_latency.get(p, {"p50": 0, "p90": 0, "p99": 0, "avg": 0, "samples": 0}),
            }

        # AI 核心编排统计(STAT-1, 由 ai_service 写入同 Redis, 此处只读聚合)
        orch_total = int((await r.hget(f"{P_ORCH}:total", "count")) or 0)
        # 后端核心总生成请求数(独立于编排统计, 反映 AI 核心真实负载)
        gen_total = int((await r.hget(f"{P_GEN}:total", "count")) or 0)
        orchestration: dict = {
            "total": orch_total,
            "available": orch_total > 0,
            "ai_core_requests": gen_total,
        }
        if orch_total > 0:
            strategy_raw = await r.hgetall(f"{P_ORCH}:strategy")
            orchestration["strategy_dist"] = {k: int(v) for k, v in strategy_raw.items()}
            orchestration["split_count"] = await _safe_zset_pct(r, f"{P_ORCH}:split_count")
            orchestration["success_rate"] = await _safe_zset_pct(r, f"{P_ORCH}:success_rate")
            orchestration["duration_ms"] = await _safe_zset_pct(r, f"{P_ORCH}:duration")
            sub_total = int((await r.hget(f"{P_SUB}:total", "count")) or 0)
            sub_status = {k: int(v) for k, v in (await r.hgetall(f"{P_SUB}:status") or {}).items()}
            sub_risk = {k: int(v) for k, v in (await r.hgetall(f"{P_SUB}:risk") or {}).items()}
            sub_skill: dict = {}
            skill_keys = await _safe_keys(r, f"{P_SUB}:skill:*")
            for sk in skill_keys:
                sk_name = sk.decode() if isinstance(sk, bytes) else sk
                sk_name = sk_name.replace(f"{P_SUB}:skill:", "")
                sh = {kk: int(vv) for kk, vv in (await _safe_hgetall(r, sk)).items()}
                st = sum(sh.values())
                if st > 0:
                    sub_skill[sk_name] = {
                        "total": st, "done": sh.get("done", 0), "failed": sh.get("failed", 0),
                        "blocked": sh.get("blocked", 0), "skipped": sh.get("skipped", 0),
                        "success_rate": round(sh.get("done", 0) / max(st, 1), 3),
                    }
            orchestration["sub_tasks"] = {
                "total": sub_total,
                "status_dist": sub_status,
                "risk_dist": sub_risk,
                "per_skill": sub_skill,
                "duration_ms": await _zset_percentiles(r, f"{P_SUB}:duration"),
            }

        # 前端性能
        fe_perf = {m: await _zset_percentiles(r, f"{P_FRONTEND}:{m}") for m in ("page_load", "ttfb", "dom_ready")}

        # 前端访问 / 点击(STAT-3)
        fe_access_raw = await r.hgetall(f"{P_FRONTEND}:access")
        frontend_access = {k: int(v) for k, v in fe_access_raw.items()}
        fe_click_raw = await r.hgetall(f"{P_FRONTEND}:click")
        frontend_clicks = {
            k: int(v) for k, v in sorted(fe_click_raw.items(), key=lambda x: int(x[1]), reverse=True)[:20]
        }
        # 匿名 UV(累计独立访客 + 今日独立访客)(R6)
        ymd = time.strftime("%Y-%m-%d")
        frontend_uv = {
            "total": await r.scard(f"{P_FRONTEND}:uv"),
            "today": await r.scard(f"{P_FRONTEND}:uv:{ymd}"),
        }

        # DAU(今天 + 昨天)
        today = datetime.utcnow().strftime("%Y%m%d")
        dau_today = await r.scard(f"{P_USER}:{today}")
        dau_yesterday = 0  # 简化, 后续可用前一天日期

        # 人均生成
        gen_counts = await r.hgetall(f"{P_USER}:gen_count")
        active_users = len(gen_counts)
        total_gens = sum(int(v) for v in gen_counts.values())
        avg_gens = round(total_gens / max(active_users, 1), 1)

        # 生成成功率(Trace 表)
        from sqlalchemy import select
        from .db import SessionLocal
        from .models import Trace
        async with SessionLocal() as s:
            traces = (await s.execute(select(Trace))).scalars().all()
            total = len(traces)
            done = sum(1 for t in traces if t.status == "done")
            gen_rate = round(done / max(total, 1), 3)

        # ---- v0.7.0 新增统计 ----
        # 需求文档
        req_ok = int((await r.hget(P_REQUIREMENT, "ok")) or 0)
        req_fail = int((await r.hget(P_REQUIREMENT, "fail")) or 0)
        req_pages_sum = int((await r.hget(f"{P_REQUIREMENT}:pages_avg", "sum")) or 0)
        req_pages_cnt = int((await r.hget(f"{P_REQUIREMENT}:pages_avg", "count")) or 0)
        req_feat_sum = int((await r.hget(f"{P_REQUIREMENT}:features_avg", "sum")) or 0)
        req_feat_cnt = int((await r.hget(f"{P_REQUIREMENT}:features_avg", "count")) or 0)

        # 上下文检测
        ctx_stats = await r.hgetall(P_CONTEXT)
        context_stats = {k.decode() if isinstance(k, bytes) else k: int(v)
                         for k, v in (ctx_stats or {}).items()}

        # 意图决策分布(v0.8.1: 安全拦截/二次确认/多选项/路由)
        decision_raw = await r.hgetall(P_INTENT_DECISION)
        decision_stats = {k.decode() if isinstance(k, bytes) else k: int(v)
                          for k, v in (decision_raw or {}).items()}
        decision_skill_raw = await r.hgetall(f"{P_INTENT_DECISION}:skill")
        decision_skill_stats = {k.decode() if isinstance(k, bytes) else k: int(v)
                                for k, v in (decision_skill_raw or {}).items()}
        decision_risk_raw = await r.hgetall(f"{P_INTENT_DECISION}:risk")
        decision_risk_stats = {k.decode() if isinstance(k, bytes) else k: int(v)
                               for k, v in (decision_risk_raw or {}).items()}

        # 注: 后置 QC 已不再写入 Redis(无性能考量), 由 ai_core.qc 改读 MySQL qc_scores(见 _read_ai_core)。
        # 此处旧的 an:qc 聚合块已移除(前端从未消费顶层 qc 字段, 仅消费 ai_core.qc)。

        # 用户评价(v0.8.5, 含六维子星)
        fb_count = int((await r.hget(P_FEEDBACK, "count")) or 0)
        fb_rating_sum = int((await r.hget(P_FEEDBACK, "rating_sum")) or 0)
        fb_rating_cnt = int((await r.hget(P_FEEDBACK, "rating_count")) or 0)
        fb_avg = round(fb_rating_sum / fb_rating_cnt, 2) if fb_rating_cnt else None
        fb_with_dims = int((await r.hget(P_FEEDBACK, "with_dims")) or 0)
        fb_dims_rate = round(fb_with_dims / max(fb_count, 1), 3) if fb_count else 0.0

        return {
            "intent_stats": intent_stats,
            "skill_outcomes": skills,
            "gen_stages": gen_stages,
            "api_latency": api_latency,
            "api_calls": api_calls,
            "orchestration": orchestration,
            "frontend_perf": fe_perf,
            "frontend_access": frontend_access,
            "frontend_clicks": frontend_clicks,
            "frontend_uv": frontend_uv,
            "generation_rate": {"total": total, "done": done, "rate": gen_rate},
            "error_stats": error_stats,
            "model_stats": model_stats,
            # v0.7.0 新增
            "requirement_doc": {"ok": req_ok, "fail": req_fail,
                                "avg_pages": round(req_pages_sum / max(req_pages_cnt, 1), 1),
                                "avg_features": round(req_feat_sum / max(req_feat_cnt, 1), 1)},
            "context_detection": context_stats,
            "intent_decisions": {
                "by_decision": decision_stats,
                "by_skill": decision_skill_stats,
                "by_risk": decision_risk_stats,
            },
            "user_stats": {
                "dau_today": dau_today,
                "active_users": active_users,
                "total_generations": total_gens,
                "avg_per_user": avg_gens,
            },
            # v0.8.5 新增: 用户评价(后置 QC 已迁移至 ai_core.qc, 读 MySQL qc_scores)
            "feedback": {
                "count": fb_count,
                "avg_rating": fb_avg,
                "with_dims_rate": fb_dims_rate,
            },
            # v0.9.0 新增: 修复/蒸馏/代码索引/精炼/闲聊重答
            "v090_features": {
                k.decode() if isinstance(k, bytes) else k: int(v)
                for k, v in ((await r.hgetall("an:v090:feature")) or {}).items()
            },
            "v090_summary_fallback": int((await r.hget("an:v090:summary_fallback", "count")) or 0),
            # v1.2.3 新增: AI 核心原生统计(意图/QC/Reviewer/安全/LLM), 独立 ai:* 命名空间
            "ai_core": await _read_ai_core(r),
        }
    except Exception as e:
        logger.warning("analytics_snapshot failed: %s", e)
        return {"error": str(e)}


# ---- v0.7.0 新增统计维度 ----
P_REQUIREMENT = "an:requirement"  # 需求文档生成
P_CONTEXT = "an:context"          # 上下文检测方式


async def record_requirement_doc(project_id: int, ok: bool, pages: int = 0, features: int = 0) -> None:
    """需求文档生成: 成功/失败 + 页面数/功能数"""
    try:
        r = await get_redis()
        await r.hincrby(P_REQUIREMENT, "ok" if ok else "fail", 1)
        if ok:
            await r.hincrby(f"{P_REQUIREMENT}:pages_avg", "sum", pages)
            await r.hincrby(f"{P_REQUIREMENT}:pages_avg", "count", 1)
            await r.hincrby(f"{P_REQUIREMENT}:features_avg", "sum", features)
            await r.hincrby(f"{P_REQUIREMENT}:features_avg", "count", 1)
    except Exception as e:
        logger.warning("analytics record_requirement_doc failed: %s", e)


async def record_context_detection(source: str) -> None:
    """上下文检测方式: chroma / none"""
    try:
        r = await get_redis()
        await r.hincrby(P_CONTEXT, source, 1)
    except Exception as e:
        logger.warning("analytics record_context_detection failed: %s", e)


# ---- 注: 后置 QC 统计已整体下线(无性能考量) ----
# 原 an:qc / ai:qc 的 Redis 写入(record_qc / qc_stats)均已移除;
# QC 评分数据现由 MySQL qc_scores 表承载, 后台「系统分析」QC 面板经
# analytics._read_ai_core 直接读取该表(见 ai_core.qc 分支)。


async def record_feedback(rating: int, has_dimensions: bool = False) -> None:
    """用户评价统计: 提交次数 / 平均评分 / 含六维子星占比。

    rating: 1-10 总评; has_dimensions: 是否携带六维子星(气泡内展开评价)。"""
    try:
        r = await get_redis()
        await r.hincrby(P_FEEDBACK, "count", 1)
        if rating is not None:
            await r.hincrby(P_FEEDBACK, "rating_sum", int(rating))
            await r.hincrby(P_FEEDBACK, "rating_count", 1)
        if has_dimensions:
            await r.hincrby(P_FEEDBACK, "with_dims", 1)
    except Exception as e:
        logger.warning("analytics record_feedback failed: %s", e)


# ---- v0.9.0 新增统计 ----

async def record_summary_fallback(conversation_id: int) -> None:
    """L1 摘要过期→MySQL 回退重压 次数(v0.9.0 P1)。"""
    try:
        r = await get_redis()
        await r.hincrby("an:v090:summary_fallback", "count", 1)
        await r.hincrby("an:v090:summary_fallback", f"conv:{conversation_id}", 1)
        logger.info("[统计] summary_fallback conv=%s", conversation_id)
    except Exception as e:
        logger.debug("[统计] summary_fallback 失败: %s", e)
