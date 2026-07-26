"""Two-step direct-connect entry (P1).

Replaces the old proxy SSE forwarding. The business service stays a thin BFF:
it authenticates, checks quota, verifies project/conversation ownership, selects
a healthy Agent Core instance, and issues a short-lived direct-connect token.
The frontend then opens EventSource straight to the Agent Core — no second SSE
hop through business.

Contract with Agent Core:
  - The direct-connect token IS the Agent Core trace (used for stream_exists
    replay). It is HMAC-signed by business and verified by agent at /stream.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from .config import settings
from .metrics import consume_daily_quota
from .security import create_access_token, get_current_user
from .models import Project, Conversation, User

logger = logging.getLogger("business.chat_entry")

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _select_agent_url() -> str:
    """Service discovery: pick one healthy agent core.

    Uses settings.ai_servers (comma-separated) when provided; otherwise falls
    back to settings.ai_service_url. Health check stubbed (P1) — real probe in
    a later phase.
    """
    servers = [s.strip() for s in (settings.ai_servers or "").split(",") if s.strip()]
    if servers:
        # naive round-robin / first-healthy; expand to load/nearness later
        return servers[0]
    return settings.ai_service_url


def _issue_connect_token(user: User, project_id: int, conversation_id: int) -> str:
    """Short-lived direct-connect token (== agent trace id).

    Reuses the access-token signing so agent can verify with the same secret.
    Carries sub(=user), project_id, conversation_id, type=connect.
    """
    import datetime
    from datetime import timezone, timedelta
    now = datetime.datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "pid": project_id,
        "cid": conversation_id,
        "type": "connect",
        "jit": secrets.token_hex(8),       # nonce, prevents replay/reuse collisions
        "iat": now,
        "exp": now + timedelta(seconds=settings.ai_connect_token_ttl),
    }
    import jwt
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


@router.get("/entry")
async def chat_entry(
    request: Request,
    response: Response,
    project_id: int,
    conversation_id: int | None = None,
    q: str = "",
    user: User = Depends(get_current_user),
):
    """Step 1 of two-step connect.

    Returns { ai_server_url, token(connect/trace), project_id, conversation_id }.
    Does NOT touch heavy message assembly — that is owned by Agent Core now.
    """
    # [0] basic input hygiene (no heavy semantic cleaning here)
    q = (q or "").strip()
    if len(q) > 8000:
        q = q[:8000]

    # [2] quota (pure Redis, no MySQL heavy query)
    ok, remaining = await consume_daily_quota(user.id, user.plan)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="daily quota exceeded",
        )

    # [3] ownership: project must belong to user; conversation optional (auto-create)
    async for db in _db_session():
        proj = await db.get(Project, project_id)
        if proj is None or proj.user_id != user.id or proj.deleted_at is not None:
            raise HTTPException(status_code=404, detail="project not found")
        conv = None
        if conversation_id is not None:
            conv = await db.get(Conversation, conversation_id)
            if conv is None or conv.project_id != project_id or conv.user_id != user.id:
                raise HTTPException(status_code=404, detail="conversation not found")
        else:
            conv = Conversation(project_id=project_id, user_id=user.id, title="新对话")
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
        conversation_id = conv.id
        break

    # [4] select agent instance
    ai_url = _select_agent_url()

    # [5] issue direct-connect token (= agent trace)
    token = _issue_connect_token(user, project_id, conversation_id)

    return {
        "ai_server_url": ai_url.rstrip("/") + "/stream",
        "token": token,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "quota_remaining": remaining,
    }


async def _db_session():
    """Yield a DB session (thin wrapper matching existing get_db)."""
    from .db import get_db
    async for s in get_db():
        yield s
