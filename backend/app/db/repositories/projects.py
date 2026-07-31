from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project

from ._base import BaseRepo, RepositoryError


logger = logging.getLogger("app.db.repositories.projects")


class ProjectStateError(ValueError):
    """项目状态迁移不合法。"""


class ProjectsRepo(BaseRepo[Project]):
    model = Project
    _transitions = {
        "draft": frozenset({"active"}),
        "active": frozenset({"trashed"}),
        "trashed": frozenset({"active", "purging"}),
        "purging": frozenset({"deleted"}),
        "deleted": frozenset(),
    }

    async def list_by_user(
        self, session: AsyncSession, user_id: int, *, limit: int = 1000
    ) -> list[Project]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(session, user_id=user_id, deleted_at=None, limit=limit)

    async def by_user_and_status(
        self, session: AsyncSession, user_id: int, status: str, *, limit: int = 100
    ) -> list[Project]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(session, user_id=user_id, status=status, limit=limit)

    async def active_version(self, session: AsyncSession, project_id: int) -> int | None:
        project = await self.get(session, project_id)
        if project is None:
            return None
        version = project.config.get("active_version", 1)
        if not isinstance(version, int) or version < 1:
            raise RepositoryError("active_version", "Project", "config.active_version 无效")
        return version

    async def transition(
        self,
        session: AsyncSession,
        project_id: int,
        *,
        expected_status: str,
        target_status: str,
    ) -> Project:
        allowed = self._transitions.get(expected_status)
        if allowed is None or target_status not in allowed:
            raise ProjectStateError(f"禁止项目状态迁移 {expected_status} -> {target_status}")
        now = datetime.now(UTC)
        values: dict[str, object] = {"status": target_status, "updated_at": now}
        if target_status == "trashed":
            values["trashed_at"] = now
        elif expected_status == "trashed" and target_status == "active":
            values.update(trashed_at=None, expires_at=None)
        elif target_status == "deleted":
            values["deleted_at"] = now
        try:
            result = await session.execute(
                update(Project)
                .where(Project.id == project_id, Project.status == expected_status)
                .values(**values)
            )
            if result.rowcount != 1:
                raise ProjectStateError("项目不存在或状态已被并发修改")
            await session.flush()
            session.expire_all()
            project = await self.get(session, project_id)
            if project is None:
                raise RepositoryError("transition", "Project", "迁移后项目不可见")
            return project
        except ProjectStateError:
            raise
        except SQLAlchemyError as exc:
            await self._rollback_after_error(session, "transition", "Project", exc)
            raise RepositoryError("transition", "Project", str(exc)) from exc

    async def activate(self, session: AsyncSession, project_id: int) -> Project:
        return await self.transition(
            session, project_id, expected_status="draft", target_status="active"
        )

    async def soft_delete(self, session: AsyncSession, project_id: int) -> Project:
        return await self.transition(
            session, project_id, expected_status="active", target_status="trashed"
        )

    async def restore(self, session: AsyncSession, project_id: int) -> Project:
        return await self.transition(
            session, project_id, expected_status="trashed", target_status="active"
        )

    async def begin_purge(self, session: AsyncSession, project_id: int) -> Project:
        return await self.transition(
            session, project_id, expected_status="trashed", target_status="purging"
        )

    async def mark_deleted(self, session: AsyncSession, project_id: int) -> Project:
        return await self.transition(
            session, project_id, expected_status="purging", target_status="deleted"
        )

    async def get_by_share_id(self, session: AsyncSession, share_id: str) -> Project | None:
        if not share_id.strip():
            raise ValueError("share_id 不得为空")
        result = await session.execute(select(Project).where(Project.share_id == share_id))
        return result.scalar_one_or_none()


project_repo = ProjectsRepo()
