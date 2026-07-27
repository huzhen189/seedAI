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


def _key(uid: int) -> str:
    return f"user_states:{uid}"


async def touch_user_state(user_id: int, **fields: Any) -> None:
    """写 user_states: Redis hash(即时) + MySQL upsert(权威, 重启可读)。

    fields 支持: current_project_id, current_conversation_id, active_trace_id,
    status, current_stage, progress_pct, pause_reason, pending_decision, checkpoint_stage。
    - Redis: 仅写入提供的非空字段(hset mapping, 值转 str), 并续期 TTL=3600。
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
        await rc.expire(_key(user_id), 3600)
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
