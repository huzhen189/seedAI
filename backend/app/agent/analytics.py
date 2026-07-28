"""AI 核心编排与质量统计系统(§多意图 v1.0 + 统计系统约定)。

写共享 Redis(redis://redis:6379/0), 与业务端 analytics 同库。
因此业务端 analytics_snapshot 可直接读取这些键, 汇总进管理后台「系统分析」标签页。

──────────────────────────────────────────────────────────────────────
统计维度全景(键前缀 an:*):
──────────────────────────────────────────────────────────────────────
1. 生成负载        an:generate        —— AI 核心收到的总生成请求数(单+多意图) 〔与业务端共享〕
2. 编排            an:orch             —— 总次数 / 策略分布 / 子任务数 / 成功率 / 耗时 〔与业务端共享〕
3. 子任务          an:subtask          —— per-skill 成功·失败·拦截·跳过 + per-risk + 耗时 p50/p90/p99 〔与业务端共享〕
4. 意图识别        ai:intent           —— 分类总次数 / 决策分布 / 来源分布 / 置信度分布 / 成功率 / 耗时
5. 后置 QC 三裁判  ai:qc    (v1.2.3)   —— 整体评分 / 7 维均值 / 人工复核率 / 评委掉线率 / 安全风险分布
6. 生成内 Reviewer ai:rev   (v1.2.3)   —— per-skill 通过·失败 / 待复核率 / 失败原因分布 / 7 维均值
7. 安全网关        ai:safe  (v1.2.3)   —— 入口风险等级分布 / 拦截·放行 / 拦截原因分布
8. LLM Provider    ai:llm   (v1.2.3)   —— 每模型次数·成功·失败·错误类型·耗时 p50/p90/p99·Token 累计
9. v0.9.0 功能     an:v090             —— repair/distill/code_index/refine/chat_retry 功能使用量 〔与业务端共享〕
10. 多意图路由      ai:mi   (v1.2.5) —— A+B 路径分布(hybrid=方案B / llm=方案A 升级) / 升级率 / 子任务数 / 耗时

⚠️ 命名空间约定(避免与业务端 an:* 统计键冲突):
  - 标「与业务端共享」的 3 项(an:orch/an:subtask/an:generate/an:v090)由 AI 核心写入、业务端
    analytics_snapshot 直接读取聚合(历史契约, 保持不变)。
  - v1.2.3 新增的 意图/QC/Reviewer/安全/LLM 五类统计统一放在 **ai:** 命名空间,
    不与业务端既有的 an:qc(哈希聚合)/an:intent:*(路由级两级命中) 混淆(后者 schema 不同,
    同键会类型冲突 / 语义串味)。业务端如需展示, 单独读 ai:* 即可。

汇总入口: ai_stats_summary() 一次性聚合以上全部维度, 供「系统分析」标签页拉取。
──────────────────────────────────────────────────────────────────────

设计约定(全模块统一):
- 所有 record_* 均为「尽力而为」:Redis 不可用 / 写入异常 → 仅 logger.warning, 绝不抛错阻塞主流程。
- 耗时 / 分数类连续量统一用 zset 存储, 超限(默认 500 样本)按排名裁剪, 避免无限增长(防内存/磁盘泄露)。
- 计数类用 hash hincrby;分布类用 hash 多字段。
- 读取端(各 *stats() 函数)全部 try/except 兜底,Redis 缺失返回 {available:False} 或 {total:0}。
"""

from __future__ import annotations

import logging
import uuid

from .config import settings
from .scoring import SCORING_DIMENSIONS  # 7 维定义(单一来源), 供 QC/Reviewer 维度聚合复用


logger = logging.getLogger("ai_service.analytics")

# zset 连续量样本上限: 超过则按排名裁剪最旧的, 控制 Redis 内存占用(防泄露)
LATENCY_MAX = 500

