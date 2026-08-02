"""S0/S9 使用的 Turn 真相服务。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import ExecutionBudget, SessionInfo, TrustFlags, UserIdentity
from app.core.ids import new_ulid
from app.core.turn_context import TurnContext
from app.db.repositories import conversations, outbox, turns, usage_ledger
from app.models import Conversation, Message, Turn
from app.security import CurrentUser

import logging

logger = logging.getLogger("app.services.turns")


_PII_PATTERNS = (
    (re.compile(r"\b1[3-9]\d{9}\b"), "[PHONE_REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[EMAIL_REDACTED]"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "[ID_REDACTED]"),
)


@dataclass(frozen=True, slots=True)
class AcceptedTurn:
    context: TurnContext
    existing: bool


def clean_message(raw_message: str) -> tuple[str, TrustFlags]:
    if not raw_message.strip():
        raise HTTPException(status_code=422, detail={"code": "EMPTY_MESSAGE"})
    truncated = len(raw_message) > 8000
    value = raw_message[:8000]
    redacted = False
    for pattern, replacement in _PII_PATTERNS:
        next_value = pattern.sub(replacement, value)
        redacted = redacted or next_value != value
        value = next_value
    injection = "ignore previous instructions" in value.lower() or "system prompt" in value.lower()
    return value.strip(), TrustFlags(injection_suspected=injection, pii_redacted=redacted, truncated=truncated)


class TurnService:
    async def accept(
        self,
        session: AsyncSession,
        *,
        user: CurrentUser,
        conversation_id: int,
        client_msg_id: str,
        raw_message: str,
        expected_conversation_version: int | None,
        prior_turn_id: str | None = None,
    ) -> AcceptedTurn:
        """受理一个新 Turn(幂等)。

        幂等键为 ``client_msg_id``：重复提交同一 id 不会新建 Turn，只复用已有流
        (且 digest 必须一致，否则判为冲突)。新 Turn 会落 user Message、预留用量、写 outbox，
        并构造 ``TurnContext`` 交给后续 Pipeline。

        Returns:
            ``AcceptedTurn(context, existing)``；existing=True 表示命中幂等复用。
        """
        digest = hashlib.sha256(raw_message.encode("utf-8")).hexdigest()
        existing = await turns.by_client_message(session, user.id, client_msg_id)
        if existing is not None:
            if existing.request_digest != digest:
                raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_DIGEST_CONFLICT"})
            logger.info("[turn] 幂等复用 turn=%s client_msg_id=%s", existing.turn_id, client_msg_id)
            context = self._context_from_existing(existing, user, raw_message)
            return AcceptedTurn(context=context, existing=True)

        clean, trust = clean_message(raw_message)
        conversation = await conversations.owned(session, conversation_id, user.id)
        if conversation is None or conversation.status != "active":
            raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
        if expected_conversation_version is not None and conversation.version != expected_conversation_version:
            raise HTTPException(status_code=409, detail={"code": "CONVERSATION_VERSION_CONFLICT"})

        logger.info(
            "[turn] 新受理 turn user=%s conv=%s msg_len=%d pii=%s truncated=%s",
            user.id, conversation_id, len(clean), trust.pii_redacted, trust.truncated,
        )
        turn_id = new_ulid()
        stream_id = new_ulid()
        fencing_token = new_ulid()
        turn = await turns.insert(
            session,
            turn_id=turn_id,
            user_id=user.id,
            conversation_id=conversation.id,
            client_msg_id=client_msg_id,
            request_digest=digest,
            stream_id=stream_id,
            trace_id=turn_id,
            status="running",
            fencing_token=fencing_token,
        )
        session.add(
            Message(
                conversation_id=conversation.id,
                project_id=conversation.project_id,
                turn_id=turn_id,
                trace_id=turn.trace_id,
                role="user",
                content=clean,
                content_refs=[],
            )
        )
        conversation.version += 1
        await usage_ledger.insert(session, turn_id=turn_id, user_id=user.id, kind="model_calls", reserved_units=1, status="reserved")
        await outbox.insert(
            session,
            event_key=f"turn:{turn_id}:accepted",
            aggregate_type="turn",
            aggregate_id=turn_id,
            event_type="turn.accepted",
            payload={"turn_id": turn_id, "stream_id": stream_id},
        )
        await session.flush()
        return AcceptedTurn(
            context=TurnContext(
                schema_version="1.0",
                trace_id=turn.trace_id,
                stream_id=stream_id,
                turn_id=turn_id,
                client_msg_id=client_msg_id,
                run_epoch=0,
                fencing_token=fencing_token,
                user=UserIdentity(user_id=user.id, tier=user.tier, roles=(user.role,)),
                session=SessionInfo(conversation_id=conversation.id, project_id=conversation.project_id),
                clean_message=clean,
                trust=trust,
                prior_turn_id=prior_turn_id,
                budget=ExecutionBudget(max_model_calls=1, reserved_model_calls=1),
            ),
            existing=False,
        )

    async def snapshot(self, session: AsyncSession, turn_id: str, user_id: int) -> dict[str, Any]:
        """返回单个 Turn 的快照(状态/流 id/运行 epoch/最后事件),供前端轮询。"""
        result = await session.execute(select(Turn).where(Turn.turn_id == turn_id, Turn.user_id == user_id))
        turn = result.scalar_one_or_none()
        if turn is None:
            raise HTTPException(status_code=404, detail={"code": "TURN_NOT_FOUND"})
        logger.debug("[turn] 快照 turn=%s status=%s", turn_id, turn.status)
        return {
            "turn_id": turn.turn_id,
            "stream_id": turn.stream_id,
            "status": turn.status,
            "run_epoch": turn.run_epoch,
            "last_event_id": turn.last_event_id,
        }

    def _context_from_existing(self, turn: Turn, user: CurrentUser, raw_message: str) -> TurnContext:
        clean, trust = clean_message(raw_message)
        return TurnContext(
            schema_version="1.0",
            trace_id=turn.trace_id,
            stream_id=turn.stream_id,
            turn_id=turn.turn_id,
            client_msg_id=turn.client_msg_id,
            run_epoch=turn.run_epoch,
            fencing_token=turn.fencing_token,
            user=UserIdentity(user_id=user.id, tier=user.tier, roles=(user.role,)),
            session=SessionInfo(conversation_id=turn.conversation_id),
            clean_message=clean,
            trust=trust,
            budget=ExecutionBudget(max_model_calls=1, reserved_model_calls=1),
        )


turn_service = TurnService()
