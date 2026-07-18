"""Pydantic 请求/响应模型。"""
from pydantic import BaseModel, EmailStr, Field


class RegisterReq(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    email: EmailStr | None = None


class LoginReq(BaseModel):
    username: str
    password: str


class TokenResp(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResp(BaseModel):
    id: int
    username: str
    email: str
    role: str
    plan: str


class RefreshReq(BaseModel):
    refresh_token: str
