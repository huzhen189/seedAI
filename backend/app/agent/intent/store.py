"""槽位跨轮记忆(混合级联 v1.2.0, 替代 SIR 的粘性信念 update_belief)。

设计要点:
- 多轮一致性靠「显式上下文 + 持久化槽位」, 而非易出 bug 的算术信念融合。
- 每轮 classify_v3 把 LLM 抽取到的槽位 + 当前意图 + 澄清轮次写入存储。
- 下游 LLM 终判会读回这些槽位作为『已收集信息』注入, 避免重复追问。

数据 schema:
  Redis 热键(intent:slots:{conversation_id}, JSON 字符串):
    {"intent_id": str, "slots": {key: value}, "clarify_rounds": int,
     "confidence": float, "updated_at": float}
  MySQL 冷备份(intent_slots 表, 每行 = 一个会话):
    业务键 (user_id, project_id, conversation_id) 联合唯一, 主鍵自增 id;
    slots 列存上述 Redis 值。切会话天然隔离, reset 仅删当前会话一行。

可靠性(#511): Redis 为主(热, 零延迟), MySQL(intent_slots 表)为持久兜底(冷)。
Redis 重启/丢失时 load 自动从 MySQL 回源并回填 Redis; save 同步写 Redis +
异步语义落 MySQL(本模块在 asyncio.to_thread 里同步执行, 故用同步引擎)。
user_id/project_id 为 None 时跳过 MySQL(退化为纯 Redis), 不阻塞主流程。
"""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from shared.config import settings  # 同步 Redis/Mysql 需要 settings
from shared.models import IntentSlots
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


def _get_sync_db():
    """返回同步 SQLAlchemy 引擎(供本模块在 asyncio.to_thread 同步上下文里落 MySQL 冷备份)。

    db.SessionLocal 是异步引擎(async_sessionmaker), 无法在同步函数内 await;
    故此处独立建一个 pymysql 同步引擎, 复用 settings.database_url(把 aiomysql 换成 pymysql)。
    带 pool_pre_ping + pool_recycle 抵御云 MySQL 空闲 NAT 掐断(同 db.py 铁律)。
    失败降级为 None, 上层跳过 MySQL 冷备份(退化为纯 Redis)。
    """
    global _sync_engine
    if _sync_engine is not None:
        return _sync_engine if _sync_engine is not False else None
    try:
        u = settings.database_url
        if u.startswith("mysql+aiomysql://"):
            u = "mysql+pymysql://" + u[len("mysql+aiomysql://"):]
        elif u.startswith("mysql://"):
            u = "mysql+pymysql://" + u[len("mysql://"):]
        elif u.startswith("sqlite+aiosqlite://"):
            u = "sqlite://" + u[len("sqlite+aiosqlite://"):]
        _sync_engine = create_engine(
            u,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=10,
            connect_args={"connect_timeout": 5} if "mysql" in u else {},
        )
        # 探活一次避免惰性连接掩盖不可用
        with _sync_engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        logger.debug("[槽位] 同步 MySQL 引擎不可用, 跳过冷备份: %s", e)
        _sync_engine = False
    return _sync_engine if _sync_engine is not False else None


def _sync_upsert_slots(user_id: int | None, project_id: int | None, conversation_id: int | None, data: dict) -> None:
    """冷备份: 按 (user_id, project_id, conversation_id) upsert 一行 IntentSlots。"""
    eng = _get_sync_db()
    if eng is None or not user_id or project_id is None or conversation_id is None:
        return
    try:
        with Session(eng) as s:
            row = (
                s.query(IntentSlots)
                .filter_by(user_id=user_id, project_id=project_id, conversation_id=conversation_id)
                .first()
            )
            if row is None:
                row = IntentSlots(
                    user_id=user_id, project_id=project_id,
                    conversation_id=conversation_id, slots=data,
                )
                s.add(row)
            else:
                row.slots = data
            s.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("[槽位] MySQL upsert 失败(忽略): %s", e)


def _sync_get_slots(user_id: int | None, project_id: int | None, conversation_id: int | None) -> dict | None:
    """冷备份读取: 按 (user_id, project_id, conversation_id) 取回该行 slots; 无则 None。"""
    eng = _get_sync_db()
    if eng is None or not user_id or project_id is None or conversation_id is None:
        return None
    try:
        with Session(eng) as s:
            row = (
                s.query(IntentSlots)
                .filter_by(user_id=user_id, project_id=project_id, conversation_id=conversation_id)
                .first()
            )
            if row is not None:
                return row.slots
    except Exception as e:  # noqa: BLE001
        logger.debug("[槽位] MySQL get 失败(忽略): %s", e)
    return None


def _sync_pop_slots(user_id: int | None, project_id: int | None, conversation_id: int | None) -> None:
    """冷备份清除: 删除 (user_id, project_id, conversation_id) 那一行(仅当前会话)。"""
    eng = _get_sync_db()
    if eng is None or not user_id or project_id is None or conversation_id is None:
        return
    try:
        with Session(eng) as s:
            row = (
                s.query(IntentSlots)
                .filter_by(user_id=user_id, project_id=project_id, conversation_id=conversation_id)
                .first()
            )
            if row is not None:
                s.delete(row)
                s.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("[槽位] MySQL pop 失败(忽略): %s", e)


logger = logging.getLogger("ai_service.intent.store")

_sync_redis = None  # 0/None=未初始化; False=不可用
_sync_engine = None  # 0/None=未初始化; False=不可用

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


def load_slots(conversation_id: int | None, user_id: int | None = None, project_id: int | None = None) -> dict:
    """读取会话槽位; 无 conv_id / 无记录 / 异常 / 过期 → 返回空结构。

    Redis miss 时回源 MySQL 冷备份(intent_slots 表按 user/project/conv 定位)并回填 Redis。
    """
    if conversation_id is None:
        return dict(_EMPTY)
    try:
        r = _get_sync_redis()
        if r is not None:
            raw = r.get(_KEY_PREFIX + str(conversation_id))
            if raw:
                return json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
            # Redis miss → 回源 MySQL 冷备份(intent_slots 表)
            mysql_data = _sync_get_slots(user_id, project_id, conversation_id)
            if mysql_data:
                try:
                    r.set(_KEY_PREFIX + str(conversation_id), json.dumps(mysql_data, ensure_ascii=False))
                except Exception:  # noqa: BLE001
                    pass
                return dict(mysql_data)
        else:
            data = _local.get(conversation_id)
            if data is not None and time.time() - data.get("updated_at", 0.0) <= _LOCAL_TTL:
                return data
    except Exception as e:  # pragma: no cover
        logger.debug("[槽位] 读取失败, 返回空: %s", e)
    return dict(_EMPTY)


def save_slots(conversation_id: int | None, data: dict, user_id: int | None = None, project_id: int | None = None) -> None:
    """持久化会话槽位(失败静默)。同步写 Redis + 冷备份落 MySQL(intent_slots 表)。"""
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
    # 冷备份到 MySQL intent_slots 表(尽力, 失败忽略)
    _sync_upsert_slots(user_id, project_id, conversation_id, data)


def reset_slots(conversation_id: int | None, user_id: int | None = None, project_id: int | None = None) -> None:
    """清空会话槽位(RESET / 退出建站时调用)。Redis + MySQL 双清(仅当前会话一行)。"""
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
    _sync_pop_slots(user_id, project_id, conversation_id)
