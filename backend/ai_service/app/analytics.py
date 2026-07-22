"""AI 核心编排统计(§多意图 v1.0 + 统计系统约定)。

写共享 Redis(redis://redis:6379/0), 与业务端 analytics 同库。
因此业务端 analytics_snapshot 可直接读取这些键, 汇总进管理后台「系统分析」标签页。

统计维度:
- 编排: 总次数 / 策略分布 / 子任务数分布 / 成功率 / 总耗时
- 子任务: per-skill 成功/失败/拦截/跳过 + per-risk 分布 + 耗时 p50/p90/p99
- v0.9.0 新增: 修复(repair) / 蒸馏(distill) / 代码索引(code_index) / 精炼(refine) / 闲聊重答(chat_retry)

键前缀: an:orch:* / an:subtask:* / an:v090:*
"""

from __future__ import annotations

import logging
import uuid

from .config import settings


logger = logging.getLogger("ai_service.analytics")

LATENCY_MAX = 500
P_ORCH = "an:orch"
P_SUB = "an:subtask"
P_GEN = "an:generate"  # 后端核心总生成请求数
P_V090 = "an:v090"      # v0.9.0 新增功能统计

# 模块级懒加载 Redis 客户端
_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client if _redis_client is not False else None
    try:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(
            settings.redis_url,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    except Exception as e:  # 缺 redis 库或连不上 → 静默降级, 不阻塞主流程
        logger.warning("AI analytics redis 不可用, 统计降级: %s", e)
        _redis_client = False
    return _redis_client if _redis_client is not False else None


async def record_orchestration(
    split_count: int,
    strategy: str,
    duration_ms: float,
    success_rate: float,
) -> None:
    """一次多意图编排完成后的汇总统计。

    split_count: 子任务数; strategy: parallel|mixed;
    duration_ms: 整体编排耗时; success_rate: 成功子任务占比(0~1)。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_ORCH}:total", "count", 1)
        await r.hincrby(f"{P_ORCH}:strategy", strategy, 1)
        await r.zadd(f"{P_ORCH}:split_count", {uuid.uuid4().hex: split_count})
        await r.zremrangebyrank(f"{P_ORCH}:split_count", 0, -(LATENCY_MAX + 1))
        await r.zadd(f"{P_ORCH}:success_rate", {uuid.uuid4().hex: round(success_rate, 3)})
        await r.zremrangebyrank(f"{P_ORCH}:success_rate", 0, -(LATENCY_MAX + 1))
        await r.zadd(f"{P_ORCH}:duration", {uuid.uuid4().hex: duration_ms})
        await r.zremrangebyrank(f"{P_ORCH}:duration", 0, -(LATENCY_MAX + 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_orchestration failed: %s", e)


async def record_generate_request() -> None:
    """AI 核心收到的总生成请求数(含单意图 + 多意图), 独立于编排统计(an:orch)。

    反映 AI 核心真实负载(编排统计仅覆盖 split 决策)。业务端 analytics_snapshot
    读取该键并入 orchestration 块展示。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_GEN}:total", "count", 1)
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_generate_request failed: %s", e)


