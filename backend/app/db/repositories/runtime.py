"""M2 运行真相表 Repository。

每个类仅访问一个 ORM 表且绝不提交、回滚或开启事务；调用方的 Service/UnitOfWork
拥有事务边界，后续 M5-M8 将在其上实现审批消费与 Outbox 一致性写入。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Approval, Deployment, OutboxEvent, Turn, TurnCheckpoint


ModelT = TypeVar("ModelT", Turn, TurnCheckpoint, Approval, Deployment, OutboxEvent)


class SingleTableRepo(Generic[ModelT]):
    model: type[ModelT]

    async def insert(self, session: AsyncSession, **values: Any) -> ModelT:
        record = self.model(**values)
        session.add(record)
        await session.flush()
        await session.refresh(record)
        return record

    async def get_by_id(self, session: AsyncSession, record_id: int) -> ModelT | None:
        return await session.get(self.model, record_id)


class TurnsRepo(SingleTableRepo[Turn]):
    model = Turn

    async def get_by_client_message(
        self, session: AsyncSession, *, user_id: int, client_msg_id: str
    ) -> Turn | None:
        result = await session.execute(
            select(Turn).where(Turn.user_id == user_id, Turn.client_msg_id == client_msg_id)
        )
        return result.scalar_one_or_none()


class TurnCheckpointsRepo(SingleTableRepo[TurnCheckpoint]):
    model = TurnCheckpoint


class ApprovalsRepo(SingleTableRepo[Approval]):
    model = Approval

    async def get_by_approval_id(self, session: AsyncSession, approval_id: str) -> Approval | None:
        result = await session.execute(select(Approval).where(Approval.approval_id == approval_id))
        return result.scalar_one_or_none()


class DeploymentsRepo(SingleTableRepo[Deployment]):
    model = Deployment


class OutboxEventsRepo(SingleTableRepo[OutboxEvent]):
    model = OutboxEvent

    async def get_by_event_key(self, session: AsyncSession, event_key: str) -> OutboxEvent | None:
        result = await session.execute(select(OutboxEvent).where(OutboxEvent.event_key == event_key))
        return result.scalar_one_or_none()


turns_repo = TurnsRepo()
turn_checkpoints_repo = TurnCheckpointsRepo()
approvals_repo = ApprovalsRepo()
deployments_repo = DeploymentsRepo()
outbox_events_repo = OutboxEventsRepo()
