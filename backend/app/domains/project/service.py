from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_ulid
from app.core.turn_context import TurnContext
from app.models import Approval, Project, ProjectTombstone, PurgeJob


class ProjectService:
    async def request_approval(self, session: AsyncSession, context: TurnContext, action: str) -> Approval:
        approval_id = new_ulid()
        nonce = secrets.token_urlsafe(24)
        approval = Approval(
            approval_id=approval_id,
            turn_id=context.turn_id,
            action=action,
            target_type="project",
            target_id=str(context.session.project_id),
            artifact_id=None,
            manifest_digest=None,
            args_hash=hashlib.sha256(context.clean_message.encode("utf-8")).hexdigest(),
            risk_level="critical" if action in {"publish", "purge"} else "high",
            challenge_nonce_hash=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            created_by=context.user.user_id,
            fencing_token=context.fencing_token,
        )
        session.add(approval)
        await session.flush()
        # nonce only lives in the immediate approval event; database stores its hash.
        approval.__dict__["_decision_nonce"] = nonce
        return approval

    async def begin_purge(self, session: AsyncSession, project: Project) -> PurgeJob:
        project.status = "purging"
        project.purge_generation += 1
        tombstone = ProjectTombstone(project_id=project.id, purge_generation=project.purge_generation)
        job = PurgeJob(project_id=project.id, purge_generation=project.purge_generation)
        session.add_all([tombstone, job])
        await session.flush()
        return job


project_service = ProjectService()
