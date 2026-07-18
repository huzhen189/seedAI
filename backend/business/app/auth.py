"""认证路由:注册 / 登录 / 刷新 / 当前用户。"""
from fastapi import APIRouter, Depends
from sqlalchemy import select

from .db import get_db
from .models import User
from .schemas import LoginReq, RefreshReq, RegisterReq, TokenResp, UserResp
from .security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from .cache import cache_user_get, cache_user_set, cache_user_invalidate

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResp)
async def register(req: RegisterReq, db=Depends(get_db)):
    exists = await db.scalar(
        select(User).where((User.username == req.username) | (User.email == (req.email or "")))
    )
    if exists:
        from fastapi import HTTPException

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
    return TokenResp(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/login", response_model=TokenResp)
async def login(req: LoginReq, db=Depends(get_db)):
    user = await db.scalar(select(User).where(User.username == req.username))
    if not user or not verify_password(req.password, user.password_hash):
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="invalid credentials")
    return TokenResp(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/refresh", response_model=TokenResp)
async def refresh(req: RefreshReq, db=Depends(get_db)):
    try:
        payload = decode_token(req.refresh_token)
        if payload.get("type") != "refresh":
            raise ValueError
        uid = int(payload["sub"])
    except Exception:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="invalid refresh token")

    user = await db.get(User, uid)
    if not user:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="user not found")
    return TokenResp(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id),
    )


@router.get("/me", response_model=UserResp)
async def me(user=Depends(get_current_user), db=Depends(get_db)):
    # 读缓存优先(活跃用户常用数据,30min 过期)
    cached = await cache_user_get(user.id)
    if cached:
        return UserResp(**cached)
    u = await db.get(User, user.id)
    if not u:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="user not found")
    data = UserResp(
        id=u.id, username=u.username, email=u.email, role=u.role, plan=u.plan
    ).model_dump()
    await cache_user_set(user.id, data)  # 回填
    return UserResp(**data)
