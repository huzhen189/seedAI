"""槽位跨轮记忆(混合级联 v1.2.0, 替代 SIR 的粘性信念 update_belief)。

设计要点:
- 多轮一致性靠「显式上下文 + 持久化槽位」, 而非易出 bug 的算术信念融合。
- 每轮 classify_v3 把 LLM 抽取到的槽位 + 当前意图 + 澄清轮次写入 Redis(无 Redis 走进程内 dict)。
- 下游 LLM 终判会读回这些槽位作为『已收集信息』注入, 避免重复追问。

数据 schema(Redis JSON 字符串, key = intent:slots:{conversation_id}):
  {"intent_id": str, "slots": {key: value}, "clarify_rounds": int,
   "confidence": float, "updated_at": float}

⚠️ 生产环境必须配置 Redis(_get_redis 返回非 None), 进程内兜底仅用于单实例开发。
   兜底 dict 已做上限 + TTL 淘汰, 防止长时间运行的进程内存泄露。
"""

from __future__ import annotations

import json
import logging
import time

from shared.config import settings  # 同步 Redis 需要 settings.redis_url(与 app/config.py 同路径)
from ..analytics import _get_redis


def _get_sync_redis():
    """返回同步 Redis 客户端(供本模块同步 load/save/reset 使用)。

    analytics._get_redis() 返回 asyncio 客户端, 在同步函数里无法 await,
    故此处独立建一个 redis-py(同步)连接, 复用同一 redis_url。失败降级为 None,
    上层走内存兜底。cascade 调用本模块时已用 asyncio.to_thread 包裹, 同步阻塞安全。
    """
    global _sync_redis
    if _sync_redis is not None:
        return _sync_redis if _sync_redis is not False else None
    try:
        import redis as _sync_redis_mod
        _sync_redis = _sync_redis_mod.from_url(
            settings.redis_url, decode_responses=True,
            protocol=2,  # 与 analytics 一致: 强制 RESP2, 避免 HELLO 握手被云 Redis 拒绝
            socket_connect_timeout=3, socket_timeout=3,
            health_check_interval=30, socket_keepalive=True,
        )
        # 探活一次避免惰性连接掩盖不可用
        _sync_redis.ping()
    except Exception as e:  # 缺库/连不上 → 降级内存兜底
        logger.debug("[槽位] 同步 Redis 不可用, 走内存兜底: %s", e)
        _sync_redis = False
    return _sync_redis if _sync_redis is not False else None


# ⚠️ 关键修复(D/#504 调试发现): 本模块 load/save/reset_slots 是同步函数,
# 但 analytics._get_redis() 返回的是 redis.asyncio 异步客户端 —— 在同步函数里
# 调 r.set/r.get 会返回未被 await 的协程, 写入静默失败, 导致跨轮 DST(意图记忆)
# 在真实 Redis 环境下完全失效(本地进程内兜底掩盖了该 bug)。
# 修复: 同步函数须使用同步 Redis 客户端; cascade 已用 asyncio.to_thread 包裹, 阻塞 I/O 不占事件循环。


logger = logging.getLogger("ai_service.intent.store")

_sync_redis = None  # 0/None=未初始化; False=不可用; 否则为同步客户端

_KEY_PREFIX = "intent:slots:"
_EMPTY = {"intent_id": "", "slots": {}, "clarify_rounds": 0, "confidence": 0.0, "updated_at": 0.0}

# 无 Redis 时的进程内兜底(单实例开发可用)
# 已做上限 + TTL 淘汰, 避免长生命周期进程内存泄露。
_local: dict[int, dict] = {}
_LOCAL_MAX = 2000
_LOCAL_TTL = 86400.0  # 24h


def _evict_local() -> None:
    """兜底 dict 淘汰: 先清过期(TTL), 仍超限再丢最旧(updated_at 最小)。"""
    now = time.time()
    expired = [cid for cid, d in _local.items() if now - d.get("updated_at", 0.0) > _LOCAL_TTL]
    for cid in expired:
        _local.pop(cid, None)
    if len(_local) >= _LOCAL_MAX:
        # 按 updated_at 升序丢最早的 20%
        n_drop = max(1, len(_local) // 5)
        oldest = sorted(_local.items(), key=lambda kv: kv[1].get("updated_at", 0.0))[:n_drop]
        for cid, _ in oldest:
            _local.pop(cid, None)


def load_slots(conversation_id: int | None) -> dict:
    """读取会话槽位; 无 conv_id / 无记录 / 异常 / 过期 → 返回空结构。"""
    if conversation_id is None:
        return dict(_EMPTY)
    try:
        r = _get_sync_redis()
        if r is not None:
            raw = r.get(_KEY_PREFIX + str(conversation_id))
            if raw:
                return json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        else:
            data = _local.get(conversation_id)
            if data is not None and time.time() - data.get("updated_at", 0.0) <= _LOCAL_TTL:
                return data
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
        r = _get_sync_redis()
        if r is not None:
            r.set(_KEY_PREFIX + str(conversation_id), json.dumps(data, ensure_ascii=False))
        else:
            _evict_local()
            _local[conversation_id] = data
    except Exception as e:  # pragma: no cover
        logger.debug("[槽位] 写入失败(忽略): %s", e)


def reset_slots(conversation_id: int | None) -> None:
    """清空会话槽位(RESET / 退出建站时调用)。"""
    if conversation_id is None:
        return
    try:
        r = _get_sync_redis()
        if r is not None:
            r.delete(_KEY_PREFIX + str(conversation_id))
        else:
            _local.pop(conversation_id, None)
    except Exception as e:  # pragma: no cover
        logger.debug("[槽位] 重置失败(忽略): %s", e)
