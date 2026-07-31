from __future__ import annotations

import asyncio

import pytest

from app.db.repositories import MessagesRepo, ProjectsRepo, UsersRepo
from app.db.schema_check import check_schema
from app.models import Conversation

from .conftest import isolated_database


def test_message_content_path_is_validated_and_only_appended() -> None:
    async def scenario() -> None:
        async with isolated_database() as (_, session_factory):
            messages = MessagesRepo()
            users = UsersRepo()
            projects = ProjectsRepo()
            async with session_factory() as session:
                user = await users.insert(
                    session,
                    email="message@example.invalid",
                    display_name="Message User",
                )
                project = await projects.insert(
                    session, user_id=user.id, name="Message Project"
                )
                conversation = Conversation(
                    project_id=project.id,
                    user_id=user.id,
                    name="Message Conversation",
                )
                session.add(conversation)
                await session.flush()
                message = await messages.insert(
                    session,
                    conversation_id=conversation.id,
                    project_id=project.id,
                    turn_no=1,
                    role="assistant",
                    content="done",
                )
                await session.commit()

                first = {
                    "path": "previews/1/v1/index.html",
                    "uri": "https://example.invalid/index.html",
                    "kind": "html",
                    "source_tool": "site_publish",
                    "status": "active",
                    "version": "v1",
                    "size_bytes": 128,
                    "created_at": 1_785_432_100,
                }
                updated = await messages.append_content_path(session, message.id, first)
                assert updated.content_path == [first]
                await session.commit()

                second = {
                    **first,
                    "path": "previews/1/v2/app.js",
                    "kind": "js",
                    "source_tool": "fs_write",
                    "version": "v2",
                }
                updated = await messages.append_content_path(session, message.id, second)
                assert [item["path"] for item in updated.content_path] == [
                    first["path"],
                    second["path"],
                ]
                await session.commit()

                with pytest.raises(ValueError, match="content_path"):
                    await messages.append_content_path(
                        session,
                        message.id,
                        {**first, "kind": "asset", "path": "../escape.html"},
                    )

    asyncio.run(scenario())


def test_schema_check_returns_structured_success_report() -> None:
    async def scenario() -> None:
        async with isolated_database() as (engine, _):
            report = await check_schema(engine)
            payload = report.as_dict()
            assert payload["ok"] is True
            assert payload["dialect"] == "sqlite"
            assert payload["issues"] == []
            assert "projects" in payload["tables_checked"]

    asyncio.run(scenario())