# ── 键前缀(语义见模块 docstring) ──
P_ORCH = "an:orch"
P_SUB = "an:subtask"
P_GEN = "an:generate"   # 后端核心总生成请求数〔与业务端共享〕
P_V090 = "an:v090"      # v0.9.0 新增功能统计〔与业务端共享〕
P_INTENT = "ai:intent"  # v1.2.0 混合级联意图识别分类统计(ai: 命名空间, 避免与业务端 an:intent:* 冲突)
P_QC = "ai:qc"          # v1.2.3 后置三裁判 QC 评分统计
P_REV = "ai:rev"        # v1.2.3 生成内 Reviewer 自审统计
P_SAFE = "ai:safe"      # v1.2.3 入口安全网关统计
P_LLM = "ai:llm"        # v1.2.3 LLM Provider 调用统计
P_MI = "ai:mi"          # v1.2.5 多意图 A+B 路由路径统计(hybrid=方案B / llm=方案A 升级 / 占比)
P_ROLE = "ai:role"       # §4 角色编排:四角色(product/design/dev/qa)分发/状态/耗时统计

# 模块级懒加载 Redis 客户端(进程内单例, 连接失败降级为 None)
_redis_client = None


def _get_redis():
    """返回共享 Redis 客户端; 不可用(缺库/连不上)时降级为 None 并缓存, 避免每次重试。"""
    global _redis_client
    if _redis_client is not None:
        return _redis_client if _redis_client is not False else None
    try:
        import redis.asyncio as aioredis

        # 关键: 与 cache.py / queue.py 保持一致, 强制 RESP2(protocol=2)。
        # redis-py 默认走 RESP3 握手会先发 `HELLO`, 部分云 Redis(老版本/代理)
        # 不支持 HELLO → 直接 `unknown command HELLO ...`。
        # 这里用 protocol=2 避开 HELLO, 否则所有 AI 核心统计写入都会静默失败。
        _redis_client = aioredis.from_url(
            settings.redis_url,
            protocol=2,
            socket_connect_timeout=3,
            socket_timeout=3,
            health_check_interval=30,
            socket_keepalive=True,
            retry_on_timeout=True,
        )
    except Exception as e:  # 缺 redis 库或连不上 → 静默降级, 不阻塞主流程
        logger.warning("AI analytics redis 不可用, 统计降级: %s", e)
        _redis_client = False
    return _redis_client if _redis_client is not False else None


# ──────────────────────────────────────────────────────────────────────
# 通用聚合工具
# ──────────────────────────────────────────────────────────────────────

async def _pct_zset(r, zkey: str) -> dict:
    """通用 zset 百分位聚合: 返回 p50/p90/p99/avg/samples。

    用于所有「连续量」统计(耗时 / 分数 / 置信度 / 方差), 统一口径,
    避免每个 *stats() 重复实现。样本为 0 时返回全 0。
    """
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


# ──────────────────────────────────────────────────────────────────────
# 0.5 HTTP 接口延迟(R1): 需求端(AI 核心 7102)全部 HTTP 接口耗时
# ──────────────────────────────────────────────────────────────────────

