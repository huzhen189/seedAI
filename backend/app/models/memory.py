"""记忆模块 v2 数据模型（MySQL Source-of-Truth × Vector Semantic Index）。

见 docs/plan-memory-v2-landing.md §1。五张表：
  - user_facts       用户强事实（KV，零容错）
  - project_facts    项目强事实（KV，零容错）
  - project_events   项目过程记忆/审计事件（不进 prompt）
  - user_soft_preferences 用户软偏好（仅向量召回 rerank，不进 prompt）
  - memories         长期语义记忆元数据（MySQL 真相行，向量只引 source_id）

所有表由 TimestampMixin 注入 created_at/updated_at（满足要求#1）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, LongText, TimestampMixin, UnsignedBigInt, enum_type


class UserFact(Base, TimestampMixin):
    """[TS] created_at/updated_at 由 TimestampMixin 注入（满足要求#1）。

    用户强事实（KV 表，结构化、强一致、零容错）：喜好/禁忌/权限/地理。
    VARCHAR(512) 只装短事实（如 '城市=深圳'/'禁忌=不要红色'）；模糊经验走
    UserSoftPreference / memories 的 summary。
    """

    __tablename__ = "user_facts"
    __table_args__ = (
        UniqueConstraint("user_id", "category", "key_name", name="uq_user_facts_user_cat_key"),
        Index("ix_user_facts_user", "user_id"),
        Index("ix_user_facts_user_cat", "user_id", "category"),
        CheckConstraint("confidence BETWEEN 0 AND 100", name="ck_user_fact_conf"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(
        enum_type("user_fact_category", "preference", "taboo", "permission", "geo"), nullable=False
    )
    key_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(
        enum_type("user_fact_source", "stated", "extracted", "imported"), default="extracted", nullable=False
    )
    confidence: Mapped[int] = mapped_column(Integer, default=90, nullable=False)


class ProjectFact(Base, TimestampMixin):
    """[TS] created_at/updated_at 由 TimestampMixin 注入（满足要求#1）。

    项目事实（KV 表，结构化、零容错）：技术栈/版本/域名/约束/状态。
    """

    __tablename__ = "project_facts"
    __table_args__ = (
        UniqueConstraint("project_id", "category", "key_name", name="uq_project_facts_proj_cat_key"),
        Index("ix_project_facts_project", "project_id"),
        Index("ix_project_facts_proj_cat", "project_id", "category"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(
        enum_type("project_fact_category", "stack", "version", "domain", "constraint", "status"), nullable=False
    )
    key_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(
        enum_type("project_fact_source", "stated", "extracted", "imported"), default="extracted", nullable=False
    )


class ProjectEvent(Base, TimestampMixin):
    """[TS] 项目过程记忆 / 审计事件（不进 prompt）。

    记录"发生了什么"（建站/改版/发布/报错/调 API 等），偏审计。事件本身不直接进
    prompt，而是被异步摘要后写进 memories(kind=proj_summary) 再间接入 L5 向量召回。
    """

    __tablename__ = "project_events"
    __table_args__ = (
        Index("ix_project_events_project_time", "project_id", "created_at"),
        Index("ix_project_events_project_kind", "project_id", "kind"),
        Index("ix_project_events_source_message", "source_message_id"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(
        enum_type("project_event_kind", "create", "edit", "publish", "error", "api_call", "other"),
        nullable=False,
    )
    detail: Mapped[str] = mapped_column(LongText(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("messages.id", ondelete="SET NULL")
    )
    embedding_status: Mapped[str] = mapped_column(
        enum_type("event_embedding_status", "pending", "ready", "failed", "skipped"),
        default="pending", nullable=False,
    )


class UserSoftPreference(Base, TimestampMixin):
    """[TS] 用户软偏好（不进 prompt，仅用于向量召回 rerank）。

    场景化经验、跨会话语义偏好（如"做科技风时偏好深色背景"）。与 UserFact 的硬事实区分：
    软偏好不进入 prompt 强事实段、不参与零容错断言，而是作为向量召回命中后的重排序信号。
    """

    __tablename__ = "user_soft_preferences"
    __table_args__ = (
        Index("ix_user_soft_pref_user", "user_id"),
        Index("ix_user_soft_pref_user_tag", "user_id", "tag"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(LongText(), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    embedding_status: Mapped[str] = mapped_column(
        enum_type("soft_pref_embedding_status", "pending", "ready", "failed"),
        default="pending", nullable=False,
    )


class Memory(Base, TimestampMixin):
    """[TS] 长期语义记忆元数据（MySQL 真相行）。

    向量库只持 (source_type, source_id) + 标题索引串；命中后回查本行取原文/摘要。
    双向关联：向量 metadata.(source_type, source_id) ⟷ 本行 id；
             本行 source_message_id ⟷ messages.id（反向溯源）。
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_user_kind", "user_id", "kind"),
        Index("ix_memories_project_kind", "project_id", "kind"),
        Index("ix_memories_conversation", "conversation_id"),
        Index("ix_memories_source_message", "source_message_id"),
        Index("ix_memories_embedding_status", "embedding_status"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE")
    )
    project_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(
        enum_type(
            "memory_kind",
            "preference",
            "proj_exp",
            "proj_summary",
            "conv_summary",
            "soft_pref",
        ),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(
        enum_type("memory_source_type", "message", "project_event", "user_soft_pref"),
        default="message", nullable=False,
    )
    source_message_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("messages.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    summary: Mapped[str] = mapped_column(LongText(), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    embedding_status: Mapped[str] = mapped_column(
        enum_type("memory_embedding_status", "pending", "ready", "failed"),
        default="pending", nullable=False,
    )


__all__ = [
    "Memory",
    "ProjectEvent",
    "ProjectFact",
    "UserFact",
    "UserSoftPreference",
]
