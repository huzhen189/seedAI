"""新链路身份、JWT 与高风险 step-up 基础。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import User


ACCESS_COOKIE = "seedai_access"
REFRESH_COOKIE = "seedai_refresh"
CSRF_COOKIE = "seedai_csrf"
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: int
    account: str
    role: str
    tier: str
    token_version: int


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _encode(payload: dict[str, Any], ttl_seconds: int) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {**payload, "iat": now, "exp": now + timedelta(seconds=ttl_seconds)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def create_access_token(user: User) -> str:
    return _encode(
        {"sub": str(user.id), "account": user.account, "role": user.role, "tier": user.tier, "tv": user.token_version, "type": "access"},
        settings.access_token_ttl,
    )


def create_refresh_token(user: User) -> str:
    return _encode({"sub": str(user.id), "tv": user.token_version, "type": "refresh"}, settings.refresh_token_ttl)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def set_auth_cookies(response: Response, user: User) -> None:
    response.set_cookie(ACCESS_COOKIE, create_access_token(user), httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.access_token_ttl, domain=settings.cookie_domain or None)
    response.set_cookie(REFRESH_COOKIE, create_refresh_token(user), httponly=True, secure=settings.cookie_secure, samesite="strict", max_age=settings.refresh_token_ttl, domain=settings.cookie_domain or None)


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, domain=settings.cookie_domain or None)
    response.delete_cookie(REFRESH_COOKIE, domain=settings.cookie_domain or None)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    token = request.cookies.get(ACCESS_COOKIE) or (credentials.credentials if credentials else None)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "AUTH_REQUIRED"})
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise ValueError("wrong token type")
        user_id = int(payload["sub"])
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "INVALID_TOKEN"}) from exc
    user = await db.get(User, user_id)
    if user is None or user.status != "active" or user.token_version != payload.get("tv"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "INVALID_TOKEN"})
    return CurrentUser(user.id, user.account, user.role, user.tier, user.token_version)


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role not in {"admin", "super_admin"}:
        raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED"})
    return user
