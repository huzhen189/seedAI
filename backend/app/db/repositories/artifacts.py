from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Artifact

from ._base import BaseRepo, RepositoryError


logger = logging.getLogger("app.db.repositories.artifacts")


class ArtifactsRepo(BaseRepo[Artifact]):
    model = Artifact

    async def list_by_project(
        self, session: AsyncSession, project_id: int, *, limit: int = 1000
    ) -> list[Artifact]:
        if project_id <= 0:
            raise ValueError("project_id 必须为正整数")
        return await self.list(session, project_id=project_id, limit=limit)

    async def get_by_trace(self, session: AsyncSession, trace_id: str) -> Artifact | None:
        if not trace_id.strip():
            raise ValueError("trace_id 不得为空")
        return await self.get_by(session, trace_id=trace_id)

    async def exists_repo_for_conversation(
        self, session: AsyncSession, conversation_id: int, repo: str
    ) -> bool:
        if conversation_id <= 0 or not repo.strip():
            raise ValueError("conversation_id 和 repo 必须有效")
        try:
            result = await session.execute(
                select(Artifact.id)
                .where(Artifact.conversation_id == conversation_id, Artifact.repo == repo)
                .limit(1)
            )
            return result.first() is not None
        except SQLAlchemyError as exc:
            logger.exception("检查会话产物失败 conversation_id=%s repo=%s", conversation_id, repo)
            raise RepositoryError("exists_repo_for_conversation", "Artifact", str(exc)) from exc

    async def upsert_by_trace(
        self,
        session: AsyncSession,
        trace_id: str,
        *,
        project_id: int,
        conversation_id: int,
        title: str,
        repo: str,
        files: dict[str, Any],
        preview_url: str = "",
        preview_path: str | None = None,
    ) -> Artifact:
        if not trace_id.strip() or project_id <= 0 or conversation_id <= 0:
            raise ValueError("trace_id/project_id/conversation_id 必须有效")
        if not isinstance(files, dict):
            raise ValueError("files 必须是对象")
        primary = next(iter(files), None)
        name = title.strip() or primary or repo.strip() or "artifact"
        try:
            result = await session.execute(
                select(Artifact).where(Artifact.trace_id == trace_id).with_for_update()
            )
            artifact = result.scalar_one_or_none()
            if artifact is None:
                artifact = Artifact(
                    project_id=project_id,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    name=name,
                    title=title,
                    repo=repo,
                    files=files,
                    preview_url=preview_url,
                    download_url=preview_url,
                    preview_path=preview_path,
                    status="done",
                )
                session.add(artifact)
            else:
                artifact.files = files
                artifact.preview_url = preview_url
                artifact.download_url = preview_url
                artifact.preview_path = preview_path
                artifact.title = title
                artifact.name = name
                artifact.status = "done"
            await session.flush()
            await session.refresh(artifact)
            return artifact
        except (SQLAlchemyError, TypeError, ValueError) as exc:
            await self._rollback_after_error(session, "upsert_by_trace", "Artifact", exc)
            raise RepositoryError("upsert_by_trace", "Artifact", str(exc)) from exc

    async def delete_all(self, session: AsyncSession, project_id: int) -> int:
        if project_id <= 0:
            raise ValueError("project_id 必须为正整数")
        try:
            result = await session.execute(delete(Artifact).where(Artifact.project_id == project_id))
            await session.flush()
            return int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            await self._rollback_after_error(session, "delete_all", "Artifact", exc)
            raise RepositoryError("delete_all", "Artifact", str(exc)) from exc

    async def delete_file(
        self, session: AsyncSession, project_id: int, filename: str
    ) -> bool:
        if project_id <= 0 or not filename.strip():
            raise ValueError("project_id 和 filename 必须有效")
        try:
            result = await session.execute(
                select(Artifact).where(Artifact.project_id == project_id).with_for_update()
            )
            for artifact in result.scalars():
                files = artifact.files
                if not isinstance(files, dict) or filename not in files:
                    continue
                remaining = {key: value for key, value in files.items() if key != filename}
                if remaining:
                    artifact.files = remaining
                else:
                    await session.delete(artifact)
                await session.flush()
                return True
            return False
        except SQLAlchemyError as exc:
            await self._rollback_after_error(session, "delete_file", "Artifact", exc)
            raise RepositoryError("delete_file", "Artifact", str(exc)) from exc


artifact_repo = ArtifactsRepo()
