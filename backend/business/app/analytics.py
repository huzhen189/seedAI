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
P_QC = "an:qc"                # 后置三裁判质检(v0.8.5): 触发/均分/需复核/每维/风险
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


async def record_frontend_access(route: str) -> None:
    """前端页面访问统计(STAT-3): 按路由累计访问次数。"""
    try:
        r = await get_redis()
        await r.hincrby(f"{P_FRONTEND}:access", route or "unknown", 1)
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


async def analytics_snapshot() -> dict:
    try:
        r = await get_redis()

        # 两级意图统计
        intent_keys = await r.keys(f"{P_INTENT_TOTAL}:*")
        intent_stats: dict = {}
        for k in sorted(intent_keys):
            key = k.decode() if isinstance(k, bytes) else k
            prefix = key.replace(P_INTENT_TOTAL + ":", "")
            tot = int((await r.get(key)) or 0)
            hit_key = key.replace(P_INTENT_TOTAL, P_INTENT_HIT)
            hit = int((await r.get(hit_key)) or 0)
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
            gen_stages[stage] = await _zset_percentiles(r, f"{P_LATENCY}:gen:{stage}")

        # API 延迟
        api_keys = await r.keys(f"{P_LATENCY}:api:*")
        api_latency: dict = {}
        for k in sorted(api_keys):
            path = k.decode() if isinstance(k, bytes) else k
            path = path.replace(f"{P_LATENCY}:api:", "")
            if ":" in path:
                continue
            cnt = await r.zcard(k)
            if cnt >= 3:
                api_latency[path] = await _zset_percentiles(r, k)

        # 业务接口调用统计(STAT-2): 调用量 / 成功率 / 延迟, 合并自 record_api_call + record_api_latency
        api_calls: dict = {}
        total_keys = await r.hgetall(f"{P_API_CALLS}:total")
        ok_keys = await r.hgetall(f"{P_API_CALLS}:ok")
        fail_keys = await r.hgetall(f"{P_API_CALLS}:fail")
        for p in sorted(total_keys.keys()):
            pt = int(total_keys.get(p, 0))
            if pt < 3:  # 噪声过滤, 少于 3 次不展示
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
            orchestration["split_count"] = await _zset_percentiles(r, f"{P_ORCH}:split_count")
            orchestration["success_rate"] = await _zset_percentiles(r, f"{P_ORCH}:success_rate")
            orchestration["duration_ms"] = await _zset_percentiles(r, f"{P_ORCH}:duration")
            sub_total = int((await r.hget(f"{P_SUB}:total", "count")) or 0)
            sub_status = {k: int(v) for k, v in (await r.hgetall(f"{P_SUB}:status") or {}).items()}
            sub_risk = {k: int(v) for k, v in (await r.hgetall(f"{P_SUB}:risk") or {}).items()}
            sub_skill: dict = {}
            skill_keys = await r.keys(f"{P_SUB}:skill:*")
            for sk in skill_keys:
                sk_name = sk.decode() if isinstance(sk, bytes) else sk
                sk_name = sk_name.replace(f"{P_SUB}:skill:", "")
                sh = {kk: int(vv) for kk, vv in (await r.hgetall(sk) or {}).items()}
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
        # Agent 用量
        agent_total = await r.hgetall(f"{P_AGENT}:total")
        agent_ok = await r.hgetall(f"{P_AGENT}:ok")
        agent_stats: dict = {}
        for aid in set(list(agent_total.keys()) + list(agent_ok.keys())):
            t = int(agent_total.get(aid, 0))
            o = int(agent_ok.get(aid, 0))
            if t > 0:
                agent_stats[aid] = {"total": t, "ok": o, "rate": round(o / t, 3)}

        # 项目状态流转
        project_status = await r.hgetall(P_PROJECT_STATUS)
        status_stats = {k.decode() if isinstance(k, bytes) else k: int(v)
                        for k, v in (project_status or {}).items()}

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

        # WebLLM
        wllm_total = await r.hgetall(f"{P_WEBLLM}:total")
        wllm_ok = await r.hgetall(f"{P_WEBLLM}:ok")
        wllm_stats: dict = {}
        for act in set(list(wllm_total.keys()) + list(wllm_ok.keys())):
            t = int(wllm_total.get(act, 0))
            o = int(wllm_ok.get(act, 0))
            if t > 0:
                wllm_stats[act] = {"total": t, "ok": o, "rate": round(o / t, 3)}

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

        # 后置三裁判 QC(v0.8.5)
        qc_count = int((await r.hget(P_QC, "count")) or 0)
        qc_overall_sum = float((await r.hget(f"{P_QC}:overall", "sum")) or 0)
        qc_overall_cnt = int((await r.hget(f"{P_QC}:overall", "count")) or 0)
        qc_overall_avg = round(qc_overall_sum / qc_overall_cnt, 2) if qc_overall_cnt else None
        qc_review = int((await r.hget(P_QC, "review")) or 0)
        qc_review_rate = round(qc_review / max(qc_count, 1), 3) if qc_count else 0.0
        qc_safety_raw = await r.hgetall(f"{P_QC}:safety")
        qc_safety = {k.decode() if isinstance(k, bytes) else k: int(v)
                     for k, v in (qc_safety_raw or {}).items()}
        qc_per_dim: dict = {}
        for d in QC_DIMS:
            s = float((await r.hget(f"{P_QC}:dim:{d}", "sum")) or 0)
            c = int((await r.hget(f"{P_QC}:dim:{d}", "count")) or 0)
            qc_per_dim[d] = round(s / c, 2) if c else 0.0

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
            "generation_rate": {"total": total, "done": done, "rate": gen_rate},
            "error_stats": error_stats,
            "model_stats": model_stats,
            # v0.7.0 新增
            "agent_usage": agent_stats,
            "project_status": status_stats,
            "requirement_doc": {"ok": req_ok, "fail": req_fail,
                                "avg_pages": round(req_pages_sum / max(req_pages_cnt, 1), 1),
                                "avg_features": round(req_feat_sum / max(req_feat_cnt, 1), 1)},
            "context_detection": context_stats,
            "webllm": wllm_stats,
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
            # v0.8.5 新增: 后置三裁判 QC + 用户评价
            "qc": {
                "count": qc_count,
                "overall_avg": qc_overall_avg,
                "review_rate": qc_review_rate,
                "per_dim_avg": qc_per_dim,
                "safety_dist": qc_safety,
            },
            "feedback": {
                "count": fb_count,
                "avg_rating": fb_avg,
                "with_dims_rate": fb_dims_rate,
            },
        }
    except Exception as e:
        logger.warning("analytics_snapshot failed: %s", e)
        return {"error": str(e)}


