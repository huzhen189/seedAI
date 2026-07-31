from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base,
    CreatedAtMixin,
    LongText,
    TimestampMixin,
    UnsignedBigInt,
    UnsignedSmallInt,
    UnsignedTinyInt,
    enum_type,
)


logger = logging.getLogger("app.models.content")


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("token_budget_daily >= 0", name="token_budget_nonnegative"),
        CheckConstraint("max_concurrent_sessions >= 1", name="sessions_positive"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(
        enum_type("user_status", "active", "disabled"), default="active", nullable=False
    )
    preferences: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    tier: Mapped[str] = mapped_column(
        enum_type("user_tier", "free", "pro", "max"), default="free", nullable=False
    )
    token_budget_daily: Mapped[int] = mapped_column(
        UnsignedBigInt, default=5_000_000, nullable=False
    )
    max_concurrent_sessions: Mapped[int] = mapped_column(
        UnsignedSmallInt, default=5, nullable=False
    )
    preferred_exec_model: Mapped[str] = mapped_column(
        enum_type("preferred_exec_model", "standard", "pro", "ultra"),
        default="standard",
        nullable=False,
    )

    account: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    nickname: Mapped[str | None] = mapped_column(String(64))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    projects: Mapped[list[Project]] = relationship(back_populates="owner")
    conversations: Mapped[list[Conversation]] = relationship(back_populates="user")


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_user_created", "user_id", "created_at"),
        CheckConstraint("token_budget_daily >= 0", name="token_budget_nonnegative"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        enum_type("project_status", "draft", "active", "trashed", "purging", "deleted"),
        default="draft",
        nullable=False,
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=lambda: {"active_version": 1}, nullable=False
    )
    requirement_doc: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    token_budget_daily: Mapped[int] = mapped_column(
        UnsignedBigInt, default=5_000_000, nullable=False
    )
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    share_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    preview_url: Mapped[str | None] = mapped_column(String(512))
    system_prompt: Mapped[str | None] = mapped_column(LongText())
    build_status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)

    owner: Mapped[User] = relationship(back_populates="projects")
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="project", passive_deletes=True
    )

    @property
    def title(self) -> str:
        return self.name


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_project_created", "project_id", "created_at"),
        Index("ix_conversations_user_status_updated", "user_id", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(128), default="新对话", nullable=False)
    mode: Mapped[str] = mapped_column(
        enum_type("conversation_mode", "chat", "build", "design", "review", "doc"),
        default="chat",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        enum_type("conversation_status", "active", "archived", "trashed"),
        default="active",
        nullable=False,
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint_stage: Mapped[str | None] = mapped_column(String(64))
    checkpoint_data: Mapped[str | None] = mapped_column(LongText())
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    project: Mapped[Project] = relationship(back_populates="conversations")
    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", passive_deletes=True
    )


