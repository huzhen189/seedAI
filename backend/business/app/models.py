"""SQLAlchemy 模型。M0 仅 User;M1 增补 Project/Conversation/Message 支撑对话持久化。"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # 邮箱可空: 未提供邮箱的用户存 NULL(唯一索引允许多个 NULL); 去掉 default='' 避免 None 被替换成 ''
    email: Mapped[Optional[str]] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin | super_admin
    plan: Mapped[str] = mapped_column(String(16), default="free")  # 收费预留
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Project(Base):
    """项目:用户创建的网站生成项目,1—N 会话。"""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(128), default="未命名项目")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    # 分享(⑤-b):share_id 为公开访问令牌(UUID,可空);is_public 控制是否允许公开访问;
    # preview_url 缓存最新一次生成的 COS 预览直链,供「复制预览链接」按钮使用。
    share_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    preview_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Conversation(Base):
    """会话:归属某项目,1—N 消息;左栏按 updated_at 排序。"""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Message(Base):
    """消息:单条对话内容(user/assistant);SSE 结束双写落库。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    model_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 链路 id:同一 trace_id 的多次(重连/续传)SSE 落库据此幂等 —— 用户消息只插一次,
    # assistant 消息按 trace_id upsert,避免刷新/重连导致重复行(§15.3 / 重连机制)。
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------- 对话追踪 / 反馈 / 用量(③-a · 文档 §3.13) ----------
class Trace(Base):
    """一次 SSE 生成会话 = 一个 Trace(可回放 / 质量统计)。"""

    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    model_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|error|aborted
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class TraceEvent(Base):
    """Trace 的结构化事件序列(按 seq 追加),用于前端回放与质量指标。

    token 事件量大,不逐条落库,仅记录聚合 token 数(见 Trace.total_tokens);
    其余结构化事件(node/think/plan/error/done/aborted/degraded)逐条落库。
    """

    __tablename__ = "trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String(16))  # node|think|plan|token|error|done|aborted|degraded
    stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON 字符串
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Feedback(Base):
    """用户对一次生成的评价(1—10 分 + 评论);统计 + 回归数据集(文档 §3.11/#36)。"""

    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    rating: Mapped[int] = mapped_column(Integer)  # 1-10
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UsageLog(Base):
    """每次生成的用量账本(成本归集 / 运营统计,文档 §3.12 / D1)。"""

    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
