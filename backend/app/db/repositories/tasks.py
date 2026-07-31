from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task

from ._base import BaseRepo, RepositoryError


logger = logging.getLogger("app.db.repositories.tasks")


class TaskStateError(ValueError):
    """Task 状态迁移或乐观锁校验失败。"""


class TasksRepo(BaseRepo[Task]):
    model = Task
    _transitions = {
        "pending": frozenset({"running"}),
        "running": frozenset({"done", "failed", "cancelled"}),
        "done": frozenset(),
        "failed": frozenset(),
        "cancelled": frozenset(),
    }

    async def by_conversation_and_status(
        self, session: AsyncSession, conversation_id: int, status: str, *, limit: int = 100
    ) -> list[Task]:
        if conversation_id <= 0:
            raise ValueError("conversation_id 必须为正整数")
        return await self.list(
            session, conversation_id=conversation_id, status=status, limit=limit
        )

    async def transition(
        self,
        session: AsyncSession,
        task_id: int,
        *,
        expected_status: str,
        target_status: str,
        expected_version: int,
    ) -> Task:
        allowed = self._transitions.get(expected_status)
        if allowed is None or target_status not in allowed:
            raise TaskStateError(f"禁止 Task 状态迁移 {expected_status} -> {target_status}")
        if expected_version < 1:
            raise ValueError("expected_version 必须大于等于 1")
        try:
            result = await session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == expected_status,
                    Task.version == expected_version,
                )
                .values(status=target_status, version=expected_version + 1)
            )
            if result.rowcount != 1:
                raise TaskStateError("Task 不存在、状态不符或 version 乐观锁冲突")
            await session.flush()
            session.expire_all()
            task = await self.get(session, task_id)
            if task is None:
                raise RepositoryError("transition", "Task", "迁移后 Task 不可见")
            return task
        except TaskStateError:
            raise
        except SQLAlchemyError as exc:
            await self._rollback_after_error(session, "transition", "Task", exc)
            raise RepositoryError("transition", "Task", str(exc)) from exc

    async def start(
        self, session: AsyncSession, task_id: int, *, expected_version: int
    ) -> Task:
        return await self.transition(
            session,
            task_id,
            expected_status="pending",
            target_status="running",
            expected_version=expected_version,
        )

    async def finish(
        self,
        session: AsyncSession,
        task_id: int,
        *,
        outcome: str,
        expected_version: int,
    ) -> Task:
        if outcome not in {"done", "failed", "cancelled"}:
            raise ValueError("outcome 必须是 done、failed 或 cancelled")
        return await self.transition(
            session,
            task_id,
            expected_status="running",
            target_status=outcome,
            expected_version=expected_version,
        )