class Message(Base, CreatedAtMixin):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_conversation_turn", "conversation_id", "turn_no"),
        CheckConstraint("turn_no >= 0", name="turn_nonnegative"),
        CheckConstraint("token_input >= 0", name="token_input_nonnegative"),
        CheckConstraint("token_output >= 0", name="token_output_nonnegative"),
        CheckConstraint("latency_ms >= 0", name="latency_nonnegative"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(
        enum_type("message_role", "user", "assistant", "system", "tool"), nullable=False
    )
    content: Mapped[str] = mapped_column(LongText(), nullable=False)
    content_summary: Mapped[str | None] = mapped_column(String(512))
    content_path: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    model: Mapped[str | None] = mapped_column(String(64))
    model_id: Mapped[str | None] = mapped_column(String(64))
    token_input: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_output: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sir_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    parent_msg_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("messages.id", ondelete="SET NULL")
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_conversation_status", "conversation_id", "status"),
        Index("ix_tasks_parent", "parent_task_id"),
        CheckConstraint("priority >= 0 AND priority <= 9", name="priority_range"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    parent_task_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("tasks.id", ondelete="SET NULL")
    )
    intent: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(
        enum_type("task_kind", "plan", "react"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        enum_type("task_status", "pending", "running", "done", "failed", "cancelled"),
        default="pending",
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        enum_type("task_source", "planner", "replanner", "user_split", "default"),
        default="default",
        nullable=False,
    )
    deps: Mapped[list[int] | None] = mapped_column(JSON)
    priority: Mapped[int] = mapped_column(UnsignedTinyInt, default=5, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ToolCall(Base, CreatedAtMixin):
    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_message", "message_id"),
        Index("ix_tool_calls_tool_created", "tool_name", "created_at"),
        Index("ix_tool_calls_conversation_created", "conversation_id", "created_at"),
        UniqueConstraint("idempotency_key", name="uq_tool_calls_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(48), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result_summary: Mapped[str | None] = mapped_column(String(512))
    risk_level: Mapped[str] = mapped_column(
        enum_type("tool_risk_level", "low", "mid", "high", "critical"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        enum_type("tool_call_status", "pending", "success", "error"),
        default="pending",
        nullable=False,
    )
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))


class SirSnapshot(Base, CreatedAtMixin):
    __tablename__ = "sir_snapshots"
    __table_args__ = (
        Index("ix_sir_snapshots_conversation_turn", "conversation_id", "turn_no"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    prev_snapshot_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("sir_snapshots.id", ondelete="SET NULL")
    )


class SessionAudit(Base, CreatedAtMixin):
    __tablename__ = "session_audits"
    __table_args__ = (
        Index("ix_session_audits_conversation_turn_stage", "conversation_id", "turn_no", "stage"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    event: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_conversation_started", "conversation_id", "started_at"),
        CheckConstraint("token_input >= 0", name="token_input_nonnegative"),
        CheckConstraint("token_output >= 0", name="token_output_nonnegative"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[str] = mapped_column(String(48), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        enum_type("agent_run_status", "running", "completed", "failed", "aborted"),
        default="running",
        nullable=False,
    )
    token_input: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_output: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryStorageLog(Base, CreatedAtMixin):
    __tablename__ = "memory_storage_log"
    __table_args__ = (Index("ix_memory_storage_log_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE")
    )
    collection: Mapped[str] = mapped_column(String(80), nullable=False)
    doc_id: Mapped[str | None] = mapped_column(String(120))
    decision: Mapped[str] = mapped_column(
        enum_type("memory_decision", "store", "skip"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(255))


class Feedback(Base, CreatedAtMixin):
    __tablename__ = "feedback"
    __table_args__ = (
        Index("ix_feedback_conversation", "conversation_id"),
        Index("ix_feedback_message", "message_id"),
        CheckConstraint("rating >= 1 AND rating <= 10", name="rating_range"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(UnsignedTinyInt, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(512))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    dimensions: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class UsageLedger(Base, CreatedAtMixin):
    __tablename__ = "usage_ledger"
    __table_args__ = (
        Index("ix_usage_ledger_user_created", "user_id", "created_at"),
        UniqueConstraint("idempotency_key", name="uq_usage_ledger_idempotency_key"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))


class RecycleBin(Base, CreatedAtMixin):
    __tablename__ = "recycle_bin"
    __table_args__ = (
        Index("ix_recycle_bin_user_trashed", "user_id", "trashed_at"),
        Index("ix_recycle_bin_resource", "resource_type", "resource_id"),
        UniqueConstraint("resource_type", "resource_id", name="uq_recycle_bin_resource"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(
        enum_type("recycle_resource_type", "project"), default="project", nullable=False
    )
    resource_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    original_name: Mapped[str | None] = mapped_column(String(255))
    trashed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_state: Mapped[str] = mapped_column(
        enum_type("recycle_purge_state", "pending", "purging", "purged", "restored"),
        default="pending",
        nullable=False,
    )


class PurgeJob(Base, CreatedAtMixin):
    __tablename__ = "purge_jobs"
    __table_args__ = (
        Index("ix_purge_jobs_user", "user_id"),
        Index("ix_purge_jobs_status", "status"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(
        enum_type("purge_resource_type", "project"), default="project", nullable=False
    )
    resource_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_type("purge_job_status", "queued", "running", "done", "failed"),
        default="queued",
        nullable=False,
    )
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base, CreatedAtMixin):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_project_version", "project_id", "version"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(512))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    repo: Mapped[str | None] = mapped_column(String(32), default="site")
    preview_url: Mapped[str | None] = mapped_column(String(512))
    download_url: Mapped[str | None] = mapped_column(String(512))
    preview_path: Mapped[str | None] = mapped_column(String(512))
    files: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str | None] = mapped_column(String(32), default="done")


class Trace(Base, TimestampMixin):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="SET NULL")
    )
    project_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    model_id: Mapped[str | None] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TraceEvent(Base, CreatedAtMixin):
    __tablename__ = "trace_events"

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[str | None] = mapped_column(LongText())


class UsageLog(Base, CreatedAtMixin):
    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    trace_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"), nullable=False)


class UserState(Base):
    __tablename__ = "user_states"

    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    current_project_id: Mapped[int | None] = mapped_column(UnsignedBigInt, index=True)
    current_conversation_id: Mapped[int | None] = mapped_column(UnsignedBigInt, index=True)
    active_trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="idle", nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(40))
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(String(20))
    pending_decision: Mapped[str | None] = mapped_column(String(30))
    checkpoint_stage: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
