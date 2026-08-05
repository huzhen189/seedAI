from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

import json
import logging
import re

from app.core.governance import action_risk_label
from app.core.ids import new_ulid
from app.core.turn_context import TurnContext
from app.models import Approval, Project, ProjectTombstone, PurgeJob

logger = logging.getLogger("app.domains.project.service")

# 增量发布文件清单令牌标记, 前端把勾选文件清单内嵌进消息文本, 形如:
#   [PUBLISH_FILES]
#   index.html
#   style.css
#   [/PUBLISH_FILES]
# S5 解析后存进 approval.args, 审批执行时取出做增量复制(未勾选文件保留不动)。
_PUBLISH_FILES_RE = re.compile(
    r"\[PUBLISH_FILES\]\s*(.*?)\s*\[/PUBLISH_FILES\]",
    re.DOTALL | re.IGNORECASE,
)


def parse_publish_files(message: str) -> list[str] | None:
    """从消息文本解析增量发布文件清单; 无标记返回 None。"""
    m = _PUBLISH_FILES_RE.search(message or "")
    if not m:
        return None
    files = [
        ln.strip()
        for ln in m.group(1).splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return files or None


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
        # 解析增量发布文件清单(若有), 连同其他结构化参数一并存入 approval.args,
        # 审批决策执行时取出透传, 避免把参数塞进消息文本或 args_hash。
        approval_args: dict = {}
        if action == "publish":
            pf = parse_publish_files(context.clean_message)
            if pf is not None:
                approval_args["publish_files"] = pf
        approval = Approval(
            approval_id=approval_id,
            turn_id=context.turn_id,
            action=action,
            target_type="project",
            target_id=str(context.session.project_id),
            artifact_id=None,
            manifest_digest=None,
            args_hash=hashlib.sha256(context.clean_message.encode("utf-8")).hexdigest(),
            args=approval_args,
            # 审批卡 risk_level 同样走 governance 统一裁决（第 3 份硬编码拷贝已收敛）：
            # 取 ToolMeta.risk 与历史推导的上界，保证审批卡风险与 S5/S6 审计口径一致。
            risk_level=action_risk_label(action),
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
