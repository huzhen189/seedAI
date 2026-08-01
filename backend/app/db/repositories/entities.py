"""新链路的单表 Repository。没有 commit/rollback/跨表调用。"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Approval,
    Artifact,
    Conversation,
    Deployment,
    Message,
    OutboxEvent,
    Project,
    ProjectTombstone,
    PurgeJob,
    Task,
    ToolCall,
    Turn,
    TurnCheckpoint,
    UsageLedger,
    User,
)

ModelT = TypeVar("ModelT")


class SingleTableRepository(Generic[ModelT]):
    model: type[ModelT]

    async def insert(self, session: AsyncSession, **values: Any) -> ModelT:
        item = self.model(**values)
        session.add(item)
        await session.flush()
        return item

    async def get(self, session: AsyncSession, record_id: int) -> ModelT | None:
        return await session.get(self.model, record_id)

    async def list(self, session: AsyncSession, statement: Select[tuple[ModelT]]) -> list[ModelT]:
        result = await session.execute(statement)
        return list(result.scalars())


class UsersRepository(SingleTableRepository[User]):
    model = User

    async def by_account(self, session: AsyncSession, account: str) -> User | None:
        return (await session.execute(select(User).where(User.account == account))).scalar_one_or_none()


class ProjectsRepository(SingleTableRepository[Project]):
    model = Project

    async def owned(self, session: AsyncSession, project_id: int, user_id: int) -> Project | None:
        return (
            await session.execute(select(Project).where(Project.id == project_id, Project.user_id == user_id))
        ).scalar_one_or_none()


class ConversationsRepository(SingleTableRepository[Conversation]):
    model = Conversation

    async def owned(self, session: AsyncSession, conversation_id: int, user_id: int) -> Conversation | None:
        return (
            await session.execute(select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id))
        ).scalar_one_or_none()


class MessagesRepository(SingleTableRepository[Message]):
    model = Message


class TurnsRepository(SingleTableRepository[Turn]):
    model = Turn

    async def by_client_message(self, session: AsyncSession, user_id: int, client_msg_id: str) -> Turn | None:
        return (
            await session.execute(select(Turn).where(Turn.user_id == user_id, Turn.client_msg_id == client_msg_id))
        ).scalar_one_or_none()

    async def by_turn_id(self, session: AsyncSession, turn_id: str) -> Turn | None:
        return (await session.execute(select(Turn).where(Turn.turn_id == turn_id))).scalar_one_or_none()

    async def cas_status(
        self,
        session: AsyncSession,
        *,
        turn_id: str,
        expected_status: str,
        expected_version: int,
        target_status: str,
        fencing_token: str | None = None,
    ) -> bool:
        values: dict[str, Any] = {"status": target_status, "lock_version": expected_version + 1}
        if fencing_token is not None:
            values["fencing_token"] = fencing_token
        result = await session.execute(
            update(Turn)
            .where(Turn.turn_id == turn_id, Turn.status == expected_status, Turn.lock_version == expected_version)
            .values(**values)
        )
        return result.rowcount == 1


class TurnCheckpointsRepository(SingleTableRepository[TurnCheckpoint]):
    model = TurnCheckpoint


class ArtifactsRepository(SingleTableRepository[Artifact]):
    model = Artifact


class DeploymentsRepository(SingleTableRepository[Deployment]):
    model = Deployment


class TasksRepository(SingleTableRepository[Task]):
    model = Task


class ToolCallsRepository(SingleTableRepository[ToolCall]):
    model = ToolCall


class ApprovalsRepository(SingleTableRepository[Approval]):
    model = Approval

    async def by_external_id(self, session: AsyncSession, approval_id: str) -> Approval | None:
        return (await session.execute(select(Approval).where(Approval.approval_id == approval_id))).scalar_one_or_none()


class UsageLedgerRepository(SingleTableRepository[UsageLedger]):
    model = UsageLedger


class OutboxRepository(SingleTableRepository[OutboxEvent]):
    model = OutboxEvent


class TombstonesRepository(SingleTableRepository[ProjectTombstone]):
    model = ProjectTombstone


class PurgeJobsRepository(SingleTableRepository[PurgeJob]):
    model = PurgeJob


users = UsersRepository()
projects = ProjectsRepository()
conversations = ConversationsRepository()
messages = MessagesRepository()
turns = TurnsRepository()
turn_checkpoints = TurnCheckpointsRepository()
artifacts = ArtifactsRepository()
deployments = DeploymentsRepository()
tasks = TasksRepository()
tool_calls = ToolCallsRepository()
approvals = ApprovalsRepository()
usage_ledger = UsageLedgerRepository()
outbox = OutboxRepository()
tombstones = TombstonesRepository()
purge_jobs = PurgeJobsRepository()
