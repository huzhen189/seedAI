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
