"""修复闭环(v0.9.0 Phase C): 消费阶段自评+QC结果 → 定位失败阶段 → 局部重启。

设计要点:
- 只重启失败阶段，保留上游产物
- 单阶段超时 30s，每 trace 最多 3 轮
- best-of-N：跨轮保留最高分产物
- 中断续跑：repair state 存 Redis，兼容 Stream XRANGE/XREAD
- 失败降级：超时/超轮 → 标记 needs_review 返回 best-of-N
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("ai_service.repair")


# ---- 数据结构 ----

@dataclass
class StageEval:
    """单阶段的自我评估(structured per-stage)。"""
    stage: str            # "planner"|"coder"|"reviewer"
    passed: bool
    scores: dict          # {correctness:8, completeness:7, ...}  1-10
    issues: list[str]     # 发现的问题列表
    artifact: str = ""    # 本阶段产物路径/摘要


@dataclass
class RepairState:
    """修复闭环状态(序列化到 Redis repair:{trace_id})。"""
    trace_id: str
    round: int = 0                  # 当前 repair 轮次(0=初始)
    failed_stage: str = ""          # 当前重试的阶段名
    done_stages: list[str] = field(default_factory=list)  # 已通过阶段
    stage_evals: dict[str, dict] = field(default_factory=dict)  # stage→eval(序列化)
    best_score: float = 0.0
    best_artifact: str = ""
    started_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps({
            "trace_id": self.trace_id, "round": self.round,
            "failed_stage": self.failed_stage, "done_stages": self.done_stages,
            "stage_evals": self.stage_evals,
            "best_score": self.best_score, "best_artifact": self.best_artifact,
            "started_at": self.started_at,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> RepairState:
        d = json.loads(data)
        return cls(**d)


# ---- Redis 持久化 ----

_REPAIR_TTL = 600  # repair 状态 10 分钟过期(足够重连窗口)


async def save_repair_state(state: RepairState) -> None:
    """保存 repair 状态到 Redis。"""
    try:
        from ..config import settings
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        await r.setex(f"repair:{state.trace_id}", _REPAIR_TTL, state.to_json())
    except Exception as e:
        logger.warning("[repair] 状态保存失败: %s", e)


async def load_repair_state(trace_id: str) -> RepairState | None:
    """从 Redis 加载 repair 状态(中断续跑入口)。"""
    try:
        from ..config import settings
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        val = await r.get(f"repair:{trace_id}")
        if val:
            return RepairState.from_json(val.decode() if isinstance(val, bytes) else val)
    except Exception as e:
        logger.warning("[repair] 状态加载失败: %s", e)
    return None


async def delete_repair_state(trace_id: str) -> None:
    """done 后清理 repair 状态。"""
    try:
        from ..config import settings
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        await r.delete(f"repair:{trace_id}")
    except Exception:
        pass


# ---- 失败定位 ----

# QC 维度 → 对应阶段映射(粗粒度定位)
_QC_TO_STAGE: dict[str, str] = {
    "correctness": "coder",       # 事实/逻辑错误 → coder 问题
    "completeness": "planner",    # 覆盖不全 → planner 需求遗漏
    "compliance": "planner",      # 不合规 → planner 约束未传递
    "readability": "coder",       # 代码可读 → coder 问题
    "efficiency": "coder",        # 冗余 → coder 问题
    "safety": "planner",          # 安全 → planner 约束不足
}


def locate_failed_stage(
    qc_dimensions: dict,
    stage_evals: dict[str, dict] | None = None,
    default: str = "coder",
) -> str:
    """定位失败阶段。

    策略: 取 QC 最低分维度 → 映射到阶段。若有 stage_evals(自评),
    结合自评 passed=False 的阶段交叉验证。
    """
    # 1. 自评直接标 failed 的阶段 → 最高优先级
    if stage_evals:
        for stage, ev in stage_evals.items():
            if not ev.get("passed", True):
                logger.info("[repair] 自评定位 failed_stage=%s", stage)
                return stage

    # 2. QC 最低分维度映射
    lowest_dim = None
    lowest_score = 10.0
    for dim, info in qc_dimensions.items():
        score = info.get("mean", 10.0)
        if score < lowest_score:
            lowest_score = score
            lowest_dim = dim

    if lowest_dim and lowest_dim in _QC_TO_STAGE and lowest_score < 6.0:
        stage = _QC_TO_STAGE[lowest_dim]
        logger.info("[repair] QC定位 failed_stage=%s dim=%s score=%.1f", stage, lowest_dim, lowest_score)
        return stage

    logger.info("[repair] 无法精确定位 → 默认 %s", default)
    return default


# ---- 修复编排 ----

async def repair_loop(
    trace_id: str,
    run_stage,             # async callable(stage_name, context) → StageEval
    stage_evals: dict[str, dict],
    qc_result: dict | None = None,
    max_rounds: int = 3,
    timeout: float = 30.0,
) -> tuple[dict[str, dict], dict | None]:
    """修复主循环：定位 → 只重启失败阶段 → best-of-N。

    Args:
        trace_id: 追踪 ID
        run_stage: 异步可调用，重跑指定阶段并返回 StageEval
        stage_evals: 当前所有阶段的评估 {stage: eval_dict}
        qc_result: QC 聚合结果(可选，用于定位)
        max_rounds: 最大修复轮数(默认 3)
        timeout: 单阶段超时秒数(默认 30)

    Returns:
        (更新后的 stage_evals, best-of-N 的 qc_result 或 None)
    """
    state = RepairState(trace_id=trace_id, started_at=time.time())
    for name, ev in stage_evals.items():
        state.stage_evals[name] = ev
        if ev.get("passed", True):
            state.done_stages.append(name)

    best_score = qc_result.get("overall", 0.0) if qc_result else 0.0

    for rnd in range(max_rounds):
        state.round = rnd + 1
        # 定位失败阶段
        failed = locate_failed_stage(
            qc_dimensions=qc_result.get("dimensions", {}) if qc_result else {},
            stage_evals=state.stage_evals,
        )
        state.failed_stage = failed
        await save_repair_state(state)

        logger.info("[repair] 第%d轮 重试=%s trace=%s", rnd + 1, failed, trace_id)

        try:
            result: StageEval = await asyncio.wait_for(
                run_stage(failed, {"repair_round": rnd + 1, "prev_issues": [
                    state.stage_evals.get(failed, {}).get("issues", [])
                ]}),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[repair] 阶段 %s 超时(%ss) trace=%s", failed, timeout, trace_id)
            continue  # 超时不消耗 round 资源? 不，已经消耗了

        # 更新评估
        ev_dict = {
            "stage": result.stage, "passed": result.passed,
            "scores": result.scores, "issues": result.issues,
        }
        state.stage_evals[failed] = ev_dict

        if result.passed:
            if failed not in state.done_stages:
                state.done_stages.append(failed)
            # best-of-N: 用 passed 轮的最高分
            overall = sum(result.scores.values()) / max(len(result.scores), 1) * 10
            if overall > best_score:
                best_score = overall
                state.best_artifact = result.artifact
                state.best_score = best_score
                logger.info("[repair] best-of-N 更新 score=%.1f", best_score)
            # 如果全 passed，提前退出
            if len(state.done_stages) >= len(state.stage_evals):
                logger.info("[repair] 所有阶段通过 trace=%s", trace_id)
                break
        else:
            logger.info("[repair] 第%d轮 仍未通过 stage=%s", rnd + 1, failed)

        await save_repair_state(state)

    await delete_repair_state(trace_id)
    return state.stage_evals, None  # caller 重新跑 QC
