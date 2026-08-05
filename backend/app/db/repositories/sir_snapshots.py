from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SirSnapshot

from ._base import BaseRepo, RepositoryError


logger = logging.getLogger("app.db.repositories.sir_snapshots")


class SirSnapshotsRepo(BaseRepo[SirSnapshot]):
    model = SirSnapshot

    async def latest_for_conversation(
        self, session: AsyncSession, conversation_id: int, kind: str | None = "base"
    ) -> SirSnapshot | None:
        """取该会话最新一条 SIR 快照。

        排序键用自增 ``id``（单调递增即写入顺序），**不能用 turn_no**——v3 真相模型的
        ``sir_snapshots`` 表根本没有该列，旧写法会在运行时抛 AttributeError，被 S1 的
        兜底 except 吞掉，导致 sir_base 永远是空壳（DST 基态恒空的真实根因）。
        """
        if conversation_id <= 0:
            raise ValueError("conversation_id 必须为正整数")
        try:
            stmt = select(SirSnapshot).where(SirSnapshot.conversation_id == conversation_id)
            if kind is not None:
                stmt = stmt.where(SirSnapshot.kind == kind)
            result = await session.execute(stmt.order_by(SirSnapshot.id.desc()).limit(1))
            return result.scalar_one_or_none()
        except SQLAlchemyError as exc:
            logger.exception("读取最新 SIR 快照失败")
            raise RepositoryError("latest_for_conversation", "SirSnapshot", str(exc)) from exc

    async def latest_for_turn(
        self, session: AsyncSession, turn_id: str, kind: str | None = "base"
    ) -> SirSnapshot | None:
        """取指定 turn 落下的快照（回溯控制：把状态回滚到"上一轮结束时"的基态）。"""
        if not turn_id:
            raise ValueError("turn_id 不得为空")
        try:
            stmt = select(SirSnapshot).where(SirSnapshot.turn_id == turn_id)
            if kind is not None:
                stmt = stmt.where(SirSnapshot.kind == kind)
            result = await session.execute(stmt.order_by(SirSnapshot.id.desc()).limit(1))
            return result.scalar_one_or_none()
        except SQLAlchemyError as exc:
            logger.exception("读取 turn SIR 快照失败")
            raise RepositoryError("latest_for_turn", "SirSnapshot", str(exc)) from exc