# ---- v0.7.0 新增统计维度 ----
P_AGENT = "an:agent"            # per-agent 使用计数
P_PROJECT_STATUS = "an:project:status"  # 项目状态流转
P_REQUIREMENT = "an:requirement"  # 需求文档生成
P_CONTEXT = "an:context"          # 上下文检测方式
P_WEBLLM = "an:webllm"           # WebLLM 本地推理
P_COS = "an:cos"                  # COS 上传统计


async def record_cos_upload(ok: bool, size_bytes: int = 0, elapsed_ms: float = 0) -> None:
    """COS 上传统计: 成功/失败 + 文件大小 + 延迟"""
    try:
        r = await get_redis()
        await r.hincrby(P_COS, "ok" if ok else "fail", 1)
        if ok and size_bytes > 0:
            await r.hincrby(f"{P_COS}:total_bytes", "sum", size_bytes)
            await r.hincrby(f"{P_COS}:total_bytes", "count", 1)
        if elapsed_ms > 0:
            zkey = f"{P_COS}:latency"
            await r.zadd(zkey, {uuid.uuid4().hex: elapsed_ms})
            await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_cos_upload failed: %s", e)


async def record_agent_usage(agent_id: str, status: str, elapsed_ms: float = 0) -> None:
    """per-agent 使用: ok/fail/abort + 延迟采样"""
    try:
        r = await get_redis()
        await r.hincrby(f"{P_AGENT}:total", agent_id, 1)
        await r.hincrby(f"{P_AGENT}:{status}", agent_id, 1)
        if elapsed_ms > 0:
            zkey = f"{P_AGENT}:latency:{agent_id}"
            await r.zadd(zkey, {uuid.uuid4().hex: elapsed_ms})
            await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_agent_usage failed: %s", e)


async def record_project_status_transition(project_id: int, old_status: str, new_status: str) -> None:
    """项目状态流转计数"""
    try:
        r = await get_redis()
        transition = f"{old_status}→{new_status}"
        await r.hincrby(P_PROJECT_STATUS, transition, 1)
    except Exception as e:
        logger.warning("analytics record_project_status failed: %s", e)


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
    """上下文检测方式: webllm / chroma / none"""
    try:
        r = await get_redis()
        await r.hincrby(P_CONTEXT, source, 1)
    except Exception as e:
        logger.warning("analytics record_context_detection failed: %s", e)


async def record_webllm_usage(action: str, ok: bool, elapsed_ms: float = 0) -> None:
    """WebLLM 本地推理: classify/chat/context + 成功/失败 + 延迟"""
    try:
        r = await get_redis()
        await r.hincrby(f"{P_WEBLLM}:total", action, 1)
        await r.hincrby(f"{P_WEBLLM}:{'ok' if ok else 'fail'}", action, 1)
        if elapsed_ms > 0:
            zkey = f"{P_WEBLLM}:latency:{action}"
            await r.zadd(zkey, {uuid.uuid4().hex: elapsed_ms})
            await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX_SAMPLES + 1))
    except Exception as e:
        logger.warning("analytics record_webllm_usage failed: %s", e)


# ---- v0.8.5 新增统计维度: 后置三裁判 QC + 用户评价 ----
QC_DIMS = ("correctness", "completeness", "compliance", "efficiency", "readability", "safety")


async def record_qc(result: dict) -> None:
    """后置三裁判 QC 统计: 触发次数 / 整体均分 / 需复核率 / 每维均分 / 安全风险分布。

    result 即 ai_service qc.py 的聚合输出(dict), 字段:
      overall(float) / needs_review(bool) / safety_risk(str) / dimensions(dict[d][mean])。
    供管理后台「系统分析」标签页量化 QC 覆盖与质量趋势(满足"新增功能必接统计"约定)。"""
    try:
        r = await get_redis()
        await r.hincrby(P_QC, "count", 1)
        overall = result.get("overall")
        if overall is not None:
            await r.hincrbyfloat(f"{P_QC}:overall", "sum", float(overall))
            await r.hincrby(f"{P_QC}:overall", "count", 1)
        if result.get("needs_review"):
            await r.hincrby(P_QC, "review", 1)
        risk = result.get("safety_risk") or "low"
        await r.hincrby(f"{P_QC}:safety", risk, 1)
        dims = result.get("dimensions") or {}
        for d in QC_DIMS:
            dv = dims.get(d) or {}
            mean = dv.get("mean")
            if mean is not None and mean > 0:
                await r.hincrbyfloat(f"{P_QC}:dim:{d}", "sum", float(mean))
                await r.hincrby(f"{P_QC}:dim:{d}", "count", 1)
    except Exception as e:
        logger.warning("analytics record_qc failed: %s", e)


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
