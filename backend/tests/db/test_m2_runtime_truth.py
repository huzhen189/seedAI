from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.db.repositories.runtime import approvals_repo, deployments_repo, outbox_events_repo, turn_checkpoints_repo, turns_repo
from app.db.runtime_schema import check_m2_runtime_schema
from app.models import Artifact, Conversation, Project, User

from .conftest import isolated_database


def test_m2_runtime_truth_models_share_a_single_caller_owned_transaction() -> None:
    async def scenario() -> None:
        async with isolated_database() as (engine, session_factory):
            async with session_factory() as session:
                user = User(email="m2@example.invalid", display_name="M2 User")
                session.add(user)
                await session.flush()
                project = Project(user_id=user.id, name="M2 Project")
                session.add(project)
                await session.flush()
                conversation = Conversation(project_id=project.id, user_id=user.id, name="M2 Conversation")
                session.add(conversation)
                await session.flush()

                turn = await turns_repo.insert(
                    session,
                    turn_id="01HZX7J2MQDDMNX1CJ94GG6N5K",
                    user_id=user.id,
                    conversation_id=conversation.id,
                    client_msg_id="client-message-1",
                    request_digest="a" * 64,
                    stream_id="01HZX7J2MQDDMNX1CJ94GG6N5M",
                    trace_id="m2-trace",
                    fencing_token="fence-0",
                )
                checkpoint = await turn_checkpoints_repo.insert(
                    session,
                    turn_id=turn.turn_id,
                    run_epoch=0,
                )
                artifact = Artifact(
                    project_id=project.id,
                    conversation_id=conversation.id,
                    version=1,
                    name="site-v1",
                    status="building",
                )
                session.add(artifact)
                await session.flush()
                approval = await approvals_repo.insert(
                    session,
                    approval_id="01HZX7J2MQDDMNX1CJ94GG6N5N",
                    turn_id=turn.turn_id,
                    action="site_deploy",
                    target_type="artifact",
                    target_id=str(artifact.id),
                    artifact_id=artifact.id,
                    manifest_digest="c" * 64,
                    args_hash="d" * 64,
                    risk_level="critical",
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    created_by=user.id,
                    fencing_token=turn.fencing_token,
                )
                deployment = await deployments_repo.insert(
                    session,
                    project_id=project.id,
                    artifact_id=artifact.id,
                    manifest_digest="c" * 64,
                    environment="production",
                )
                outbox = await outbox_events_repo.insert(
                    session,
                    event_key=f"turn:{turn.turn_id}:accepted",
                    aggregate_type="turn",
                    aggregate_id=turn.turn_id,
                    event_type="turn.accepted",
                    payload={"turn_id": turn.turn_id},
                )
                await session.commit()

                assert checkpoint.turn_id == turn.turn_id
                assert approval.artifact_id == artifact.id
                assert deployment.artifact_id == artifact.id
                assert outbox.status == "pending"
                assert await turns_repo.get_by_client_message(
                    session, user_id=user.id, client_msg_id="client-message-1"
                ) is not None

            report = await check_m2_runtime_schema(engine)
            assert report.ok, report.as_dict()

    asyncio.run(scenario())
