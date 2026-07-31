from __future__ import annotations

import asyncio

from app.db.repositories import UsersRepo

from .conftest import isolated_database


def test_base_repo_crud() -> None:
    async def scenario() -> None:
        async with isolated_database() as (_, session_factory):
            repo = UsersRepo()
            async with session_factory() as session:
                user = await repo.insert(
                    session,
                    email="repo@example.invalid",
                    display_name="Repo User",
                )
                await session.commit()
                assert user.id > 0

                loaded = await repo.get(session, user.id)
                assert loaded is not None
                assert loaded.display_name == "Repo User"

                listed = await repo.list(session, email="repo@example.invalid")
                assert [item.id for item in listed] == [user.id]

                updated = await repo.update(
                    session, user.id, display_name="Updated User"
                )
                await session.commit()
                assert updated.display_name == "Updated User"

                deleted = await repo.hard_delete(session, user.id)
                await session.commit()
                assert deleted is True
                assert await repo.get(session, user.id) is None

    asyncio.run(scenario())
