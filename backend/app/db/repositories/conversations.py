from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message

from ._base import BaseRepo, RepositoryError


logger = logging.getLogger("app.db.repositories.conversations")


class ConversationStateError(ValueError):
    """会话状态迁移不合法。"""


class ConversationsRepo(BaseRepo[Conversation]):
    model = Conversation
    _transitions = {
        "active": frozenset({"archived", "trashed"}),
        "archived": frozenset({"active", "trashed"}),
        "trashed": frozenset({"active"}),
    }

    async def list_by_project(
        self,
        session: AsyncSession,
        project_id: int,
        user_id: int | None = None,
        *,
        limit: int = 1000,
    ) -> list[Conversation]:
        if project_id <= 0:
            raise ValueError("project_id 必须为正整数")
        filters: dict[str, int] = {"project_id": project_id}
        if user_id is not None:
            if user_id <= 0:
                raise ValueError("user_id 必须为正整数")
            filters["user_id"] = user_id
        try:
            statement = self._filtered_statement(filters).order_by(Conversation.updated_at.desc())
            result = await session.execute(statement.limit(limit))
            return list(result.scalars().all())
        except SQLAlchemyError as exc:
            logger.exception("读取项目会话失败 project_id=%s user_id=%s", project_id, user_id)
            raise RepositoryError("list_by_project", "Conversation", str(exc)) from exc

    async def by_project(
        self, session: AsyncSession, project_id: int, *, limit: int = 100
    ) -> list[Conversation]:
        return await self.list_by_project(session, project_id, limit=limit)

    async def get_with_messages(
        self, session: AsyncSession, conversation_id: int
    ) -> Conversation | None:
        return await self.get(session, conversation_id)

    async def delete_cascade(self, session: AsyncSession, conversation: Conversation) -> None:
        if not isinstance(conversation.id, int) or conversation.id <= 0:
            raise ValueError("conversation 必须带有效 id")
        try:
            await session.execute(delete(Message).where(Message.conversation_id == conversation.id))
            await session.delete(conversation)
            await session.flush()
        except SQLAlchemyError as exc:
            await self._rollback_after_error(session, "delete_cascade", "Conversation", exc)
            raise RepositoryError("delete_cascade", "Conversation", str(exc)) from exc

    async def transition(
        self,
        session: AsyncSession,
        conversation_id: int,
        *,
        expected_status: str,
        target_status: str,
    ) -> Conversation:
        allowed = self._transitions.get(expected_status)
        if allowed is None or target_status not in allowed:
            raise ConversationStateError(
                f"禁止会话状态迁移 {expected_status} -> {target_status}"
            )
        now = datetime.now(UTC)
        values: dict[str, object] = {"status": target_status, "updated_at": now}
        if target_status == "trashed":
            values["trashed_at"] = now
        elif expected_status == "trashed":
            values.update(trashed_at=None, deleted_at=None)
        try:
            result = await session.execute(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.status == expected_status,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise ConversationStateError("会话不存在或状态已被并发修改")
            await session.flush()
            # 移除 session.expire_all()：会误伤同会话的其它对象（如 site 路径的 project），
            # 在同步上下文访问其惰性属性时触发 MissingGreenlet。改用 session.refresh 只刷新本会话记录。
            conversation = await self.get(session, conversation_id)
            if conversation is not None:
                await session.refresh(conversation)
            if conversation is None:
                raise RepositoryError("transition", "Conversation", "迁移后会话不可见")
            return conversation
        except ConversationStateError:
            raise
        except SQLAlchemyError as exc:
            await self._rollback_after_error(session, "transition", "Conversation", exc)
            raise RepositoryError("transition", "Conversation", str(exc)) from exc

    async def archive(self, session: AsyncSession, conversation_id: int) -> Conversation:
        return await self.transition(
            session, conversation_id, expected_status="active", target_status="archived"
        )

    async def activate(self, session: AsyncSession, conversation_id: int) -> Conversation:
        return await self.transition(
            session, conversation_id, expected_status="archived", target_status="active"
        )

    async def soft_delete(
        self, session: AsyncSession, conversation_id: int, *, expected_status: str = "active"
    ) -> Conversation:
        return await self.transition(
            session,
            conversation_id,
            expected_status=expected_status,
            target_status="trashed",
        )

    async def restore(self, session: AsyncSession, conversation_id: int) -> Conversation:
        return await self.transition(
            session, conversation_id, expected_status="trashed", target_status="active"
        )


conv_repo = ConversationsRepo()
