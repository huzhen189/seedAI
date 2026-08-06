"""新数据库认证 API。"""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.repositories import users
from app.models import User
from app.security import CurrentUser, clear_auth_cookies, create_access_token, decode_token, get_current_user, hash_password, set_auth_cookies, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    account: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    account: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/register", status_code=201)
async def register(body: RegisterRequest, response: Response, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    if await users.by_account(db, body.account):
        raise HTTPException(status_code=409, detail={"code": "ACCOUNT_EXISTS"})
    user = await users.insert(
        db,
        account=body.account,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        email=body.email,
    )
    await db.commit()
    set_auth_cookies(response, user, request)
    return {"user": {"id": user.id, "account": user.account, "display_name": user.display_name, "role": user.role, "tier": user.tier}}


@router.post("/login")
async def login(body: LoginRequest, response: Response, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    user = await users.by_account(db, body.account)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})
    if user.status != "active":
        raise HTTPException(status_code=403, detail={"code": "ACCOUNT_DISABLED"})
    set_auth_cookies(response, user, request)
    return {"user": {"id": user.id, "account": user.account, "display_name": user.display_name, "role": user.role, "tier": user.tier}, "access_token": create_access_token(user)}


@router.post("/refresh")
async def refresh(response: Response, request: Request, db: AsyncSession = Depends(get_db), current: CurrentUser = Depends(get_current_user)) -> dict[str, str]:
    user = await db.get(User, current.id)
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN"})
    set_auth_cookies(response, user, request)
    return {"access_token": create_access_token(user)}


@router.post("/logout", status_code=204)
async def logout(response: Response, request: Request) -> None:
    clear_auth_cookies(response, request)


@router.get("/me")
async def me(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """返回当前登录用户信息;含用户级偏好(目前为偏好执行模型)。"""
    user = await db.get(User, current.id)
    prefs = user.preferences if user and isinstance(user.preferences, dict) else {}
    return {
        "id": current.id,
        "account": current.account,
        "role": current.role,
        "tier": current.tier,
        "preferred_model": prefs.get("preferred_model", "qwen"),
    }


class PreferredModelRequest(BaseModel):
    model: str = Field(min_length=1, max_length=32)


@router.put("/me/preferred-model")
async def set_preferred_model(
    body: PreferredModelRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """保存当前用户的偏好执行模型(user_id 绑定, 仅写自己)。

    值须经 list_models() 白名单校验(只接受后端确实枚举到的可用模型), 拒绝无效/越权值。
    """
    from app.llm import list_models

    allowed = {m["id"] for m in list_models()}
    if body.model not in allowed:
        raise HTTPException(status_code=400, detail={"code": "INVALID_MODEL", "allowed": sorted(allowed)})

    user = await db.get(User, current.id)
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    prefs = user.preferences if isinstance(user.preferences, dict) else {}
    prefs = dict(prefs)  # 复制, 避免原地改 JSON 列 mutation 未刷新的坑
    prefs["preferred_model"] = body.model
    user.preferences = prefs
    await db.commit()
    return {"preferred_model": body.model}
