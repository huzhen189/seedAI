"""用户级「我的状态」读写(v4 断点复联方案的状态源)。

设计: Redis hash `user_states:{uid}` 作为即时(低延迟)状态, MySQL `user_states` 表
作为权威落库(重启可读)。两者都写, 读取时 Redis 优先、miss 回 MySQL。

字段(status / current_stage / pause_reason / pending_decision / progress_pct /
current_project_id / current_conversation_id / active_trace_id / checkpoint_stage)
与 docs/my-info-state-design.md §6 一致。

注意: 本模块不依赖 proxy, 避免与 chat SSE / Worker 形成环 import。
proxy.py 与 queue.py(Worker) 都可直接导入本模块。
"""

import logging
from typing import Any, Optional

from sqlalchemy import select

from .cache import get_redis
from .db import SessionLocal
from .models import UserState

logger = logging.getLogger("app.user_state")

# Redis 状态缓存过期时间: 3 小时。任何读 / 写都会刷新该 TTL(滑动过期),
# 保证活跃用户状态常驻, 长期不活跃(>3h)自动回收, 需要时回 MySQL 取权威落库。
USER_STATE_TTL = 3 * 3600


def _key(uid: int) -> str:
    return f"user_states:{uid}"


async def _refresh_ttl(rc: Any, uid: int) -> None:
    """读取命中 Redis 后刷新过期时间(滑动续期), 避免活跃期间被回收。"""
    try:
        await rc.expire(_key(uid), USER_STATE_TTL)
    except Exception as e:  # noqa: BLE001
        logger.warning("[user_state] Redis 续期失败 uid=%s: %s", uid, e)


async def touch_user_state(user_id: int, **fields: Any) -> None:
    """写 user_states: Redis hash(即时) + MySQL upsert(权威, 重启可读)。

    fields 支持: current_project_id, current_conversation_id, active_trace_id,
    status, current_stage, progress_pct, pause_reason, pending_decision, checkpoint_stage。
    - Redis: 仅写入提供的非空字段(hset mapping, 值转 str), 并续期 TTL=USER_STATE_TTL(3h)。
    - MySQL: upsert 整行(按 user_id 唯一), 仅对模型已有属性赋值。
    """
    if not user_id:
        return
    # 过滤 None 值(Redis 不写 None;MySQL 对未提供字段保持不变)
    clean = {k: v for k, v in fields.items() if v is not None}
    if not clean:
        return

    # 1) Redis(即时)
    try:
        rc = await get_redis()
        mapping = {k: str(v) for k, v in clean.items()}
        await rc.hset(_key(user_id), mapping=mapping)
        await rc.expire(_key(user_id), USER_STATE_TTL)
    except Exception as e:  # noqa: BLE001
        logger.warning("[user_state] Redis 写入失败 uid=%s: %s", user_id, e)

    # 2) MySQL(权威落库)
    try:
        async with SessionLocal() as s:
            row = (await s.execute(
                select(UserState).where(UserState.user_id == user_id)
            )).scalar_one_or_none()
            if row is None:
                row = UserState(user_id=user_id)
                s.add(row)
            for k, v in clean.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[user_state] MySQL 写入失败 uid=%s: %s", user_id, e)


