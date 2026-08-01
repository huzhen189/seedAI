"""M2 运行真相模型：Turn、审批、部署、检查点与事务 Outbox。

这些表是后续 S0-S9 的持久化骨架。Repository 只操作单表；跨表事务、审批消费和
领域写入将在 Service/UnitOfWork 层实现。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin, LongText, UnsignedBigInt, enum_type


class Turn(Base, CreatedAtMixin):
    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("turn_id", name="uq_turns_turn_id"),
        UniqueConstraint("user_id", "client_msg_id", name="uq_turns_user_client_msg"),
        UniqueConstraint("stream_id", name="uq_turns_stream_id"),
        Index("ix_turns_conversation_created", "conversation_id", "created_at"),
        Index("ix_turns_status_created", "status", "created_at"),
        CheckConstraint("run_epoch >= 0", name="run_epoch_nonnegative"),
        CheckConstraint("lock_version >= 1", name="lock_version_positive"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    client_msg_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    stream_id: Mapped[str] = mapped_column(String(26), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        enum_type(
            "turn_status",
            "accepted",
            "running",
            "waiting_clarification",
            "waiting_approval",
            "paused",
            "recovery_pending",
            "needs_manual",
            "completed",
            "failed",
            "cancelled",
            "blocked",
        ),
        default="accepted",
        nullable=False,
    )
    run_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fencing_token: Mapped[str] = mapped_column(String(64), nullable=False)
    last_event_id: Mapped[str | None] = mapped_column(String(64))
    terminal_error_code: Mapped[str | None] = mapped_column(String(96))
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class TurnCheckpoint(Base, CreatedAtMixin):
    __tablename__ = "turn_checkpoints"
    __table_args__ = (
        UniqueConstraint("turn_id", "run_epoch", name="uq_turn_checkpoints_turn_epoch"),
        Index("ix_turn_checkpoints_turn_epoch", "turn_id", "run_epoch"),
        CheckConstraint("run_epoch >= 0", name="run_epoch_nonnegative"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("turns.turn_id", ondelete="CASCADE"), nullable=False
    )
    run_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0", nullable=False)
    code_version: Mapped[str | None] = mapped_column(String(64))
    config_version: Mapped[str | None] = mapped_column(String(64))
    plan_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    plan_hash: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(64))
    task_input_hash: Mapped[str | None] = mapped_column(String(64))
    dependency_output_hashes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    completed_operation_keys: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tool_result_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    sir_before_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    sir_after_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    artifact_ids: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(26))
    usage_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    response_fragment_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    last_committed_event_id: Mapped[str | None] = mapped_column(String(64))
    partial_draft: Mapped[str | None] = mapped_column(LongText())


class Approval(Base, CreatedAtMixin):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_approvals_approval_id"),
        Index("ix_approvals_turn_status", "turn_id", "status"),
        Index("ix_approvals_expires", "expires_at"),
        CheckConstraint("plan_revision >= 0", name="plan_revision_nonnegative"),
        CheckConstraint("lock_version >= 1", name="lock_version_positive"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    approval_id: Mapped[str] = mapped_column(String(26), nullable=False)
    turn_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("turns.turn_id", ondelete="CASCADE"), nullable=False
    )
    plan_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(128))
    artifact_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    manifest_digest: Mapped[str | None] = mapped_column(String(64))
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(
        enum_type("approval_risk_level", "high", "critical"), nullable=False
    )
    step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    challenge_nonce_hash: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        enum_type(
            "approval_status",
            "pending_first",
            "first_confirmed",
            "pending_second",
            "approved",
            "rejected",
            "expired",
            "consumed",
            "invalidated",
        ),
        default="pending_first",
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    decided_by: Mapped[int | None] = mapped_column(UnsignedBigInt)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[str] = mapped_column(String(64), nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Deployment(Base, CreatedAtMixin):
    __tablename__ = "deployments"
    __table_args__ = (
        Index("ix_deployments_project_created", "project_id", "created_at"),
        Index("ix_deployments_artifact_environment", "artifact_id", "environment"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        enum_type("deployment_status", "pending", "uploading", "health_checking", "succeeded", "failed"),
        default="pending",
        nullable=False,
    )
    previous_deployment_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    health_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    object_prefix: Mapped[str | None] = mapped_column(String(1024))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base, CreatedAtMixin):
    """不含项目 FK 的控制线事件；sink 必须自行校验 tombstone/generation。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_outbox_events_event_key"),
        Index("ix_outbox_events_status_created", "status", "created_at"),
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_type("outbox_event_status", "pending", "processing", "delivered", "dead"),
        default="pending",
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(96))
