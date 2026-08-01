from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

import logging

from app.core.ids import new_ulid
from app.core.turn_context import TurnContext
from app.models import Approval, Project, ProjectTombstone, PurgeJob

logger = logging.getLogger("app.domains.project.service")


class ProjectService:
    async def request_approval(self, session: AsyncSession, context: TurnContext, action: str) -> Approval:
        """为高危项目动作(publish/purge/trash)创建审批卡。

        生成一次性质询 nonce(只下发一次,库里只存 sha256),按动作定 risk_level
        (publish/purge=critical,其余 high),默认 30 分钟过期。返回的 Approval 上临时挂
        ``_decision_nonce`` 供 S5/SSE 下发一次,不进任何持久化载荷。
        """
        logger.info("[project] 创建审批 action=%s turn=%s", action, context.turn_id)
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
        """把项目置为 purging 并递增 generation,同时建 tombstone 与 purge job(同事务)。"""
        logger.info("[project] 启动 purge project=%s generation=%d", project.id, project.purge_generation + 1)
        project.status = "purging"
        project.purge_generation += 1
        tombstone = ProjectTombstone(project_id=project.id, purge_generation=project.purge_generation)
        job = PurgeJob(project_id=project.id, purge_generation=project.purge_generation)
        session.add_all([tombstone, job])
        await session.flush()
        return job


project_service = ProjectService()
