"""鉴权工具:bcrypt 密码哈希 + JWT 签发/校验 + 当前用户依赖。"""
import logging

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
logger = logging.getLogger("business.security")


_bearer = HTTPBearer(auto_error=False)

# 鉴权 Cookie 名(文档 §2.1:HttpOnly + Secure + SameSite)
ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"


# ---------- 密码 ----------
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ---------- JWT ----------
def create_access_token(user_id: int, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),  # subject:用户 id,decode 后转 int 用
        "role": role,  # 角色直接塞进 token,鉴权依赖无需每次查库
        "type": "access",  # 标记令牌类型,刷新接口会拒绝非 refresh 类型的 token
        "iat": now,  # 签发时间
        "exp": now + timedelta(seconds=settings.access_token_ttl),  # 过期时间(短时效)
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(seconds=settings.refresh_token_ttl),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


class CurrentUser:
    def __init__(self, user_id: int, role: str):
        self.id = user_id
        self.role = role


# 滑动过期: 每次有效操作都会重新签发 token(见 get_current_user)。
# 保留阈值常量仅作文档参考, 实际改为「每次操作必续」以满足产品需求:
# 用户有新操作时即刷新过期时间, 避免长会话(如 e2e 全流程)中途掉线。
RENEW_THRESHOLD = 600  # 10 分钟(仅作历史参考, 不再作为续期门控)


def _set_access_cookie(response: Response, token: str) -> None:
    """在 Response 上设置新的 access_token Cookie(滑动续期用)。"""
    response.set_cookie(
        ACCESS_COOKIE, token,
        max_age=settings.access_token_ttl,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain or None,
    )


def _renew_access_token(response: Response, user: CurrentUser) -> str:
    """滑动续期: 每次有效操作重新签发 access_token, 双通道回传。

    - Set-Cookie: 浏览器同源客户端(SSE/页面)自动携带
    - X-Access-Token 响应头: 非浏览器客户端(?token= / Bearer)据此轮换本地 token
    """
    new_token = create_access_token(user.id, user.role)
    _set_access_cookie(response, new_token)
    response.headers["X-Access-Token"] = new_token
    return new_token


def get_current_user(
    request: Request,
    response: Response,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> CurrentUser:
    # 1) HttpOnly Cookie(前端同源自动携带,SSE/页面均可用,文档 §2.1)
    token = request.cookies.get(ACCESS_COOKIE)
    # 2) 兼容 Bearer(便于 API 调试 / 非浏览器客户端)
    if not token and creds is not None:
        token = creds.credentials
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication",
        )
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise ValueError("not an access token")
        user = CurrentUser(int(payload["sub"]), payload.get("role", "user"))
        # 滑动过期(产品需求): 每次有效操作都重新签发 token, 刷新过期时间,
        # 双通道回传(Set-Cookie + X-Access-Token), 活跃用户不会断线。
        _renew_access_token(response, user)
        return user
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


# 角色常量(与 User.role 字段、文档 §3 RBAC 三级保持一致)。
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "admin"
ROLE_USER = "user"


def is_super_admin(user: CurrentUser) -> bool:
    return user.role == ROLE_SUPER_ADMIN


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """管理页只读权限:super_admin 或 admin 均可进入后台(文档 §3)。

    注意:此前实现只放行 `admin`,会把 super_admin 也挡在门外(与文档冲突),
    这里改为双角色放行。
    """
    if user.role not in (ROLE_ADMIN, ROLE_SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def require_super_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """控制面 / 用户与角色管理权限:仅 super_admin(文档 §3)。

    普通 admin 仅能查看后台,不能执行控制面与角色管理。
    """
    if user.role != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super admin only")
    return user
