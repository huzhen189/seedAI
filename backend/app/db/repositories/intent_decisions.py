from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IntentDecision

from ._base import BaseRepo


logger = logging.getLogger("app.db.repositories.intent_decisions")


class IntentDecisionsRepo(BaseRepo[IntentDecision]):
    model = IntentDecision

    async def corrected_for_intent(
        self, session: AsyncSession, chosen_intent: str, *, limit: int = 100
    ) -> list[IntentDecision]:
        if not chosen_intent.strip():
            raise ValueError("chosen_intent 不得为空")
        return await self.list(
            session,
            chosen_intent=chosen_intent,
            hitl_corrected=True,
            limit=limit,
        )
