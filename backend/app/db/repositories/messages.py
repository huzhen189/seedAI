from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ContentPathItem, Conversation, Message, validate_content_path

from ._base import BaseRepo, RepositoryError


logger = logging.getLogger("app.db.repositories.messages")


class MessagesRepo(BaseRepo[Message]):
    model = Message

    @staticmethod
    def normalize_content(content: str) -> str:
        """解包历史 SSE ``{"data": ...}`` 拼接格式；普通正文原样返回。"""
        if not content or not content.startswith('{"data":'):
            return content
        decoder = json.JSONDecoder()
        position = 0
        parts: list[str] = []
        while position < len(content):
            while position < len(content) and content[position].isspace():
                position += 1
            if position >= len(content):
                break
            try:
                value, end = decoder.raw_decode(content, position)
            except json.JSONDecodeError:
                return content
            if not isinstance(value, dict) or "data" not in value:
                return content
            parts.append(str(value["data"]))
            position = end
        return "".join(parts) if parts else content

    async def by_conversation(
        self, session: AsyncSession, conversation_id: int, *, limit: int = 500
    ) -> list[Message]:
        if conversation_id <= 0:
            raise ValueError("conversation_id 必须为正整数")
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1..5000 之间")
        try:
            result = await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.turn_no.asc(), Message.id.asc())
                .limit(limit)
            )
            messages = list(result.scalars().all())
            for message in messages:
                message.content = self.normalize_content(message.content)
            return messages
        except SQLAlchemyError as exc:
            logger.exception("读取会话消息失败 conversation_id=%s", conversation_id)
            raise RepositoryError("by_conversation", "Message", str(exc)) from exc

    async def list_by_conversation(
        self, session: AsyncSession, conversation_id: int, *, limit: int = 500
    ) -> list[Message]:
        return await self.by_conversation(session, conversation_id, limit=limit)

    async def get_by_trace(
        self, session: AsyncSession, trace_id: str, role: str
    ) -> Message | None:
        if not trace_id.strip():
            raise ValueError("trace_id 不得为空")
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("role 非法")
        try:
            result = await session.execute(
                select(Message).where(Message.trace_id == trace_id, Message.role == role).limit(2)
            )
            rows = list(result.scalars())
            if len(rows) > 1:
                raise RepositoryError("get_by_trace", "Message", "同 trace/role 存在重复消息")
            return rows[0] if rows else None
        except RepositoryError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("按 trace 读取消息失败 trace_id=%s role=%s", trace_id, role)
            raise RepositoryError("get_by_trace", "Message", str(exc)) from exc

    async def upsert_assistant(
        self,
        session: AsyncSession,
        conversation_id: int,
        trace_id: str,
        content: str,
        model_id: str,
    ) -> Message:
        normalized = self.normalize_content(content)
        existing = await self.get_by_trace(session, trace_id, "assistant")
        if existing is not None:
            existing.content = normalized
            existing.model_id = model_id
            await session.flush()
            await session.refresh(existing)
            return existing
        try:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise LookupError(f"会话 id={conversation_id} 不存在")
            result = await session.execute(
                select(func.coalesce(func.max(Message.turn_no), 0)).where(
                    Message.conversation_id == conversation_id
                )
            )
            next_turn = int(result.scalar_one()) + 1
            message = Message(
                conversation_id=conversation_id,
                project_id=conversation.project_id,
                turn_no=next_turn,
                role="assistant",
                content=normalized,
                model_id=model_id,
                trace_id=trace_id,
            )
            session.add(message)
            await session.flush()
            await session.refresh(message)
            return message
        except (SQLAlchemyError, LookupError, ValueError) as exc:
            await self._rollback_after_error(session, "upsert_assistant", "Message", exc)
            raise RepositoryError("upsert_assistant", "Message", str(exc)) from exc

    async def delete_by_conversation(
        self, session: AsyncSession, conversation_id: int
    ) -> int:
        if conversation_id <= 0:
            raise ValueError("conversation_id 必须为正整数")
        try:
            result = await session.execute(
                delete(Message).where(Message.conversation_id == conversation_id)
            )
            await session.flush()
            return int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            await self._rollback_after_error(session, "delete_by_conversation", "Message", exc)
            raise RepositoryError("delete_by_conversation", "Message", str(exc)) from exc

    async def append_content_path(
        self,
        session: AsyncSession,
        message_id: int,
        items: ContentPathItem | dict[str, Any] | Sequence[ContentPathItem | dict[str, Any]],
    ) -> Message:
        raw_items: list[ContentPathItem | dict[str, Any]] = (
            [items] if isinstance(items, (ContentPathItem, dict)) else list(items)
        )
        if not raw_items:
            raise ValueError("至少需要追加一个 content_path 条目")
        additions = validate_content_path(raw_items)
        try:
            result = await session.execute(
                select(Message).where(Message.id == message_id).with_for_update()
            )
            message = result.scalar_one_or_none()
            if message is None:
                raise LookupError(f"消息 id={message_id} 不存在")
            current = validate_content_path(message.content_path)
            message.content_path = [*current, *additions]
            await session.flush()
            await session.refresh(message)
            return message
        except (SQLAlchemyError, LookupError, ValueError) as exc:
            await self._rollback_after_error(session, "append_content_path", "Message", exc)
            if isinstance(exc, ValueError):
                raise
            raise RepositoryError("append_content_path", "Message", str(exc)) from exc


message_repo = MessagesRepo()
