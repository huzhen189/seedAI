"""S9 的 W0 终态、消息、用量与 Outbox 同事务收口。"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.turn_context import TurnContext
from app.db.repositories import outbox
from app.models import Message, Turn, UsageLedger

logger = logging.getLogger("app.services.finalize")


class FinalizeService:
    async def finalize(self, session: AsyncSession, context: TurnContext) -> str:
        """把本轮结果在单一事务内收口(终态唯一写入点)。

        步骤：
          1. 以 ``for_update`` 锁住 Turn 行,避免并发终态竞争；
          2. 由 validation 状态决定终态：completed / waiting_approval / blocked；
          3. completed 时写 assistant 消息(内容来自护栏后的 reply_final,绝不带原始生成文本)；
          4. 释放用量预留(settled=0, status=released)；
          5. 同一个事务落 outbox(终态事件,event_key 唯一即幂等护栏)——若不同事务,
             外部只能看到 accepted、永远等不到收口。

        Returns:
            终态字符串(completed/waiting_approval/blocked)。
        """
        logger.debug("[finalize] 收口开始 turn=%s", context.turn_id)
        turn = (await session.execute(select(Turn).where(Turn.turn_id == context.turn_id).with_for_update())).scalar_one()
        terminal = "completed"
        if context.validation is not None and context.validation.status == "needs_approval":
            terminal = "waiting_approval"
        elif context.validation is not None and context.validation.status == "block":
            terminal = "blocked"
        if terminal == "completed":
            session.add(
                Message(
                    conversation_id=context.session.conversation_id,
                    project_id=context.session.project_id or 0,
                    turn_id=context.turn_id,
                    role="assistant",
                    content=context.reply_final,
                    content_refs=[{"artifact_id": ref} for ref in (context.execution.artifact_refs if context.execution else [])],
                )
            )
        usage = (await session.execute(select(UsageLedger).where(UsageLedger.turn_id == context.turn_id, UsageLedger.kind == "model_calls"))).scalar_one_or_none()
        if usage is not None:
            usage.settled_units = 0
            usage.status = "released"
        turn.status = terminal
        turn.last_event_id = "finalized"
        turn.lock_version += 1
        # 终态必须与业务写入同事务落 Outbox，否则外部只能看到 turn.accepted、
        # 永远等不到收口。event_key 的唯一约束即幂等护栏。
        artifact_refs = list(context.execution.artifact_refs) if context.execution else []
        await outbox.insert(
            session,
            event_key=f"turn:{context.turn_id}:{terminal}",
            aggregate_type="turn",
            aggregate_id=context.turn_id,
            event_type=f"turn.{terminal}",
            payload={
                "turn_id": context.turn_id,
                "conversation_id": context.session.conversation_id,
                "project_id": context.session.project_id,
                "status": terminal,
                "artifact_refs": artifact_refs,
            },
        )
        await session.flush()
        logger.info("[finalize] 收口完成 turn=%s -> %s", context.turn_id, terminal)
        return terminal


finalize_service = FinalizeService()
