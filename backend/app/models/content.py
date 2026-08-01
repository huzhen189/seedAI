"""新数据库的租户、内容与不可变 Artifact 真相模型。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, LongText, TimestampMixin, UnsignedBigInt, enum_type


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("account", name="uq_users_account"),
        UniqueConstraint("email", name="uq_users_email"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(enum_type("user_role", "user", "admin", "super_admin"), default="user", nullable=False)
    tier: Mapped[str] = mapped_column(enum_type("user_tier", "free", "pro", "max"), default="free", nullable=False)
    status: Mapped[str] = mapped_column(enum_type("user_status", "active", "disabled"), default="active", nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_user_created", "user_id", "created_at"),
        CheckConstraint("lock_version >= 1", name="lock_version_positive"),
        CheckConstraint("purge_generation >= 0", name="purge_generation_nonnegative"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(enum_type("project_status", "draft", "active", "trashed", "purging"), default="draft", nullable=False)
    site_spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    head_artifact_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    published_artifact_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    active_deployment_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    lock_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    purge_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_project_created", "project_id", "created_at"),
        Index("ix_conversations_user_updated", "user_id", "updated_at"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(enum_type("conversation_status", "active", "archived"), default="active", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    canonical_sir_snapshot_id: Mapped[int | None] = mapped_column(UnsignedBigInt)


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_turn", "turn_id"),
        UniqueConstraint("turn_id", "role", name="uq_messages_turn_role"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    turn_id: Mapped[str | None] = mapped_column(String(26))
    role: Mapped[str] = mapped_column(enum_type("message_role", "user", "assistant", "system"), nullable=False)
    content: Mapped[str] = mapped_column(LongText(), nullable=False)
    content_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    model_slot: Mapped[str | None] = mapped_column(String(32))


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_artifacts_project_version"),
        Index("ix_artifacts_project_status", "project_id", "status"),
        Index("ix_artifacts_manifest_digest", "manifest_digest"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(UnsignedBigInt, ForeignKey("conversations.id", ondelete="SET NULL"))
    parent_artifact_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    site_spec_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    site_spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    checksums: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    vendor_manifest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(enum_type("artifact_status", "building", "verified", "preview_ready", "failed", "deleted"), default="building", nullable=False)
    preview_path: Mapped[str | None] = mapped_column(String(1024))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class UserModelKey(Base, TimestampMixin):
    __tablename__ = "user_model_keys"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_user_model_keys_user_provider"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    encrypted_key: Mapped[str] = mapped_column(LongText(), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(enum_type("model_key_status", "active", "disabled", "invalid"), default="active", nullable=False)
    last_validated_at: Mapped[str | None] = mapped_column(String(64))