async def record_sub_task(
    skill: str,
    status: str,
    risk_level: str,
    duration_ms: float,
) -> None:
    """单个子任务完成后的统计(skill / 状态 / 风险 / 耗时)。

    status ∈ {done, failed, blocked, skipped}
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_SUB}:total", "count", 1)
        await r.hincrby(f"{P_SUB}:skill:{skill}", status, 1)
        await r.hincrby(f"{P_SUB}:status", status, 1)
        await r.hincrby(f"{P_SUB}:risk", risk_level, 1)
        zkey = f"{P_SUB}:duration"
        await r.zadd(zkey, {uuid.uuid4().hex: duration_ms})
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX + 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_sub_task failed: %s", e)


async def orchestration_stats() -> dict:
    """读取并聚合编排统计, 供业务端 analytics_snapshot 调用(跨服务同 Redis)。"""
    try:
        r = _get_redis()
        if r is None:
            return {"total": 0, "available": False}

        total = int((await r.hget(f"{P_ORCH}:total", "count")) or 0)
        if total == 0:
            return {"total": 0, "available": True}

        strategy = {k: int(v) for k, v in (await r.hgetall(f"{P_ORCH}:strategy") or {}).items()}

        async def _pct(zkey: str) -> dict:
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

        split_count = await _pct(f"{P_ORCH}:split_count")
        success_rate = await _pct(f"{P_ORCH}:success_rate")
        duration = await _pct(f"{P_ORCH}:duration")

        total_sub = int((await r.hget(f"{P_SUB}:total", "count")) or 0)
        status_raw = await r.hgetall(f"{P_SUB}:status")
        status_dist = {k: int(v) for k, v in status_raw.items()}
        risk_raw = await r.hgetall(f"{P_SUB}:risk")
        risk_dist = {k: int(v) for k, v in risk_raw.items()}

        skill_keys = await r.keys(f"{P_SUB}:skill:*")
        skill_stats: dict = {}
        for k in skill_keys:
            key = k.decode() if isinstance(k, bytes) else k
            sk = key.replace(f"{P_SUB}:skill:", "")
            h = {kk: int(vv) for kk, vv in (await r.hgetall(key) or {}).items()}
            t = sum(h.values())
            ok = h.get("done", 0)
            if t > 0:
                skill_stats[sk] = {
                    "total": t,
                    "done": h.get("done", 0),
                    "failed": h.get("failed", 0),
                    "blocked": h.get("blocked", 0),
                    "skipped": h.get("skipped", 0),
                    "success_rate": round(ok / max(t, 1), 3),
                }
        sub_dur = await _pct(f"{P_SUB}:duration")

        return {
            "total": total,
            "available": True,
            "strategy_dist": strategy,
            "split_count": split_count,
            "success_rate": success_rate,
            "duration_ms": duration,
            "sub_tasks": {
                "total": total_sub,
                "status_dist": status_dist,
                "risk_dist": risk_dist,
                "per_skill": skill_stats,
                "duration_ms": sub_dur,
            },
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics orchestration_stats failed: %s", e)
        return {"total": 0, "available": True, "error": str(e)}


# ---- v0.9.0 新增功能统计 ----

async def _incr_v090(feature: str) -> None:
    """通用增量计数器(按功能名)。"""
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_V090}:total", "count", 1)
        await r.hincrby(f"{P_V090}:feature", feature, 1)
    except Exception:
        pass


async def record_repair(skill: str, rounds: int, success: bool) -> None:
    """修复闭环统计。rounds=实际修复轮数, success=最终是否通过。"""
    try:
        r = _get_redis()
        if r is None:
            return
        await _incr_v090("repair")
        await r.hincrby(f"{P_V090}:repair:{skill}", "success" if success else "failed", 1)
        await r.zadd(f"{P_V090}:repair:rounds", {uuid.uuid4().hex: rounds})
        await r.zremrangebyrank(f"{P_V090}:repair:rounds", 0, -(LATENCY_MAX + 1))
        logger.info("[统计] repair skill=%s rounds=%d success=%s", skill, rounds, success)
    except Exception as e:
        logger.debug("[统计] repair 失败: %s", e)


async def record_distill(cnt_project_mems: int, cnt_user_prefs: int) -> None:
    """蒸馏统计(每轮 done 触发一次)。"""
    try:
        await _incr_v090("distill")
        r = _get_redis()
        if r is None:
            return
        total = cnt_project_mems + cnt_user_prefs
        await r.zadd(f"{P_V090}:distill:items", {uuid.uuid4().hex: total})
        await r.zremrangebyrank(f"{P_V090}:distill:items", 0, -(LATENCY_MAX + 1))
    except Exception as e:
        logger.debug("[统计] distill 失败: %s", e)


async def record_code_index(chunks: int) -> None:
    """代码索引统计。"""
    try:
        await _incr_v090("code_index")
        r = _get_redis()
        if r is None:
            return
        await r.zadd(f"{P_V090}:code_index:chunks", {uuid.uuid4().hex: chunks})
        await r.zremrangebyrank(f"{P_V090}:code_index:chunks", 0, -(LATENCY_MAX + 1))
    except Exception as e:
        logger.debug("[统计] code_index 失败: %s", e)


async def record_refine(len_before: int, len_after: int) -> None:
    """L2 对话精炼统计。"""
    try:
        await _incr_v090("refine")
        r = _get_redis()
        if r is None:
            return
        ratio = round(len_after / max(len_before, 1), 3)
        await r.zadd(f"{P_V090}:refine:ratio", {uuid.uuid4().hex: ratio})
        await r.zremrangebyrank(f"{P_V090}:refine:ratio", 0, -(LATENCY_MAX + 1))
    except Exception as e:
        logger.debug("[统计] refine 失败: %s", e)


async def record_chat_retry(success: bool) -> None:
    """闲聊重答统计(Phase D)。"""
    try:
        await _incr_v090("chat_retry")
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_V090}:chat_retry", "success" if success else "failed", 1)
    except Exception as e:
        logger.debug("[统计] chat_retry 失败: %s", e)
