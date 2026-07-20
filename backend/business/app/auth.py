"""认证路由:注册 / 登录 / 刷新 / 当前用户 / 登出。

令牌通过 HttpOnly + Secure + SameSite Cookie 下发(文档 §2.1),前端不持有
token;EventSource / 页面同源时浏览器自动携带 Cookie。另兼容 Bearer(便于
API 调试 / 非浏览器客户端)。
"""

from typing import Literal, Optional, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import or_, select

from .cache import cache_user_get, cache_user_set
from .config import settings
from .db import get_db
from .models import User
from .repos.user_repo import user_repo
from .schemas import LoginReq, RefreshReq, RegisterReq, UpdateMeReq, UserResp
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


class _CookieOpts(TypedDict):
    max_age: int
    httponly: bool
    secure: bool
    samesite: Literal["lax", "strict", "none"]
    domain: Optional[str]


def _set_cookies(resp: Response, access: str, refresh: str) -> None:
    """把 access / refresh 两个 JWT 写进 HttpOnly Cookie。

    为什么用 Cookie 而非把 token 返给前端存 localStorage?
      - HttpOnly: JS 读不到,从源头挡住 XSS 窃取 token;
      - SameSite=lax: 跨站 POST 不自动带,缓解 CSRF(配合业务仅同源调用更稳);
      - Secure: 仅 HTTPS 传输(生产 cookie_secure=true;本地 http 联调设 false 才能写)。
    浏览器在同源的 EventSource / fetch 请求里会自动带上这两个 Cookie,
    前端无需手动管理 token,也避免 Bearer 头在 SSE 里无法携带的问题。
    access 短时效(max_age=access_token_ttl),refresh 长时效用于静默续期。
    """
    opts: _CookieOpts = {
        "max_age": settings.access_token_ttl,
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "domain": settings.cookie_domain or None,
    }
    resp.set_cookie(ACCESS_COOKIE, access, **opts)
    # refresh 复用除 max_age 外的全部属性,仅把有效期换成 refresh_token_ttl。
    refresh_opts: _CookieOpts = {**opts, "max_age": settings.refresh_token_ttl}
    resp.set_cookie(REFRESH_COOKIE, refresh, **refresh_opts)


def _clear_cookies(resp: Response) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        resp.delete_cookie(name, domain=settings.cookie_domain or None)


def _to_resp(u: User) -> UserResp:
    return UserResp(id=u.id, username=u.username, nickname=u.nickname,
                    email=u.email, role=u.role, plan=u.plan)


@router.post("/register", response_model=UserResp)
async def register(req: RegisterReq, response: Response, db=Depends(get_db)):
    # 唯一性校验
    existing = await user_repo.get_by_username(db, req.username)
    if existing:
        raise HTTPException(status_code=409, detail="username already exists")
    if req.email:
        existing_email = await user_repo.get_by_email(db, req.email)
        if existing_email:
            raise HTTPException(status_code=409, detail="email already exists")
    # 通过 Repo 创建(内部走 Redis+MySQL 双写)
    user = await user_repo.create(
        db,
        username=req.username,
        nickname=req.nickname or req.username,
        email=req.email or None,
        password_hash=hash_password(req.password),
        role="user",
        plan="free",
    )
    _set_cookies(response, create_access_token(user.id, user.role), create_refresh_token(user.id))
    return _to_resp(user)


@router.post("/login", response_model=UserResp)
async def login(req: LoginReq, response: Response, db=Depends(get_db)):
    user = await user_repo.get_by_username(db, req.username)
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    _set_cookies(response, create_access_token(user.id, user.role), create_refresh_token(user.id))
    return _to_resp(user)


@router.post("/refresh", response_model=UserResp)
async def refresh(req: RefreshReq, response: Response, db=Depends(get_db)):
    # 刷新令牌必须严格校验两点:
    #   1. 能正常 decode(签名/过期都会抛异常 -> 401);
    #   2. token 的 type 字段必须为 "refresh",防止拿 access token 当 refresh 用
    #      (access 短时效、且权限不同,混用会放大被盗用风险)。
    try:
        payload = decode_token(req.refresh_token)
        if payload.get("type") != "refresh":
            raise ValueError
        uid = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="invalid refresh token")

    user = await user_repo.get_by_id(db, uid)
    if not user:
        raise HTTPException(status_code=401, detail="user not found")
    _set_cookies(response, create_access_token(user.id, user.role), create_refresh_token(user.id))
    return _to_resp(user)


@router.post("/logout")
async def logout(response: Response):
    _clear_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=UserResp)
async def me(user=Depends(get_current_user), db=Depends(get_db)):
    cached = await cache_user_get(user.id)
    if cached:
        return UserResp(**cached)
    u = await user_repo.get_by_id(db, user.id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    data = _to_resp(u).model_dump()
    await cache_user_set(user.id, data)
    return UserResp(**data)


@router.patch("/me", response_model=UserResp)
async def update_me(req: UpdateMeReq, user=Depends(get_current_user), db=Depends(get_db)):
    u = await user_repo.get_by_id(db, user.id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")

    # 邮箱变更需查重(排除自己)
    if req.email is not None and req.email != u.email:
        existing = await user_repo.get_by_email(db, req.email)
        if existing and existing.id != u.id:
            raise HTTPException(status_code=409, detail="email already exists")

    if req.new_password:
        if not req.old_password or not verify_password(req.old_password, u.password_hash):
            raise HTTPException(status_code=400, detail="old password incorrect")
        u.password_hash = hash_password(req.new_password)
        await db.commit()
        await db.refresh(u)

    # 普通字段走 Repo update(自动 Redis 缓存)
    u = await user_repo.update_profile(
        db, u,
        nickname=req.nickname,
        email=req.email,
    )
    data = _to_resp(u).model_dump()
    await cache_user_set(u.id, data)
    return UserResp(**data)
