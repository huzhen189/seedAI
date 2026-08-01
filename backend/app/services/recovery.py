"""进程重启后的 Turn 自愈。

进程被强杀时，在途 Pipeline 随进程消失，但 turns.status 仍停留在运行态。
这些 Turn 没有任何执行者，若不回收，前端快照会永远显示"运行中"。
启动时统一翻为 failed 并标注错误码，用户可重新发起。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.contracts import TurnStatus
from app.db import transaction
from app.models import Turn

logger = logging.getLogger("app.services.recovery")

# 进程内存活才有意义的状态：重启后必然失去执行者。
_ORPHAN_STATUS = (
    TurnStatus.ACCEPTED.value,
    TurnStatus.RUNNING.value,
    TurnStatus.RECOVERY_PENDING.value,
)


async def reconcile_orphan_turns() -> int:
    """把无执行者的运行态 Turn 翻为终态，返回回收数量。"""
    async with transaction() as session:
        rows = list(
            (await session.execute(select(Turn).where(Turn.status.in_(_ORPHAN_STATUS)))).scalars()
        )
        now = datetime.now(UTC)
        for turn in rows:
            turn.status = TurnStatus.FAILED.value
            turn.terminal_error_code = "ORPHANED_BY_RESTART"
            turn.lock_version += 1
            turn.updated_at = now

    if rows:
        logger.warning("[recovery] 回收孤儿 Turn %d 个: %s", len(rows), [t.turn_id for t in rows[:10]])
    else:
        logger.info("[recovery] 无孤儿 Turn")
    return len(rows)
