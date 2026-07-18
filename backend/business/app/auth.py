"""认证路由:注册 / 登录 / 刷新 / 当前用户 / 登出。

令牌通过 HttpOnly + Secure + SameSite Cookie 下发(文档 §2.1),前端不持有
token;EventSource / 页面同源时浏览器自动携带 Cookie。另兼容 Bearer(便于
API 调试 / 非浏览器客户端)。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from .cache import cache_user_get, cache_user_set
from .config import settings
from .db import get_db
from .models import User
from .schemas import LoginReq, RefreshReq, RegisterReq, UserResp
from .security import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_cookies(resp: Response, access: str, refresh: str) -> None:
    opts = dict(
        max_age=settings.access_token_ttl,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain or None,
    )
    resp.set_cookie(ACCESS_COOKIE, access, **opts)
    resp.set_cookie(
        REFRESH_COOKIE, refresh, **{**opts, "max_age": settings.refresh_token_ttl}
    )


def _clear_cookies(resp: Response) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        resp.delete_cookie(name, domain=settings.cookie_domain or None)


@router.post("/register", response_model=UserResp)
async def register(req: RegisterReq, response: Response, db=Depends(get_db)):
    exists = await db.scalar(
        select(User).where(
            (User.username == req.username) | (User.email == (req.email or ""))
        )
    )
    if exists:
        raise HTTPException(status_code=409, detail="username or email already exists")

    user = User(
        username=req.username,
        email=req.email or "",
        password_hash=hash_password(req.password),
        role="user",
        plan="free",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    _set_cookies(
        response,
        create_access_token(user.id, user.role),
        create_refresh_token(user.id),
    )
    return UserResp(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        plan=user.plan,
    )


@router.post("/login", response_model=UserResp)
async def login(req: LoginReq, response: Response, db=Depends(get_db)):
    user = await db.scalar(select(User).where(User.username == req.username))
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    _set_cookies(
        response,
        create_access_token(user.id, user.role),
        create_refresh_token(user.id),
    )
    return UserResp(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        plan=user.plan,
    )


@router.post("/refresh", response_model=UserResp)
async def refresh(req: RefreshReq, response: Response, db=Depends(get_db)):
    try:
        payload = decode_token(req.refresh_token)
        if payload.get("type") != "refresh":
            raise ValueError
        uid = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="invalid refresh token")

    user = await db.get(User, uid)
    if not user:
        raise HTTPException(status_code=401, detail="user not found")
    _set_cookies(
        response,
        create_access_token(user.id, user.role),
        create_refresh_token(user.id),
    )
    return UserResp(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        plan=user.plan,
    )


@router.post("/logout")
async def logout(response: Response):
    _clear_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=UserResp)
async def me(user=Depends(get_current_user), db=Depends(get_db)):
    # 读缓存优先(活跃用户常用数据,30min 过期)
    cached = await cache_user_get(user.id)
    if cached:
        return UserResp(**cached)
    u = await db.get(User, user.id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    data = UserResp(
        id=u.id, username=u.username, email=u.email, role=u.role, plan=u.plan
    ).model_dump()
    await cache_user_set(user.id, data)  # 回填
    return UserResp(**data)
