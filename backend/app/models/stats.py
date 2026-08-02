from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin, LongText, UnsignedBigInt, UnsignedTinyInt, enum_type


logger = logging.getLogger("app.models.stats")


class MetricsDaily(Base, CreatedAtMixin):
    __tablename__ = "metrics_daily"
    __table_args__ = (
        UniqueConstraint(
            "stat_date",
            "user_id",
            "model",
            "dimension",
            "dimension_key",
            name="uq_metrics_daily_dimensions",
        ),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    model: Mapped[str | None] = mapped_column(String(64))
    dimension: Mapped[str | None] = mapped_column(String(48))
    dimension_key: Mapped[str | None] = mapped_column(String(64))
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_input: Mapped[int] = mapped_column(UnsignedBigInt, default=0, nullable=False)
    token_output: Mapped[int] = mapped_column(UnsignedBigInt, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"), nullable=False)
    latency_p50_ms: Mapped[int | None] = mapped_column(Integer)
    latency_p90_ms: Mapped[int | None] = mapped_column(Integer)
    latency_p99_ms: Mapped[int | None] = mapped_column(Integer)
    score_relevance_avg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    score_completeness_avg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    score_accuracy_avg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    score_safety_avg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    score_efficiency_avg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    score_experience_avg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    score_overall_avg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    projects_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sites_deployed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deploy_success: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_turns_per_project: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), nullable=False
    )
    multi_intent_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0"), nullable=False
    )
    partial_failure_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0"), nullable=False
    )
    misroute_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0"), nullable=False
    )
    l4_soft_confirm_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0"), nullable=False
    )
    fallback_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0"), nullable=False
    )
    degradation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    degradation_accepted_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0"), nullable=False
    )
    guard_blocked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    guard_warned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    undisclosed_mock_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0"), nullable=False
    )
    csat_avg: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    nps: Mapped[int | None] = mapped_column(Integer)
    avg_page_load_ms: Mapped[int | None] = mapped_column(Integer)
    sse_reconnect_avg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))


