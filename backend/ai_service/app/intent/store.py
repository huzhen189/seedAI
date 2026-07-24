"""槽位跨轮记忆(混合级联 v1.2.0, 替代 SIR 的粘性信念 update_belief)。

设计要点(见方案文档 §优化点 2/4):
- 多轮一致性靠「显式上下文 + 持久化槽位」, 而非易出 bug 的算术信念融合。
- 每轮 classify_v3 把 LLM 抽取到的槽位 + 当前意图 + 澄清轮次写入 Redis(无 Redis 走进程内 dict)。
- 下游 LLM 终判会读回这些槽位作为『已收集信息』注入, 避免重复追问。

数据 schema(Redis JSON 字符串, key = intent:slots:{conversation_id}):
  {"intent_id": str, "slots": {key: value}, "clarify_rounds": int,
   "confidence": float, "updated_at": float}
"""

from __future__ import annotations

import json
import logging
import time

from ..analytics import _get_redis

logger = logging.getLogger("ai_service.intent.store")

_KEY_PREFIX = "intent:slots:"
_EMPTY = {"intent_id": "", "slots": {}, "clarify_rounds": 0, "confidence": 0.0, "updated_at": 0.0}

# 无 Redis 时的进程内兜底(单实例开发可用)
_local: dict[int, dict] = {}


def load_slots(conversation_id: int | None) -> dict:
    """读取会话槽位; 无 conv_id / 无记录 / 异常 → 返回空结构。"""
    if conversation_id is None:
        return dict(_EMPTY)
    try:
        r = _get_redis()
        if r is not None:
            raw = r.get(_KEY_PREFIX + str(conversation_id))
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                return data
        else:
            return dict(_local.get(conversation_id, _EMPTY))
    except Exception as e:  # pragma: no cover
        logger.debug("[槽位] 读取失败, 返回空: %s", e)
    return dict(_EMPTY)


def save_slots(conversation_id: int | None, data: dict) -> None:
    """持久化会话槽位(失败静默)。"""
    if conversation_id is None:
        return
    data = dict(data)
    data["updated_at"] = time.time()
    try:
        r = _get_redis()
        if r is not None:
            r.set(_KEY_PREFIX + str(conversation_id), json.dumps(data, ensure_ascii=False))
        else:
            _local[conversation_id] = data
    except Exception as e:  # pragma: no cover
        logger.debug("[槽位] 写入失败(忽略): %s", e)


def reset_slots(conversation_id: int | None) -> None:
    """清空会话槽位(RESET / 退出建站时调用)。"""
    if conversation_id is None:
        return
    try:
        r = _get_redis()
        if r is not None:
            r.delete(_KEY_PREFIX + str(conversation_id))
        else:
            _local.pop(conversation_id, None)
    except Exception as e:  # pragma: no cover
        logger.debug("[槽位] 重置失败(忽略): %s", e)
