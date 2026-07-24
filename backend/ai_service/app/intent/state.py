"""SIR 核心: 跨轮意图信念状态(IntentState) — Redis 持久化。

为什么需要它(相对现状/Plan C 的根本差异):
  现状与 Plan C 都是"每轮独立分类", 没有任何跨轮记忆。本模块把"对话正在
  朝哪个意图收敛"显式建模为一个可被持久化、可被后续轮次读取/累积的
  **信念状态(IntentState)**, 从而满足"哪怕多轮对话依然明确识别意图"的硬需求。

设计要点:
  - 数据存 Redis(key=intent_state:{conv_id}), TTL = 会话级(24h)。
  - 无 Redis / 连接失败时优雅降级: load 返回 None → 信念每轮从零开始
    (等价于退化成"每轮独立", 不比现状更差)。
  - 更新公式 update_belief 带"粘性": 同向证据逐轮加速收敛; 已收敛的
    build 遇到闲聊 aside 仍保持 ≥0.7, 不被打断。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("ai_service.intent.state")

# ── Redis 客户端(复用 analytics 的懒加载, 失败降级) ──
try:  # 延迟导入, 避免循环引用
    from ..analytics import _get_redis as _get_redis_client
except Exception:  # pragma: no cover
    _get_redis_client = None

KEY_PREFIX = "intent_state:"
_TTL_SECONDS = 24 * 3600  # 会话级 TTL

# 粘性系数: 先验基础权重。值越大, 历史信念越"顽固"。
STICK = 0.55

# 决策阈值(与文档 §3.4 对齐)
COMMIT_THRESHOLD = 0.70
CLARIFY_THRESHOLD = 0.40
CLARIFY_MAX_ROUNDS = 2


@dataclass
class IntentState:
    """跨轮意图信念状态。

    - belief_l1/l2: 当前收敛方向(build/site, chat/casual, ...)。
    - running_conf: 跨轮累积置信(0~1), 由 update_belief 单调/粘性演化。
    - specs: 已抽取的需求规格(跨轮累积, 如 {pages:3, tech:"React"})。
    - missing_specs: 仍缺失的关键规格(用于动态澄清追问)。
    - clarify_rounds: 已澄清轮次(≤2), 持久化以避免死循环。
    - trajectory: 每轮信号快照(供可观测 JSONL 复盘)。
    - reset_at: 最近一次 RESET 的时间戳(用于衰减判定)。
    """

    conv_id: int = 0
    belief_l1: str = "chat"
    belief_l2: str = "casual"
    running_conf: float = 0.0
    specs: dict = field(default_factory=dict)
    missing_specs: list = field(default_factory=list)
    clarify_rounds: int = 0
    trajectory: list = field(default_factory=list)
    updated_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_payload(cls, conv_id: int, data: dict) -> "IntentState":
        """从 dict 安全构造(忽略未知/过期字段, 防 schema 漂移崩溃)。"""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known and k != "conv_id"}
        return cls(conv_id=conv_id, **filtered)


def _key(conv_id: int) -> str:
    return f"{KEY_PREFIX}{conv_id}"


def _redis():
    if _get_redis_client is None:
        return None
    try:
        return _get_redis_client()
    except Exception as e:  # pragma: no cover
        logger.debug("[状态] redis 客户端获取失败: %s", e)
        return None


async def load_state(conv_id: int) -> Optional[IntentState]:
    """加载跨轮信念状态; 无 conv_id / 无 redis / 不存在 → 返回 None。"""
    if not conv_id:
        return None
    try:
        r = _redis()
        if r is None:
            return None
        raw = await r.get(_key(conv_id))
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        st = IntentState.from_payload(conv_id, data)
        logger.info("[状态] 加载 conv=%s belief=%s/%s conf=%.2f rounds=%d",
                    conv_id, st.belief_l1, st.belief_l2, st.running_conf, st.clarify_rounds)
        return st
    except Exception as e:  # pragma: no cover
        logger.debug("[状态] load_state 异常 conv=%s: %s", conv_id, e)
        return None


async def save_state(s: IntentState) -> None:
    """写回跨轮信念状态(Redis + TTL); 失败静默降级。"""
    s.updated_at = time.time()
    try:
        r = _redis()
        if r is None:
            return
        await r.set(_key(s.conv_id), s.to_json(), ex=_TTL_SECONDS)
        logger.info("[状态] 保存 conv=%s belief=%s/%s conf=%.2f rounds=%d",
                    s.conv_id, s.belief_l1, s.belief_l2, s.running_conf, s.clarify_rounds)
    except Exception as e:  # pragma: no cover
        logger.debug("[状态] save_state 异常 conv=%s: %s", s.conv_id, e)


async def reset_state(conv_id: int) -> Optional[IntentState]:
    """RESET: 删除信念状态并返回一个新的空状态(便于继续流转)。"""
    try:
        r = _redis()
        if r is not None:
            await r.delete(_key(conv_id))
    except Exception as e:  # pragma: no cover
        logger.debug("[状态] reset_state 异常 conv=%s: %s", conv_id, e)
    logger.info("[状态] RESET conv=%s", conv_id)
    return IntentState(conv_id=conv_id, updated_at=time.time())


def update_belief(prior: Optional[IntentState], cur_score: float,
                  cur_l1: str, cur_l2: str) -> tuple[float, str, str]:
    """粘性信念更新(核心公式, SIR 跨轮连续性的关键)。

    返回 (running_conf, belief_l1, belief_l2)。

    设计目标(相对文档初版公式的修正):
      1. 同向加速收敛: 未收敛且方向一致 → 正常粘性 STICK, running 逐轮上升。
      2. **强抗打断**: 先验已收敛(≥0.7)且本轮是低信号噪声(闲聊 aside / 低分)→
         至多回落 5%(running = max(cur, prior*0.95)), 保持 ≥0.7, 不被一条 aside 翻转。
         (这是 SIR 相对 Plan C/现状 的根本价值, 初版公式因误用 STICK 会跌破阈值, 已修正)
      3. 同向且已收敛: 维持高置信, 方向沿用更强证据一方。
      4. 未收敛且方向不同: 正常粘性, 方向取更强证据一方。
      5. 显式 RESET(用户说"随便聊聊")由 pipeline 单独处理, 不走本函数。

    说明: 当本轮是"强且明确的不同意图"(cur_score 高于 prior*0.95)时, 允许重定向
    (如用户明确"我们聊聊天吧"), 不无限压制; 仅抵抗低信号噪声。
    """
    if prior is None or prior.running_conf == 0:
        return cur_score, cur_l1, cur_l2

    p = prior.running_conf
    same_dir = (prior.belief_l1 == cur_l1)
    converged = p >= COMMIT_THRESHOLD
    noise = (cur_l1 in ("chat", "casual")) or (cur_score < 0.4)

    if converged and (noise or not same_dir):
        # 强抗打断: 已收敛信念只会被低信号噪声轻微撼动(至多 -5%)
        running = max(cur_score, p * 0.95)
        l1, l2 = prior.belief_l1, prior.belief_l2
        stick_used = 0.95
    elif same_dir:
        # 同向(含未收敛): 正常粘性加速收敛
        running = STICK * p + (1 - STICK) * cur_score
        l1, l2 = (cur_l1, cur_l2) if cur_score >= p else (prior.belief_l1, prior.belief_l2)
        stick_used = STICK
    else:
        # 未收敛且方向不同: 正常粘性, 方向取更强证据一方
        running = STICK * p + (1 - STICK) * cur_score
        l1, l2 = (cur_l1, cur_l2) if cur_score >= p else (prior.belief_l1, prior.belief_l2)
        stick_used = STICK

    running = max(0.0, min(1.0, running))

    logger.info("[信念] 更新 prior=%.2f(%s/%s) cur=%.2f(%s/%s) stick=%.2f → running=%.2f(%s/%s)",
                p, prior.belief_l1, prior.belief_l2,
                cur_score, cur_l1, cur_l2, stick_used, running, l1, l2)
    return running, l1, l2
