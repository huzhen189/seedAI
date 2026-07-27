"""Shared ORM models — single source of truth for business & agent.

Project 1:N Conversation (explicit per user requirement):
  - Project = asset / site container (system_prompt, requirement_doc, build_status, share)
  - Conversation = one dialogue inside a project
  - Message -> Conversation -> Project -> User
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

logger = logging.getLogger("shared.models")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    nickname: Mapped[str | None] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)  # user | super_admin
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)  # free | pro | enterprise
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    projects: Mapped[list["Project"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="user")


class Project(Base):
    """Asset / site container. One project owns many conversations."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="未命名项目", nullable=False)
    share_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    preview_url: Mapped[str | None] = mapped_column(String(512))
    system_prompt: Mapped[str | None] = mapped_column(Text)
    build_status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    requirement_doc: Mapped[str | None] = mapped_column(JSON)  # structured requirement JSON
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # soft delete
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    owner: Mapped["User"] = relationship(back_populates="projects")
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    @property
    def title(self) -> str:
        """向后兼容只读别名(单进程合并时旧调用方可能仍读 title)。统一命名后逐步移除。"""
        return self.name


class Conversation(Base):
    """A single dialogue inside a project."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="新对话", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    checkpoint_stage: Mapped[str | None] = mapped_column(String(64))
    checkpoint_data: Mapped[str | None] = mapped_column(Text)  # JSON snapshot
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="conversations")
    user: Mapped["User"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")

    @property
    def title(self) -> str:
        """向后兼容只读别名(单进程合并时旧调用方/前端可能仍读 title)。统一命名后逐步移除。"""
        return self.name


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conv_created", "conversation_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(48))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    parent_msg_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class Artifact(Base):
    """Versioned generated artifact (site / doc) stored on COS."""

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))  # 前端展示标题(与 name 并存, 历史 name 保留)
    url: Mapped[str | None] = mapped_column(String(512))  # 保留兼容列(历史索引),业务实际使用 preview_url
    # ── 以下为落库/前端展示所需字段(C1 补齐: 此前缺失导致每次建站 AttributeError 500) ──
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    repo: Mapped[str | None] = mapped_column(String(32), default="site")
    preview_url: Mapped[str | None] = mapped_column(String(512))
    download_url: Mapped[str | None] = mapped_column(String(512))
    files: Mapped[dict | None] = mapped_column(JSON)  # dict{name -> {name, size, url/content}}
    status: Mapped[str | None] = mapped_column(String(32), default="done")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_artifacts_project_version", "project_id", "version"),
    )


class Trace(Base):
    """Per-stream call record for analytics / admin."""

    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    model_id: Mapped[str | None] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # ── C1 补齐(可空, 防御性): 让 trace 起止时间可被记录/统计 ──
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


# ── 对话追踪补充表(③-a 回放 / QC 雷达图 / 多维反馈) ──
# 合并自 business/app/models.py 的单表定义, 统一为单一事实源。
class TraceEvent(Base):
    """Trace 的结构化事件序列(按 seq 追加),用于前端回放与质量指标。"""

    __tablename__ = "trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String(16))
    stage: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class Feedback(Base):
    """用户对一次生成的评价(1—10 分 + 评论 + 多维细分);统计 + 回归数据集。"""

    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    dimensions: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class QcScore(Base):
    """后置 QC 三裁判评分: 以 trace_id 串联生成, 供后台雷达图复盘。"""

    __tablename__ = "qc_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), index=True)
    model_id: Mapped[str | None] = mapped_column(String(48))
    overall: Mapped[float] = mapped_column(default=0.0)
    result: Mapped[dict | None] = mapped_column(JSON)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    safety_risk: Mapped[str] = mapped_column(String(16), default="low")
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class UsageLog(Base):
    """每次生成的用量账本(成本归集 / 运营统计)。"""

    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class UserState(Base):
    """用户级「我的状态」入口索引(断点复联三场景权威状态源)。

    与 conversations.checkpoint_* / traces.status 互补: 本表是「用户级入口」
    (上一次在哪个项目/会话、任务跑到哪、是否需要续跑), conversations/traces 是任务级明细。
    详见 docs/my-info-state-design.md(v4)。
    """

    __tablename__ = "user_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    current_project_id: Mapped[int | None] = mapped_column(Integer, index=True)
    current_conversation_id: Mapped[int | None] = mapped_column(Integer, index=True)
    active_trace_id: Mapped[str | None] = mapped_column(String(64), index=True)  # 最近一次生成链路 id(字符串), 用于续跑
    status: Mapped[str] = mapped_column(String(20), default="idle", nullable=False)  # idle/running/paused/aborted/done/error
    current_stage: Mapped[str | None] = mapped_column(String(40))
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(String(20))  # user_interrupt / offline_timeout
    pending_decision: Mapped[str | None] = mapped_column(String(30))  # continue_instruction / retry_model / ...
    checkpoint_stage: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_user_states_user", "user_id", unique=True),
    )
