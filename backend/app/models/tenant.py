from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UnsignedBigInt, enum_type


logger = logging.getLogger("app.models.tenant")


class VectorCollection(Base, TimestampMixin):
    __tablename__ = "vector_collections"
    __table_args__ = (
        UniqueConstraint("collection", name="uq_vector_collections_collection"),
        Index("ix_vector_collections_owner", "scope", "owner_id"),
        Index("ix_vector_collections_status_accessed", "status", "last_accessed_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(
        enum_type("vector_scope", "user", "project", "global"), nullable=False
    )
    owner_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    collection: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_model: Mapped[str] = mapped_column(
        String(64), default="text-embedding-v3", nullable=False
    )
    dim: Mapped[int] = mapped_column(default=1024, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_type("vector_collection_status", "ready", "building", "archived", "dropped"),
        default="ready",
        nullable=False,
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserModelKey(Base, TimestampMixin):
    __tablename__ = "user_model_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_model_keys_user_provider"),
        Index("ix_user_model_keys_user", "user_id"),
        Index("ix_user_model_keys_status", "status"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    api_key_enc: Mapped[str] = mapped_column(String(512), nullable=False)
    base_url_override: Mapped[str | None] = mapped_column(String(255))
    model_map: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        enum_type("user_model_key_status", "active", "disabled", "invalid"),
        default="active",
        nullable=False,
    )
    is_valid: Mapped[bool | None] = mapped_column(Boolean)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PausedTurn(Base, TimestampMixin):
    __tablename__ = "paused_turns"
    __table_args__ = (
        UniqueConstraint("turn_id", name="uq_paused_turns_turn_id"),
        Index("ix_paused_turns_user_status", "user_id", "status"),
        Index("ix_paused_turns_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        enum_type("paused_turn_status", "paused", "resumed", "cancelled"),
        default="paused",
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(String(255))
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    completed_task_ids: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
