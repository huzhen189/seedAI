from __future__ import annotations

import asyncio

import pytest

from app.db.repositories import (
    ProjectsRepo,
    ProjectStateError,
    TasksRepo,
    TaskStateError,
    UsersRepo,
)
from app.models import Conversation

from .conftest import isolated_database


async def seed_project(session: object) -> tuple[int, int]:
    users = UsersRepo()
    projects = ProjectsRepo()
    user = await users.insert(
        session,
        email="state@example.invalid",
        display_name="State User",
    )
    project = await projects.insert(session, user_id=user.id, name="State Project")
    await session.flush()
    return user.id, project.id


def test_project_terminal_state_machine() -> None:
    async def scenario() -> None:
        async with isolated_database() as (_, session_factory):
            repo = ProjectsRepo()
            async with session_factory() as session:
                _, project_id = await seed_project(session)
                await session.commit()

                active = await repo.activate(session, project_id)
                assert active.status == "active"
                trashed = await repo.soft_delete(session, project_id)
                assert trashed.status == "trashed"
                assert trashed.trashed_at is not None
                restored = await repo.restore(session, project_id)
                assert restored.status == "active"
                assert restored.trashed_at is None
                trashed_again = await repo.soft_delete(session, project_id)
                purging = await repo.begin_purge(session, trashed_again.id)
                deleted = await repo.mark_deleted(session, purging.id)
                assert deleted.status == "deleted"
                assert deleted.deleted_at is not None

                with pytest.raises(ProjectStateError):
                    await repo.transition(
                        session,
                        project_id,
                        expected_status="deleted",
                        target_status="active",
                    )

    asyncio.run(scenario())


def test_task_state_machine_and_optimistic_lock() -> None:
    async def scenario() -> None:
        async with isolated_database() as (_, session_factory):
            repo = TasksRepo()
            async with session_factory() as session:
                user_id, project_id = await seed_project(session)
                conversation = Conversation(
                    project_id=project_id,
                    user_id=user_id,
                    name="Task Conversation",
                )
                session.add(conversation)
                await session.flush()
                task = await repo.insert(
                    session,
                    conversation_id=conversation.id,
                    title="Run",
                    kind="plan",
                )
                await session.commit()

                running = await repo.start(session, task.id, expected_version=1)
                assert (running.status, running.version) == ("running", 2)
                with pytest.raises(TaskStateError):
                    await repo.finish(
                        session,
                        task.id,
                        outcome="done",
                        expected_version=1,
                    )
                done = await repo.finish(
                    session,
                    task.id,
                    outcome="done",
                    expected_version=2,
                )
                assert (done.status, done.version) == ("done", 3)

    asyncio.run(scenario())