async def get_user_state(user_id: int) -> Optional[dict]:
    """读 user_states: Redis 优先, miss 回 MySQL; 返回 dict 或 None。"""
    if not user_id:
        return None
    result: dict = {}
    try:
        rc = await get_redis()
        raw = await rc.hgetall(_key(user_id))
        if raw:
            result.update(raw)
            # 读取命中 Redis -> 刷新滑动过期(USER_STATE_TTL), 保持活跃状态常驻。
            await _refresh_ttl(rc, user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[user_state] Redis 读取失败 uid=%s: %s", user_id, e)

    if not result:
        # Redis miss → MySQL
        try:
            async with SessionLocal() as s:
                row = (await s.execute(
                    select(UserState).where(UserState.user_id == user_id)
                )).scalar_one_or_none()
                if row is not None:
                    result = {
                        "current_project_id": row.current_project_id,
                        "current_conversation_id": row.current_conversation_id,
                        "active_trace_id": row.active_trace_id,
                        "status": row.status,
                        "current_stage": row.current_stage,
                        "progress_pct": row.progress_pct,
                        "pause_reason": row.pause_reason,
                        "pending_decision": row.pending_decision,
                        "checkpoint_stage": row.checkpoint_stage,
                    }
        except Exception as e:  # noqa: BLE001
            logger.warning("[user_state] MySQL 读取失败 uid=%s: %s", user_id, e)

    return result or None


async def ensure_user_state(user_id: int) -> None:
    """用户注册 / 超管创建时调用: 确保 user_states 行存在(MySQL)+ 写入 Redis 默认状态并续期。

    语义: user_states 是 users 的「一对一扩展表」, user_id 即主键, 注册即建。
    幂等: 仅当 MySQL 无该行 / Redis 无该 key 时才初始化为默认状态(idle),
    绝不覆盖已有运行时状态(例如注册后已在生成、Redis 已是 running 的场景)。
    """
    if not user_id:
        return
    # Redis: 仅当 key 不存在时初始化默认状态(idle), 再统一续期 TTL(滑动过期)。
    try:
        rc = await get_redis()
        if not await rc.exists(_key(user_id)):
            await rc.hset(_key(user_id), mapping={"status": "idle"})
        await rc.expire(_key(user_id), USER_STATE_TTL)
    except Exception as e:  # noqa: BLE001
        logger.warning("[user_state] Redis 初始化失败 uid=%s: %s", user_id, e)

    # MySQL: 仅当行不存在时建行(默认 status=idle)。
    try:
        async with SessionLocal() as s:
            row = (await s.execute(
                select(UserState).where(UserState.user_id == user_id)
            )).scalar_one_or_none()
            if row is None:
                s.add(UserState(user_id=user_id, status="idle"))
                await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[user_state] MySQL 初始化失败 uid=%s: %s", user_id, e)


# 进程重启对账需要「显式清空」的运行时字段(这些字段在活运行时可能是脏值,
# 不能像 touch_user_state 那样跳过 None —— 必须主动置 None 以清除指向已死 Worker 的孤儿状态)。
_RESET_FIELDS = [
    "pause_reason",
    "pending_decision",
    "active_trace_id",
    "current_stage",
    "checkpoint_stage",
    "progress_pct",
]


async def reset_user_state(user_id: int) -> None:
    """把 user_states 重置为 idle 并清空运行时字段。

    与 touch_user_state 的关键区别: 本函数会**显式把字段置 None**(而非跳过 None),
    因为需要清除指向已死 Worker 的脏值(active_trace_id / current_stage / pause_reason 等)。

    用于进程启动对账(reconcile_orphaned_runs): 孤儿 running/paused 状态指向进程被强杀前
    在途的 Worker, 该 Worker 已死亡, 必须清掉否则前端会误 resume 一个再也不会产出的流。

    保留 current_project_id / current_conversation_id, 让用户在刷新后仍停留在正确项目/会话,
    仅任务层状态回到 idle(无在途任务)。
    """
    if not user_id:
        return
    try:
        rc = await get_redis()
        # 置 idle, 再删除其余运行时字段(若存在)
        await rc.hset(_key(user_id), "status", "idle")
        existing = await rc.hgetall(_key(user_id))
        del_fields = [f for f in _RESET_FIELDS if f in existing]
        if del_fields:
            await rc.hdel(_key(user_id), *del_fields)
        await rc.expire(_key(user_id), USER_STATE_TTL)
    except Exception as e:  # noqa: BLE001
        logger.warning("[user_state] reset Redis 失败 uid=%s: %s", user_id, e)
    try:
        async with SessionLocal() as s:
            row = (await s.execute(
                select(UserState).where(UserState.user_id == user_id)
            )).scalar_one_or_none()
            if row is not None:
                row.status = "idle"
                for f in _RESET_FIELDS:
                    if hasattr(row, f):
                        setattr(row, f, None)
                await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[user_state] reset MySQL 失败 uid=%s: %s", user_id, e)
