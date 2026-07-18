"""鉴权工具:bcrypt 密码哈希 + JWT 签发/校验 + 当前用户依赖。"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings


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


def get_current_user(
    request: Request,
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
        return CurrentUser(int(payload["sub"]), payload.get("role", "user"))
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
