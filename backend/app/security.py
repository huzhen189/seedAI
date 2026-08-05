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


def _effective_cookie_domain(request: Request | None) -> str | None:
    """计算实际下发的 Cookie Domain。

    设计: 仅在「配置域名是请求 host 的父域/自身」时下发 Domain(支持跨子域 SSO),
    否则下发 None(浏览器把 Cookie 绑到当前 host)。

    为什么: 配置 COOKIE_DOMAIN=huzhen.net.cn 但用户在 localhost 访问时,
    浏览器因 Cookie 的 Domain 与 localhost 不匹配而拒绝回送,导致登录态丢失、
    受保护请求 401 又弹出登录框。动态化后 localhost / 127.0.0.1 / 自定义域名
    都能正确存回 Cookie,同时保留生产跨子域能力。
    """
    cfg = settings.cookie_domain
    if not cfg:
        return None
    if request is None:
        return cfg
    host = (request.headers.get("host") or request.url.hostname or "").split(":")[0].lower()
    if not host:
        return cfg
    cfg_lower = cfg.lower().lstrip(".")
    # host 与配置域名同域或为其子域时才下发 Domain
    if host == cfg_lower or host.endswith("." + cfg_lower):
        return cfg
    return None


def set_auth_cookies(response: Response, user: User, request: Request | None = None) -> None:
    domain = _effective_cookie_domain(request)
    response.set_cookie(ACCESS_COOKIE, create_access_token(user), httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.access_token_ttl, domain=domain)
    response.set_cookie(REFRESH_COOKIE, create_refresh_token(user), httponly=True, secure=settings.cookie_secure, samesite="strict", max_age=settings.refresh_token_ttl, domain=domain)


def clear_auth_cookies(response: Response, request: Request | None = None) -> None:
    domain = _effective_cookie_domain(request)
    response.delete_cookie(ACCESS_COOKIE, domain=domain)
    response.delete_cookie(REFRESH_COOKIE, domain=domain)


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


def require_super_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """仅超级管理员(super_admin)可访问:用户/角色管理、控制面、重置等。"""
    if user.role != "super_admin":
        raise HTTPException(status_code=403, detail={"code": "SUPER_ADMIN_REQUIRED"})
    return user
