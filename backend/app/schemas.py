"""Pydantic 请求/响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterReq(BaseModel):
    account: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    nickname: str | None = Field(None, max_length=64)
    email: EmailStr | None = None


class LoginReq(BaseModel):
    account: str
    password: str


class TokenResp(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account: str
    display_name: str = ""
    email: str | None = None
    role: str
    tier: str


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
    name: str | None = Field(None, max_length=255)


class ProjectResp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    name: str
    created_at: datetime
    updated_at: datetime
    share_id: str | None = None
    is_public: bool = False
    preview_url: str | None = None
    requirement_doc: str | None = None  # 需求分析文档(JSON 字符串, 前端重启后还原)


class MessageResp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    conversation_id: int
    role: str
    content: str
    model_id: str | None = None
    trace_id: str | None = None
    created_at: datetime


class ConversationResp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    user_id: int
    name: str | None = None
    created_at: datetime
    updated_at: datetime


class AutoStartReq(BaseModel):
    """首条对话自动建项目+会话: 按对话文本自动命名。"""
    text: str = Field(min_length=1, max_length=2000)
    # 注意: from_attributes 默认会按字段名去读 ORM 的 `messages` relationship(惰性加载),
    # 在同步序列化上下文触发异步查询 -> MissingGreenlet 500。改用 validation_alias 让其
    # 在 from_attributes 时找不到对应属性而落到 default, 避免误触发关系查询。
    # 需要真实消息列表时由 get_conversation 路由显式填充 resp.messages。
    messages: list[MessageResp] = Field(default_factory=list, validation_alias="_messages_disabled")


class SearchItemResp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    type: str  # project | conversation
    id: int
    title: str
    project_id: int | None = None


# ---------- 管理后台(§3 RBAC) ----------
class AdminUserResp(BaseModel):
    """管理后台用户列表项(敏感字段不外泄)。"""

    model_config = ConfigDict(from_attributes=True)
    id: int
    account: str
    display_name: str = ""
    email: str | None = None
    role: str
    tier: str


class SetRoleReq(BaseModel):
    """变更用户角色(super_admin 仅可由种子/已有 super_admin 赋予,不能被降级)。"""

    role: str = Field(pattern="^(user|admin|super_admin)$")


class SetTierReq(BaseModel):
    """变更用户套餐等级(v3: tier 枚举 free/pro/max; 原 plan 字段已并入 tier)。"""

    tier: str = Field(min_length=1, max_length=16)


# ---------- 对话反馈(③-a:1-10 评分 + 评论) ----------
class FeedbackReq(BaseModel):
    """提交一次生成的评价;trace_id 关联 Trace/Message,供统计与回归数据集。

    dimensions: 气泡内 6 维细分评分(可选), 键=QC_DIMENSIONS,
    值=1-10 整数; 缺省 None(旧评价 / 仅整体评分)。
    """

    trace_id: str = Field(min_length=1, max_length=64)
    conversation_id: int | None = None
    rating: int = Field(ge=1, le=10)  # 1-10 分(整体)
    comment: str | None = None
    dimensions: dict | None = None  # {"correctness": int, ..., "safety": int}