class MetricsEvent(Base):
    __tablename__ = "metrics_events"
    __table_args__ = (
        Index("ix_metrics_events_user_occurred", "user_id", "occurred_at"),
        Index("ix_metrics_events_type_occurred", "event_type", "occurred_at"),
        UniqueConstraint("idempotency_key", name="uq_metrics_events_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    event_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class QcScore(Base, CreatedAtMixin):
    __tablename__ = "qc_scores"
    __table_args__ = (
        Index("ix_qc_scores_user_dimension_created", "user_id", "dimension", "created_at"),
        Index("ix_qc_scores_conversation_created", "conversation_id", "created_at"),
        CheckConstraint("score >= 0 AND score <= 100", name="score_range"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, default=0, nullable=False)
    project_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    conversation_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    message_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    dimension: Mapped[str] = mapped_column(
        enum_type(
            "qc_dimension",
            "relevance",
            "completeness",
            "accuracy",
            "safety",
            "efficiency",
            "experience",
            "overall",
        ),
        default="overall",
        nullable=False,
    )
    score: Mapped[int] = mapped_column(UnsignedTinyInt, default=0, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(64))
    rationale: Mapped[str | None] = mapped_column(String(512))
    auto: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    sub_task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    model_id: Mapped[str | None] = mapped_column(String(48))
    overall: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    safety_risk: Mapped[str] = mapped_column(String(16), default="low", nullable=False)
    partial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FlowCheck(Base, CreatedAtMixin):
    __tablename__ = "flow_checks"
    __table_args__ = (
        Index("ix_flow_checks_user_created", "user_id", "created_at"),
        Index("ix_flow_checks_message", "message_id"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    message_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    turn_no: Mapped[int | None] = mapped_column(Integer)
    check_source: Mapped[str] = mapped_column(
        enum_type("flow_check_source", "log_review", "stage_audit", "trace"), nullable=False
    )
    stage: Mapped[str | None] = mapped_column(String(32))
    issues: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    state_excerpt: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    log_ref: Mapped[str | None] = mapped_column(String(255))
    passed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class OutputGuardLog(Base):
    __tablename__ = "output_guard_log"
    __table_args__ = (
        Index("ix_output_guard_user_category_occurred", "user_id", "category", "occurred_at"),
        Index("ix_output_guard_decision_occurred", "decision", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    message_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    input_excerpt: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(
        enum_type("output_guard_category", "toxic", "compliance", "unsafe"), nullable=False
    )
    decision: Mapped[str] = mapped_column(
        enum_type("output_guard_decision", "allow", "rewrite", "reject"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(512))
    model_used: Mapped[str] = mapped_column(String(64), default="intent_lite", nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class Degradation(Base, CreatedAtMixin):
    __tablename__ = "degradations"
    __table_args__ = (
        Index("ix_degradations_user_created", "user_id", "created_at"),
        Index("ix_degradations_feature_created", "feature", "created_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    project_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    conversation_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    message_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    feature: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(
        enum_type("degradation_tier", "T0", "T1", "T2", "mock", "static"), nullable=False
    )
    intent_l1: Mapped[str | None] = mapped_column(String(32))
    limitation: Mapped[str | None] = mapped_column(String(512))
    upgrade_hint: Mapped[str | None] = mapped_column(String(512))
    accepted: Mapped[bool | None] = mapped_column(Boolean)
    via_event: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class IntentDecision(Base):
    __tablename__ = "intent_decisions"
    __table_args__ = (
        Index("ix_intent_decisions_user_occurred", "user_id", "occurred_at"),
        Index("ix_intent_decisions_chosen_occurred", "chosen_intent", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    message_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    l1_hits: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    l2_hits: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    l3_hits: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    chosen_intent: Mapped[str] = mapped_column(String(32), nullable=False)
    chosen_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    gate_stage: Mapped[str] = mapped_column(
        enum_type("intent_gate_stage", "L1", "L2", "L3", "L4"), nullable=False
    )
    was_soft_confirm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hitl_corrected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    corrected_to: Mapped[str | None] = mapped_column(String(32))
    is_multi: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dag_size: Mapped[int | None] = mapped_column(UnsignedTinyInt)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class ModelCall(Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_user_occurred", "user_id", "occurred_at"),
        Index("ix_model_calls_model_occurred", "model", "occurred_at"),
        UniqueConstraint("idempotency_key", name="uq_model_calls_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str | None] = mapped_column(String(16))
    stage: Mapped[str | None] = mapped_column(String(16))
    purpose: Mapped[str | None] = mapped_column(String(32))
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class KbChangeLog(Base, CreatedAtMixin):
    __tablename__ = "kb_change_log"
    __table_args__ = (
        Index("ix_kb_change_log_collection_created", "collection", "created_at"),
        Index("ix_kb_change_log_actor_created", "actor_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    collection: Mapped[str] = mapped_column(String(64), nullable=False)
    doc_id: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(
        enum_type("kb_change_action", "create", "update", "delete", "rollback"),
        nullable=False,
    )
    actor_user_id: Mapped[int] = mapped_column(UnsignedBigInt, nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(255))


class TraceEvent(Base, CreatedAtMixin):
    """一次 Turn 的结构化链路事件序列(按 seq 追加)。

    由 ``app.core.audit.DbAuditSink`` 在 Turn 收尾时批量写入：turn_start(用户输入)
    → S0..S9 各阶段的 IN/OUT/changed 快照 → turn_end(最终回复)。管理后台「回放」
    据此还原完整处理链路,不再依赖去 app.log 翻 [pipeline.io] 文本行。
    """

    __tablename__ = "trace_events"
    __table_args__ = (Index("ix_trace_events_trace_seq", "trace_id", "seq"),)

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # stage / turn_start / turn_end / turn_error
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    # JSON 文本; LONGTEXT 以容纳阶段 IO 快照(写入侧仍按 48KB 上限逐级截断)。
    payload: Mapped[str | None] = mapped_column(LongText)


class Feedback(Base, CreatedAtMixin):
    """用户对一次生成的评价(1—10 分 + 评语 + 多维细分),供统计与回归数据集。"""

    __tablename__ = "feedbacks"
    __table_args__ = (
        UniqueConstraint("trace_id", name="uq_feedbacks_trace_id"),
        Index("ix_feedbacks_user_created", "user_id", "created_at"),
        CheckConstraint("rating >= 1 AND rating <= 10", name="rating_range"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(UnsignedBigInt, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(UnsignedBigInt, index=True)
    message_id: Mapped[int | None] = mapped_column(UnsignedBigInt)
    rating: Mapped[int] = mapped_column(UnsignedTinyInt, default=0, nullable=False)
    comment: Mapped[str | None] = mapped_column(LongText)
    # 六维细分评分 {relevance: 8, accuracy: 9, ...}
    dimensions: Mapped[dict[str, Any] | None] = mapped_column(JSON)
