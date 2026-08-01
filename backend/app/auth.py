"""新数据库认证 API。"""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.repositories import users
from app.models import User
from app.security import CurrentUser, clear_auth_cookies, create_access_token, decode_token, get_current_user, hash_password, set_auth_cookies, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    account: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    account: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/register", status_code=201)
async def register(body: RegisterRequest, response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
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
    set_auth_cookies(response, user)
    return {"user": {"id": user.id, "account": user.account, "display_name": user.display_name, "role": user.role, "tier": user.tier}}


@router.post("/login")
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    user = await users.by_account(db, body.account)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS"})
    if user.status != "active":
        raise HTTPException(status_code=403, detail={"code": "ACCOUNT_DISABLED"})
    set_auth_cookies(response, user)
    return {"user": {"id": user.id, "account": user.account, "display_name": user.display_name, "role": user.role, "tier": user.tier}, "access_token": create_access_token(user)}


@router.post("/refresh")
async def refresh(response: Response, db: AsyncSession = Depends(get_db), current: CurrentUser = Depends(get_current_user)) -> dict[str, str]:
    user = await db.get(User, current.id)
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN"})
    set_auth_cookies(response, user)
    return {"access_token": create_access_token(user)}


@router.post("/logout", status_code=204)
async def logout(response: Response) -> None:
    clear_auth_cookies(response)


@router.get("/me")
async def me(current: CurrentUser = Depends(get_current_user)) -> dict[str, object]:
    return {"id": current.id, "account": current.account, "role": current.role, "tier": current.tier}
