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


@router.post("/register", response_model=UserResp)
async def register(req: RegisterReq, response: Response, db=Depends(get_db)):
    # 唯一性校验:用户名必查;邮箱仅当用户填写时才参与查重(允许空邮箱用户)。
    # 用 or_ 合并,任一命中即视为已存在 -> 409,防止重复账号 / 邮箱撞车。
    conditions = [User.username == req.username]
    if req.email:
        conditions.append(User.email == req.email)
    exists = await db.scalar(select(User).where(or_(*conditions)))
    if exists:
        raise HTTPException(status_code=409, detail="username or email already exists")

    # 新建用户:默认 role=user、plan=free(超级管理员由 SEED_SUPER_ADMIN 种子注入,
    # 不在此开放自助注册);nickname 缺省回退为用户名;密码经 bcrypt 哈希存储,
    # 绝不落明文。
    user = User(
        username=req.username,
        nickname=req.nickname or req.username,
        email=req.email or None,
        password_hash=hash_password(req.password),
        role="user",
        plan="free",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    # 注册即签发并下发 Cookie,用户无需再走一次登录。
    _set_cookies(
        response,
        create_access_token(user.id, user.role),
        create_refresh_token(user.id),
    )
    return UserResp(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
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
        nickname=user.nickname,
        email=user.email,
        role=user.role,
        plan=user.plan,
    )


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
        nickname=user.nickname,
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
        id=u.id,
        username=u.username,
        nickname=u.nickname,
        email=u.email,
        role=u.role,
        plan=u.plan,
    ).model_dump()
    await cache_user_set(user.id, data)  # 回填
    return UserResp(**data)


@router.patch("/me", response_model=UserResp)
async def update_me(req: UpdateMeReq, user=Depends(get_current_user), db=Depends(get_db)):
    """修改当前用户信息:昵称 / 邮箱 / 密码(改密码需验旧密码)。"""
    u = await db.get(User, user.id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")

    # 邮箱变更需查重(排除自己)
    if req.email is not None and req.email != u.email:
        exists = await db.scalar(select(User).where((User.email == req.email) & (User.id != u.id)))
        if exists:
            raise HTTPException(status_code=409, detail="email already exists")
        u.email = req.email

    if req.nickname is not None:
        u.nickname = req.nickname

    if req.new_password:
        # 即便已登录也要验旧密码:防止会话被劫持时攻击者无声改密把自己锁在外面。
        # 旧密码错误直接 400,不泄露任何额外信息。
        if not req.old_password or not verify_password(req.old_password, u.password_hash):
            raise HTTPException(status_code=400, detail="old password incorrect")
        u.password_hash = hash_password(req.new_password)

    await db.commit()
    await db.refresh(u)
    # 更新缓存(让 /auth/me 立即生效)
    data = UserResp(
        id=u.id,
        username=u.username,
        nickname=u.nickname,
        email=u.email,
        role=u.role,
        plan=u.plan,
    ).model_dump()
    await cache_user_set(u.id, data)
    return UserResp(**data)
