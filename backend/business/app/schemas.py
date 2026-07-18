"""Pydantic 请求/响应模型。"""
from pydantic import BaseModel, EmailStr, Field


class RegisterReq(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    nickname: str | None = Field(None, max_length=64)
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
    nickname: str = ""
    email: str
    role: str
    plan: str


class UpdateMeReq(BaseModel):
    """修改当前用户信息(昵称/邮箱/密码)。字段均可选,只更新传入项。"""

    nickname: str | None = Field(None, max_length=64)
    email: EmailStr | None = None
    old_password: str | None = None
    new_password: str | None = Field(None, min_length=6, max_length=128)


class RefreshReq(BaseModel):
    refresh_token: str
