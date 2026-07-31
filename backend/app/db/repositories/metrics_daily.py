from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MetricsDaily

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.metrics_daily")


class MetricsDailyRepo(BaseRepo[MetricsDaily]):
    model = MetricsDaily

    async def by_user_and_date(
        self, session: AsyncSession, user_id: int, stat_date: date
    ) -> list[MetricsDaily]:
        if user_id <= 0:
            raise ValueError("user_id 必须为正整数")
        return await self.list(
            session, user_id=user_id, stat_date=stat_date, limit=1000
        )
