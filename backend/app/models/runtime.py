"""新数据库的 Turn、审批、操作账本、部署、Outbox 与 purge 真相模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, LongText, TimestampMixin, UnsignedBigInt, enum_type


class Turn(Base, TimestampMixin):
    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("turn_id", name="uq_turns_turn_id"),
        UniqueConstraint("user_id", "client_msg_id", name="uq_turns_user_client_message"),
        UniqueConstraint("stream_id", name="uq_turns_stream_id"),
        Index("ix_turns_conversation_created", "conversation_id", "created_at"),
        Index("ix_turns_status_created", "status", "created_at"),
        CheckConstraint("run_epoch >= 0", name="run_epoch_nonnegative"),
        CheckConstraint("lock_version >= 1", name="lock_version_positive"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    client_msg_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    stream_id: Mapped[str] = mapped_column(String(26), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        enum_type("turn_status", "accepted", "running", "waiting_clarification", "waiting_approval", "paused", "recovery_pending", "needs_manual", "completed", "failed", "cancelled", "blocked"),
        default="accepted",
        nullable=False,
    )
    run_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fencing_token: Mapped[str] = mapped_column(String(64), nullable=False)
    last_event_id: Mapped[str | None] = mapped_column(String(64))
    terminal_error_code: Mapped[str | None] = mapped_column(String(96))
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class TurnCheckpoint(Base, TimestampMixin):
    __tablename__ = "turn_checkpoints"
    __table_args__ = (
        UniqueConstraint("turn_id", "run_epoch", name="uq_turn_checkpoints_turn_epoch"),
        Index("ix_turn_checkpoints_turn_epoch", "turn_id", "run_epoch"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(String(26), ForeignKey("turns.turn_id", ondelete="CASCADE"), nullable=False)
    run_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0", nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    plan_hash: Mapped[str | None] = mapped_column(String(64))
    sir_before_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    sir_after_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    completed_operation_keys: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    partial_draft: Mapped[str | None] = mapped_column(LongText())
    last_committed_event_id: Mapped[str | None] = mapped_column(String(64))


class SirSnapshot(Base, TimestampMixin):
    __tablename__ = "sir_snapshots"
    __table_args__ = (Index("ix_sir_snapshots_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(26), ForeignKey("turns.turn_id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(enum_type("sir_snapshot_kind", "base", "provisional", "canonical"), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    prev_snapshot_id: Mapped[int | None] = mapped_column(UnsignedBigInt)


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_turn_status", "turn_id", "status"),
        UniqueConstraint("turn_id", "plan_revision", "task_key", name="uq_tasks_turn_revision_key"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(String(26), ForeignKey("turns.turn_id", ondelete="CASCADE"), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    task_key: Mapped[str] = mapped_column(String(128), nullable=False)
    domain: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(enum_type("task_status", "pending", "running", "done", "failed", "cancelled"), default="pending", nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dependency_output_hashes: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    result_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supersedes_task_id: Mapped[int | None] = mapped_column(UnsignedBigInt)


class ToolCall(Base, TimestampMixin):
    """副作用 Tool 的 W0 operation ledger。"""

    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_tool_calls_operation_key"),
        Index("ix_tool_calls_turn_status", "turn_id", "status"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(String(26), ForeignKey("turns.turn_id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(160), nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(enum_type("operation_status", "running", "succeeded", "failed", "unknown"), default="running", nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String(1024))
    error_code: Mapped[str | None] = mapped_column(String(96))
    fencing_token: Mapped[str] = mapped_column(String(64), nullable=False)


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_approvals_approval_id"),
        Index("ix_approvals_turn_status", "turn_id", "status"),
        Index("ix_approvals_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    approval_id: Mapped[str] = mapped_column(String(26), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(26), ForeignKey("turns.turn_id", ondelete="CASCADE"), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(128))
    artifact_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    manifest_digest: Mapped[str | None] = mapped_column(String(64))
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(enum_type("approval_risk", "high", "critical"), nullable=False)
    status: Mapped[str] = mapped_column(enum_type("approval_status", "pending_first", "first_confirmed", "pending_second", "approved", "rejected", "expired", "consumed", "invalidated"), default="pending_first", nullable=False)
    step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    challenge_nonce_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    decided_by: Mapped[int | None] = mapped_column(UnsignedBigInt)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[str] = mapped_column(String(64), nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ApprovalDecision(Base, TimestampMixin):
    __tablename__ = "approval_decisions"
    __table_args__ = (UniqueConstraint("approval_id", "decision_nonce_hash", name="uq_approval_decisions_nonce"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    approval_id: Mapped[str] = mapped_column(String(26), ForeignKey("approvals.approval_id", ondelete="CASCADE"), nullable=False)
    decision: Mapped[str] = mapped_column(enum_type("approval_decision", "approve", "reject"), nullable=False)
    decision_nonce_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_by: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)


class AuthorizationGrant(Base, TimestampMixin):
    __tablename__ = "authorization_grants"
    __table_args__ = (UniqueConstraint("approval_id", "new_epoch", name="uq_authorization_grants_approval_epoch"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    approval_id: Mapped[str] = mapped_column(String(26), ForeignKey("approvals.approval_id", ondelete="CASCADE"), nullable=False)
    approved_plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    old_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    new_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Deployment(Base, TimestampMixin):
    __tablename__ = "deployments"
    __table_args__ = (Index("ix_deployments_project_created", "project_id", "created_at"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    artifact_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(enum_type("deployment_status", "pending", "uploading", "health_checking", "succeeded", "failed"), default="pending", nullable=False)
    previous_deployment_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    health_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    object_prefix: Mapped[str | None] = mapped_column(String(1024))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageLedger(Base, TimestampMixin):
    __tablename__ = "usage_ledger"
    __table_args__ = (UniqueConstraint("turn_id", "kind", name="uq_usage_ledger_turn_kind"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(String(26), ForeignKey("turns.turn_id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    settled_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(enum_type("usage_status", "reserved", "settled", "released"), default="reserved", nullable=False)


class OutboxEvent(Base, TimestampMixin):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_outbox_events_event_key"),
        Index("ix_outbox_events_status_available", "status", "available_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(enum_type("outbox_status", "pending", "processing", "delivered", "dead"), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(96))


class ProjectTombstone(Base, TimestampMixin):
    __tablename__ = "project_tombstones"
    __table_args__ = (UniqueConstraint("project_id", "purge_generation", name="uq_project_tombstones_generation"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    purge_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(enum_type("tombstone_status", "active", "completed"), default="active", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PurgeJob(Base, TimestampMixin):
    __tablename__ = "purge_jobs"
    __table_args__ = (UniqueConstraint("project_id", "purge_generation", name="uq_purge_jobs_generation"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    purge_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(enum_type("purge_status", "queued", "running", "succeeded", "failed"), default="queued", nullable=False)
    step: Mapped[str] = mapped_column(String(64), default="freeze", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(96))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