async def record_api_latency(path: str, elapsed_ms: float) -> None:
    """记录 AI 核心(需求端)HTTP 接口耗时(R1), 写入 ai:api:latency:{path}。

    与业务端 stats:latency:* 对称; 业务端 snapshot 扫描 ai:api:latency:* 聚合进管理后台
    「运行指标」第二个 tab。注意 /generate 为 SSE 流式端点, 由中间件排除(不记入延迟)。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.lpush(f"ai:api:latency:{path}", str(round(elapsed_ms, 1)))
        await r.ltrim(f"ai:api:latency:{path}", 0, 99)
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_api_latency failed: %s", e)


# ──────────────────────────────────────────────────────────────────────
# 1. 编排 / 2. 子任务 / 生成负载  (既有, 保持契约不变)
# ──────────────────────────────────────────────────────────────────────

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
    读取该键并入编排块展示。
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

        split_count = await _pct_zset(r, f"{P_ORCH}:split_count")
        success_rate = await _pct_zset(r, f"{P_ORCH}:success_rate")
        duration = await _pct_zset(r, f"{P_ORCH}:duration")

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
        sub_dur = await _pct_zset(r, f"{P_SUB}:duration")

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


# ──────────────────────────────────────────────────────────────────────
# 4. 意图识别统计 (v1.2.0, 扩展置信度 v1.2.3)
# ──────────────────────────────────────────────────────────────────────

async def record_intent_classify(
    decision: str,
    source: str,
    duration_ms: float,
    success: bool = True,
    confidence: float | None = None,
) -> None:
    """混合级联意图识别(v1.2.0)每次分类的统计。

    decision ∈ {route, clarify, split, block}
    source   ∈ {selection, reset, superfast, novelty, llm_ruling, block}
    confidence: 意图置信度 0~1(可选; 仅部分分支可精确给出, 其余传 None 不写该维)
    与 cascade.py 的 observe_record 调用点一一对应, 保证可观测 + 可统计双轨。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_INTENT}:total", "count", 1)
        await r.hincrby(f"{P_INTENT}:decision", decision, 1)
        await r.hincrby(f"{P_INTENT}:source", source, 1)
        await r.hincrby(f"{P_INTENT}:success", "ok" if success else "fail", 1)
        if confidence is not None:
            await r.zadd(f"{P_INTENT}:confidence", {uuid.uuid4().hex: float(confidence)})
            await r.zremrangebyrank(f"{P_INTENT}:confidence", 0, -(LATENCY_MAX + 1))
        zkey = f"{P_INTENT}:duration"
        await r.zadd(zkey, {uuid.uuid4().hex: duration_ms})
        await r.zremrangebyrank(zkey, 0, -(LATENCY_MAX + 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_intent_classify failed: %s", e)


async def intent_stats() -> dict:
    """读取意图识别统计(决策分布 / 来源分布 / 成功率 / 置信度 / 耗时)。"""
    try:
        r = _get_redis()
        if r is None:
            return {"total": 0, "available": False}
        total = int((await r.hget(f"{P_INTENT}:total", "count")) or 0)
        if total == 0:
            return {"total": 0, "available": True}
        decision = {k: int(v) for k, v in (await r.hgetall(f"{P_INTENT}:decision") or {}).items()}
        source = {k: int(v) for k, v in (await r.hgetall(f"{P_INTENT}:source") or {}).items()}
        success = {k: int(v) for k, v in (await r.hgetall(f"{P_INTENT}:success") or {}).items()}
        confidence = await _pct_zset(r, f"{P_INTENT}:confidence")
        dur = await _pct_zset(r, f"{P_INTENT}:duration")
        return {
            "total": total, "available": True,
            "decision_dist": decision, "source_dist": source,
            "success_dist": success, "confidence": confidence, "duration_ms": dur,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics intent_stats failed: %s", e)
        return {"total": 0, "available": True, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# 10. 多意图 A+B 路由路径统计 (v1.2.5)
# ──────────────────────────────────────────────────────────────

async def record_multi_intent_path(
    source: str,
    escalated: bool,
    sub_task_count: int = 0,
    duration_ms: float = 0.0,
) -> None:
    """多意图 A+B 路由路径统计(v1.2.5)。

    仅在轻量门控通过(疑似多意图)后调用, 每次 recognize_intents 记一次。

    source ∈ {hybrid, llm}: 最终采用哪条路径的产出。
      - hybrid: 方案B(混合分层)结果被采用(B 直出, 或 B 失败回退)
      - llm:    方案A(LLM 深拆)升级成功被采用
    escalated: 是否发生过 B→A 升级(B 未拆出 ≥2 子任务 / 平均置信 < 阈值 → 升方案A)。
    sub_task_count: 最终子任务数(单意图回退为 0)。
    A/B 路径占比 = path_dist[hybrid] / (hybrid + llm), 见 multi_intent_stats()。
    所有记录「尽力而为」, Redis 缺失仅告警。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_MI}:total", "count", 1)
        if source:
            await r.hincrby(f"{P_MI}:path", source, 1)
        if escalated:
            await r.hincrby(f"{P_MI}:escalated", "count", 1)
        if sub_task_count:
            await r.zadd(f"{P_MI}:subtasks", {uuid.uuid4().hex: sub_task_count})
            await r.zremrangebyrank(f"{P_MI}:subtasks", 0, -(LATENCY_MAX + 1))
        if duration_ms:
            await r.zadd(f"{P_MI}:duration", {uuid.uuid4().hex: duration_ms})
            await r.zremrangebyrank(f"{P_MI}:duration", 0, -(LATENCY_MAX + 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_multi_intent_path failed: %s", e)


async def multi_intent_stats() -> dict:
    """读取多意图 A+B 路由统计(路径分布 / A·B 占比 / 升级率 / 子任务数 / 耗时)。"""
    try:
        r = _get_redis()
        if r is None:
            return {"total": 0, "available": False}
        total = int((await r.hget(f"{P_MI}:total", "count")) or 0)
        if total == 0:
            return {"total": 0, "available": True}
        path = {k: int(v) for k, v in (await r.hgetall(f"{P_MI}:path") or {}).items()}
        escalated = int((await r.hget(f"{P_MI}:escalated", "count")) or 0)
        subtasks = await _pct_zset(r, f"{P_MI}:subtasks")
        dur = await _pct_zset(r, f"{P_MI}:duration")
        hy = path.get("hybrid", 0)
        ll = path.get("llm", 0)
        ab = hy + ll
        ab_ratio = (
            {"hybrid": round(hy / ab, 3), "llm": round(ll / ab, 3)}
            if ab else {"hybrid": 0, "llm": 0}
        )
        return {
            "total": total, "available": True,
            "path_dist": path, "ab_ratio": ab_ratio,
            "escalated": escalated,
            "escalate_rate": round(escalated / max(total, 1), 3),
            "sub_task_count": subtasks, "duration_ms": dur,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics multi_intent_stats failed: %s", e)
        return {"total": 0, "available": True, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 11. §4 角色编排统计(四角色 product/design/dev/qa)
# ──────────────────────────────────────────────────────────────────────

async def record_role_dispatch(
    role: str,
    skill: str,
    status: str,
    duration_ms: float = 0.0,
) -> None:
    """§4 角色编排:每次由 RoleAgent 分发执行一次技能的统计。

    role   ∈ {product, design, dev, qa}
    skill  : 实际执行的技能名(agent_requirement / agent_design / agent_build ...)
    status ∈ {done, failed, blocked, skipped}
    键: ai:role:total / ai:role:role:{role}:{status} / ai:role:skill:{skill}:{status}
        / ai:role:duration(zset)
    与现有 an:subtask 互补:an:subtask 是「技能级」,ai:role:* 是「角色级」,
    便于观察 SOP 各阶段(产品→设计→开发→评审)的负载与成功率。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_ROLE}:total", "count", 1)
        if role:
            await r.hincrby(f"{P_ROLE}:role:{role}", status, 1)
        if skill:
            await r.hincrby(f"{P_ROLE}:skill:{skill}", status, 1)
        if duration_ms:
            await r.zadd(f"{P_ROLE}:duration", {uuid.uuid4().hex: duration_ms})
            await r.zremrangebyrank(f"{P_ROLE}:duration", 0, -(LATENCY_MAX + 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_role_dispatch failed: %s", e)


async def role_stats() -> dict:
    """读取 §4 角色编排统计(四角色状态分布 / per-skill 状态分布 / 耗时)。"""
    try:
        r = _get_redis()
        if r is None:
            return {"total": 0, "available": False}
        total = int((await r.hget(f"{P_ROLE}:total", "count")) or 0)
        if total == 0:
            return {"total": 0, "available": True}
        # 四角色状态分布
        roles: dict = {}
        for role in ("product", "design", "dev", "qa"):
            h = {k: int(v) for k, v in (await r.hgetall(f"{P_ROLE}:role:{role}") or {}).items()}
            if h:
                roles[role] = h
        # per-skill 状态分布
        skill_keys = await r.keys(f"{P_ROLE}:skill:*")
        skill_stats: dict = {}
        for k in skill_keys:
            key = k.decode() if isinstance(k, bytes) else k
            sk = key.replace(f"{P_ROLE}:skill:", "")
            h = {kk: int(vv) for kk, vv in (await r.hgetall(key) or {}).items()}
            skill_stats[sk] = h
        dur = await _pct_zset(r, f"{P_ROLE}:duration")
        return {"total": total, "available": True,
                "roles": roles, "per_skill": skill_stats, "duration_ms": dur}
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics role_stats failed: %s", e)
        return {"total": 0, "available": True, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 5. 后置 QC 统计 (v1.2.3; v2.3.0 起单裁判)
# ──────────────────────────────────────────────────────────────────────

async def record_qc(result: dict, duration_ms: float = 0.0) -> None:
    """后置 QC(单裁判 v2.3.0)每次运行的统计。

    result: run_qc() 返回的聚合 dict(见 qc.py)。提取:
      - overall 整体评分(zset → p50/p90/p99/avg, 看整体质量趋势)
      - 7 维每维 mean(zset → 看各维质量分布, 定位短板维度)
      - needs_review 计数(人工复核率 = needs_review / total)
      - partial 计数(评委掉线/失败占比, 反映模型可用性)
      - safety_risk 分布(low/medium/high/critical)
    失败仅告警跳过, 不阻塞主流程。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_QC}:total", "count", 1)
        overall = float(result.get("overall", 0) or 0)
        await r.zadd(f"{P_QC}:overall", {uuid.uuid4().hex: overall})
        await r.zremrangebyrank(f"{P_QC}:overall", 0, -(LATENCY_MAX + 1))
        if result.get("needs_review"):
            await r.hincrby(f"{P_QC}:needs_review", "count", 1)
        if result.get("partial"):
            await r.hincrby(f"{P_QC}:partial", "count", 1)
        risk = result.get("safety_risk", "low") or "low"
        await r.hincrby(f"{P_QC}:safety", risk, 1)
        for dim, payload in (result.get("dimensions") or {}).items():
            mean = float((payload or {}).get("mean", 0) or 0)
            await r.zadd(f"{P_QC}:dim:{dim}", {uuid.uuid4().hex: mean})
            await r.zremrangebyrank(f"{P_QC}:dim:{dim}", 0, -(LATENCY_MAX + 1))
        if duration_ms:
            await r.zadd(f"{P_QC}:duration", {uuid.uuid4().hex: duration_ms})
            await r.zremrangebyrank(f"{P_QC}:duration", 0, -(LATENCY_MAX + 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_qc failed: %s", e)


async def qc_stats() -> dict:
    """读取 QC(单裁判)统计(整体评分 / 7 维均值 / 复核率 / 掉线率 / 安全风险)。"""
    try:
        r = _get_redis()
        if r is None:
            return {"total": 0, "available": False}
        total = int((await r.hget(f"{P_QC}:total", "count")) or 0)
        if total == 0:
            return {"total": 0, "available": True}
        overall = await _pct_zset(r, f"{P_QC}:overall")
        needs = int((await r.hget(f"{P_QC}:needs_review", "count")) or 0)
        partial = int((await r.hget(f"{P_QC}:partial", "count")) or 0)
        safety = {k: int(v) for k, v in (await r.hgetall(f"{P_QC}:safety") or {}).items()}
        dur = await _pct_zset(r, f"{P_QC}:duration")
        dims = {d: await _pct_zset(r, f"{P_QC}:dim:{d}") for d in SCORING_DIMENSIONS}
        return {
            "total": total, "available": True, "overall": overall,
            "needs_review": needs,
            "needs_review_rate": round(needs / max(total, 1), 3),
            "partial": partial, "partial_rate": round(partial / max(total, 1), 3),
            "safety_dist": safety, "duration_ms": dur, "dimensions": dims,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics qc_stats failed: %s", e)
        return {"total": 0, "available": True, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 6. 生成内 Reviewer 自审统计 (v1.2.3)
# ──────────────────────────────────────────────────────────────────────

async def record_reviewer(
    skill: str,
    review: dict,
    reason: str = "ok",
    duration_ms: float = 0.0,
) -> None:
    """生成内 Reviewer 自审每次运行的统计。

    skill: 技能名(agent_build / agent_generate_site ...);
    review: _review() 返回值(含 passed / needs_review / scores);
    reason: 失败归类 —— 取值:
        static_html     静态分析: 缺 <html 根标签
        static_close_tag 静态分析: <script>/<style> 标签未闭合
        parse_fail      LLM 输出无法解析(JSON 脏输出)
        llm_fail        LLM 调用失败(模型不可用)
        llm_unpassed    LLM 判定未通过(内容缺陷)
        ok              通过
    统计: 总次数 / per-skill 通过·失败 / 待复核率 / 失败原因分布 / 7 维均值分布。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_REV}:total", "count", 1)
        passed = bool(review.get("passed"))
        await r.hincrby(f"{P_REV}:skill:{skill}", "passed" if passed else "failed", 1)
        if review.get("needs_review"):
            await r.hincrby(f"{P_REV}:needs_review", "count", 1)
        # 仅失败/待复核时记录失败原因, 便于定位高频缺陷类型
        if not passed or review.get("needs_review"):
            await r.hincrby(f"{P_REV}:reason", reason, 1)
        scores = review.get("scores") or {}
        for dim, val in scores.items():
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            await r.zadd(f"{P_REV}:dim:{dim}", {uuid.uuid4().hex: v})
            await r.zremrangebyrank(f"{P_REV}:dim:{dim}", 0, -(LATENCY_MAX + 1))
        if duration_ms:
            await r.zadd(f"{P_REV}:duration", {uuid.uuid4().hex: duration_ms})
            await r.zremrangebyrank(f"{P_REV}:duration", 0, -(LATENCY_MAX + 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_reviewer failed: %s", e)


async def reviewer_stats() -> dict:
    """读取 Reviewer 统计(per-skill 通过率 / 待复核率 / 失败原因 / 7 维均值)。"""
    try:
        r = _get_redis()
        if r is None:
            return {"total": 0, "available": False}
        total = int((await r.hget(f"{P_REV}:total", "count")) or 0)
        if total == 0:
            return {"total": 0, "available": True}
        skill_keys = await r.keys(f"{P_REV}:skill:*")
        per_skill: dict = {}
        for k in skill_keys:
            key = k.decode() if isinstance(k, bytes) else k
            sk = key.replace(f"{P_REV}:skill:", "")
            h = {kk: int(vv) for kk, vv in (await r.hgetall(key) or {}).items()}
            t = h.get("passed", 0) + h.get("failed", 0)
            per_skill[sk] = {
                "passed": h.get("passed", 0), "failed": h.get("failed", 0),
                "total": t, "pass_rate": round(h.get("passed", 0) / max(t, 1), 3),
            }
        needs = int((await r.hget(f"{P_REV}:needs_review", "count")) or 0)
        reason = {k: int(v) for k, v in (await r.hgetall(f"{P_REV}:reason") or {}).items()}
        dur = await _pct_zset(r, f"{P_REV}:duration")
        dims = {d: await _pct_zset(r, f"{P_REV}:dim:{d}") for d in SCORING_DIMENSIONS}
        return {
            "total": total, "available": True, "per_skill": per_skill,
            "needs_review": needs,
            "needs_review_rate": round(needs / max(total, 1), 3),
            "reason_dist": reason, "duration_ms": dur, "dimensions": dims,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics reviewer_stats failed: %s", e)
        return {"total": 0, "available": True, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 7. 安全网关统计 (v1.2.3)
# ──────────────────────────────────────────────────────────────────────

async def record_safety(risk_level: str, blocked: bool, reason: str = "") -> None:
    """入口安全网关统计(cascade 调用)。

    risk_level: low|medium|high|critical(来自 run_safety);
    blocked: 是否拦截(高危直接阻断);
    reason: 拦截原因(用于高频风险归类, 截断 60 字符)。
    仅记录「决策时刻」(每次分类一次), 不重复统计 QC 阶段的安全地板。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_SAFE}:total", "count", 1)
        await r.hincrby(f"{P_SAFE}:risk", risk_level or "low", 1)
        await r.hincrby(f"{P_SAFE}:outcome", "blocked" if blocked else "pass", 1)
        if blocked and reason:
            await r.hincrby(f"{P_SAFE}:reason", reason[:60], 1)
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_safety failed: %s", e)


async def safety_stats() -> dict:
    """读取安全网关统计(风险等级分布 / 拦截·放行 / 拦截原因)。"""
    try:
        r = _get_redis()
        if r is None:
            return {"total": 0, "available": False}
        total = int((await r.hget(f"{P_SAFE}:total", "count")) or 0)
        if total == 0:
            return {"total": 0, "available": True}
        risk = {k: int(v) for k, v in (await r.hgetall(f"{P_SAFE}:risk") or {}).items()}
        outcome = {k: int(v) for k, v in (await r.hgetall(f"{P_SAFE}:outcome") or {}).items()}
        reason = {k: int(v) for k, v in (await r.hgetall(f"{P_SAFE}:reason") or {}).items()}
        return {"total": total, "available": True,
                "risk_dist": risk, "outcome_dist": outcome, "reason_dist": reason}
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics safety_stats failed: %s", e)
        return {"total": 0, "available": True, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 8. LLM Provider 调用统计 (v1.2.3)
# ──────────────────────────────────────────────────────────────────────

async def record_llm_call(
    model: str,
    ok: bool,
    latency_ms: float = 0.0,
    error_type: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    """LLM Provider 调用统计(每模型)。

    覆盖: Reviewer/Planner(_chat 包装) + QC 评委(_judge_one) + 意图 LLM 终判(_llm_rule)。
    统计维度:
      - 次数 / 成功 / 失败(失败按 error_type 归类)
      - 耗时 zset(p50/p90/p99/avg)
      - Token 累计(prompt_tokens / completion_tokens, 有 usage 回传时记录)
    所有数值为「尽力而为」, 失败仅告警。
    """
    try:
        r = _get_redis()
        if r is None:
            return
        await r.hincrby(f"{P_LLM}:total", "count", 1)
        await r.hincrby(f"{P_LLM}:model:{model}", "total", 1)
        await r.hincrby(f"{P_LLM}:model:{model}", "ok" if ok else "fail", 1)
        if not ok and error_type:
            await r.hincrby(f"{P_LLM}:model:{model}:err", error_type[:60], 1)
        if latency_ms:
            await r.zadd(f"{P_LLM}:model:{model}:duration", {uuid.uuid4().hex: latency_ms})
            await r.zremrangebyrank(f"{P_LLM}:model:{model}:duration", 0, -(LATENCY_MAX + 1))
        if tokens_in or tokens_out:
            await r.hincrby(f"{P_LLM}:model:{model}:tok_in", "total", int(tokens_in))
            await r.hincrby(f"{P_LLM}:model:{model}:tok_out", "total", int(tokens_out))
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics record_llm_call failed: %s", e)


async def llm_stats() -> dict:
    """读取 LLM Provider 统计(每模型的次数/成功率/耗时/Token/错误分布)。"""
    try:
        r = _get_redis()
        if r is None:
            return {"total": 0, "available": False}
        total = int((await r.hget(f"{P_LLM}:total", "count")) or 0)
        if total == 0:
            return {"total": 0, "available": True}
        model_keys = await r.keys(f"{P_LLM}:model:*")
        models: dict = {}
        for k in model_keys:
            key = k.decode() if isinstance(k, bytes) else k
            m = key.replace(f"{P_LLM}:model:", "")
            if ":" in m:  # 子键: model:<m>:duration / :err / :tok_in / :tok_out
                base = m.split(":", 1)[0]
                entry = models.setdefault(base, {
                    "total": 0, "ok": 0, "fail": 0, "success_rate": 0.0,
                    "err_dist": {}, "duration_ms": {}, "tokens_in": 0, "tokens_out": 0,
                })
                if m.endswith(":duration"):
                    entry["duration_ms"] = await _pct_zset(r, key)
                elif m.endswith(":tok_in"):
                    entry["tokens_in"] = int((await r.hget(key, "total")) or 0)
                elif m.endswith(":tok_out"):
                    entry["tokens_out"] = int((await r.hget(key, "total")) or 0)
                elif m.endswith(":err"):
                    entry["err_dist"] = {kk: int(vv) for kk, vv in (await r.hgetall(key) or {}).items()}
                continue
            # 主键: model:<m>
            h = {kk: int(vv) for kk, vv in (await r.hgetall(key) or {}).items()}
            entry = models.setdefault(base if ":" in m else m, {
                "total": 0, "ok": 0, "fail": 0, "success_rate": 0.0,
                "err_dist": {}, "duration_ms": {}, "tokens_in": 0, "tokens_out": 0,
            })
            entry["total"] = h.get("total", 0)
            entry["ok"] = h.get("ok", 0)
            entry["fail"] = h.get("fail", 0)
            entry["success_rate"] = round(h.get("ok", 0) / max(h.get("total", 1), 1), 3)
        return {"total": total, "available": True, "models": models}
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics llm_stats failed: %s", e)
        return {"total": 0, "available": True, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 9. v0.9.0 新增功能统计 (既有)
# ──────────────────────────────────────────────────────────────────────

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


async def v090_stats() -> dict:
    """读取 v0.9.0 功能统计(功能分布 + 修复轮数分布)。"""
    try:
        r = _get_redis()
        if r is None:
            return {"total": 0, "available": False}
        total = int((await r.hget(f"{P_V090}:total", "count")) or 0)
        if total == 0:
            return {"total": 0, "available": True}
        feature = {k: int(v) for k, v in (await r.hgetall(f"{P_V090}:feature") or {}).items()}
        repair_rounds = await _pct_zset(r, f"{P_V090}:repair:rounds")
        return {"total": total, "available": True,
                "feature_dist": feature, "repair_rounds": repair_rounds}
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics v090_stats failed: %s", e)
        return {"total": 0, "available": True, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────
# 汇总入口
# ──────────────────────────────────────────────────────────────────────

async def _gen_total() -> dict:
    """生成负载总数(独立读取, 避免与编排统计耦合)。"""
    r = _get_redis()
    if r is None:
        return {"total": 0, "available": False}
    total = int((await r.hget(f"{P_GEN}:total", "count")) or 0)
    return {"total": total, "available": True}


async def ai_stats_summary() -> dict:
    """汇总 AI 核心全部统计维度, 供管理后台「系统分析」一次性拉取。

    包含: 生成负载 / 编排 / 子任务 / 意图识别 / 后置 QC / Reviewer / 安全网关 /
    LLM Provider / v0.9.0 功能。任意子模块异常均被兜底, 不影响整体返回。
    """
    try:
        return {
            "available": True,
            "generate": await _gen_total(),
            "orchestration": await orchestration_stats(),
            "intent": await intent_stats(),
            "multi_intent": await multi_intent_stats(),
            "qc": await qc_stats(),
            "reviewer": await reviewer_stats(),
            "safety": await safety_stats(),
            "llm": await llm_stats(),
            "v090": await v090_stats(),
            "role": await role_stats(),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("AI analytics ai_stats_summary failed: %s", e)
        return {"available": False, "error": str(e)}
