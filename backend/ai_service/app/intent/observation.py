"""SIR 可观测性: 每轮意图识别统一 JSONL 记录(见文档 §3.6 / 用户提案第②点)。

为什么: 现状只有 logger.info 文本日志, 无法系统化复盘误判、标校词表/权重。
本模块把每次意图识别的标准化快照 append 到 logs/intent_observations.jsonl,
字段对齐用户给的 schema(request_id 由 Worker 入口生成, 这里接收; tokens_used
取自 LLM response_metadata, 取不到则缺省 0; outcome 由 Worker 执行后异步回填)。

outcome 回填机制: 每条记录带 request_id, Worker 执行结束(成功/失败/用户退出)
后调用 mark_outcome(request_id, outcome) 在 JSONL 末尾追加一条
{"request_id":..., "outcome":"..."} 的薄记录(或维护一个并行 outcome 索引)。
为简单可靠, 这里用"追加薄记录"方式, 复盘时用 request_id 关联。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("ai_service.intent.observation")

# 日志落点: 与 ai_service.log 同级目录
_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
_LOG_PATH = os.path.join(_LOG_DIR, "intent_observations.jsonl")

# 内存去重标记, 避免同进程重复建目录(无状态)
_dir_ensured = False


def _ensure_dir() -> None:
    global _dir_ensured
    if _dir_ensured:
        return
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        _dir_ensured = True
    except Exception as e:  # pragma: no cover
        logger.warning("[可观测] 日志目录创建失败: %s", e)


def record(
    *,
    request_id: str,
    conversation_id: int | None,
    user_id,
    raw_input: str,
    llm_intent: str,
    llm_confidence: float,
    rules_triggered: list[str],
    belief_before: float,
    belief_after: float,
    decision: str,
    latency_ms: float,
    tokens_used: int = 0,
    specialist_routed: Optional[str] = None,
    outcome: str = "pending",
    extra: Optional[dict] = None,
) -> None:
    """追加一条意图识别观测 JSONL。失败静默(不阻塞主流程)。"""
    _ensure_dir()
    rec = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_id": request_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "raw_input": (raw_input or "")[:500],
        "llm_intent": llm_intent,
        "llm_confidence": round(float(llm_confidence), 3),
        "rules_triggered": rules_triggered or [],
        "belief_before": round(float(belief_before), 3),
        "belief_after": round(float(belief_after), 3),
        "decision": decision,
        "latency_ms": round(float(latency_ms), 1),
        "tokens_used": int(tokens_used),
        "specialist_routed": specialist_routed,
        "outcome": outcome,
    }
    if extra:
        rec["extra"] = extra
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # pragma: no cover
        logger.debug("[可观测] 写入失败: %s", e)


def mark_outcome(request_id: str, outcome: str) -> None:
    """异步回填一次识别的最终结果(成功/失败/用户退出/忽略)。"""
    _ensure_dir()
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "request_id": request_id,
                "outcome": outcome,
                "_type": "outcome",
            }, ensure_ascii=False) + "\n")
    except Exception as e:  # pragma: no cover
        logger.debug("[可观测] outcome 回填失败: %s", e)
