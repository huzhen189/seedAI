"""Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    nickname: str = ""
    email: str | None = None
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


# ---------- 项目 / 会话 / 消息 (M1) ----------
class CreateProjectReq(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class RenameReq(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class CreateConversationReq(BaseModel):
    project_id: int
    title: str | None = Field(None, max_length=255)


class ProjectResp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    name: str
    created_at: datetime
    updated_at: datetime


class MessageResp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    conversation_id: int
    role: str
    content: str
    model_id: str | None = None
    created_at: datetime


class ConversationResp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    user_id: int
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResp] = []


class SearchItemResp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    type: str  # project | conversation
    id: int
    title: str
    project_id: int | None = None
