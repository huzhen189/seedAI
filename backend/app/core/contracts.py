"""M2 的跨模块契约：十阶段、Turn 与领域结果对象。

这里的对象只描述边界与不变量；具体业务语义在后续 M3-M8 的 Stage、Domain
Service 与 Repository 中实现。所有对象均携带 schema_version，解析失败必须显式失败。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"
MAX_ACTION_ITEMS: Final[Literal[3]] = 3
MAX_INTERNAL_TASKS: Final[int] = 20


class ContractModel(BaseModel):
    """严格的 wire/跨模块对象基类。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = SCHEMA_VERSION


class StageId(str, Enum):
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"
    S7 = "S7"
    S8 = "S8"
    S9 = "S9"


PIPELINE_STAGES: tuple[StageId, ...] = tuple(StageId)


class StageStatus(str, Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    NO_OP = "no_op"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"


class Domain(str, Enum):
    CHAT = "chat"
    SITE = "site"
    RESEARCH = "research"
    PROJECT = "project"


class SpeechAct(str, Enum):
    ASK = "ask"
    DISCUSS = "discuss"
    CREATE = "create"
    EDIT = "edit"
    REVIEW = "review"
    CONFIRM_PENDING_ACTION = "confirm_pending_action"
    CANCEL = "cancel"
    PUBLISH = "publish"
    TRASH = "trash"
    RESTORE = "restore"
    PURGE = "purge"


class TargetType(str, Enum):
    NONE = "none"
    PROJECT = "project"
    CONVERSATION = "conversation"
    PAGE = "page"
    COMPONENT = "component"
    ARTIFACT = "artifact"
    DEPLOYMENT = "deployment"


class RiskLevel(str, Enum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"
    CRITICAL = "critical"


class TurnStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_CLARIFICATION = "waiting_clarification"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    RECOVERY_PENDING = "recovery_pending"
    NEEDS_MANUAL = "needs_manual"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class ApprovalStatus(str, Enum):
    PENDING_FIRST = "pending_first"
    FIRST_CONFIRMED = "first_confirmed"
    PENDING_SECOND = "pending_second"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    INVALIDATED = "invalidated"


class ArtifactStatus(str, Enum):
    BUILDING = "building"
    VERIFIED = "verified"
    PREVIEW_READY = "preview_ready"
    FAILED = "failed"
    DELETED = "deleted"


class DeploymentStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    HEALTH_CHECKING = "health_checking"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ErrorEnvelope(ContractModel):
    code: str = Field(min_length=1, max_length=96)
    category: str = Field(min_length=1, max_length=64)
    what: str = Field(min_length=1, max_length=512)
    why: str | None = Field(default=None, max_length=512)
    next: str | None = Field(default=None, max_length=512)
    retryable: bool = False
    retry_scope: Literal["none", "stage", "task", "turn"] = "none"
    trace_id: str | None = Field(default=None, max_length=64)
    safe_details: dict[str, Any] = Field(default_factory=dict)


class UserIdentity(ContractModel):
    user_id: int = Field(gt=0)
    tier: Literal["free", "pro", "max"] = "free"
    roles: tuple[str, ...] = ()


class SessionInfo(ContractModel):
    conversation_id: int = Field(gt=0)
    project_id: int | None = Field(default=None, gt=0)
    locale: str = Field(default="zh-CN", min_length=2, max_length=16)


class TrustFlags(ContractModel):
    injection_suspected: bool = False
    pii_redacted: bool = False
    truncated: bool = False


class ControlEvent(ContractModel):
    kind: Literal["stop", "pause", "resume", "supplement", "correct", "discard"]
    payload: dict[str, Any] = Field(default_factory=dict)


class TargetRef(ContractModel):
    type: TargetType = TargetType.NONE
    id: str | None = Field(default=None, max_length=128)
    path: str | None = Field(default=None, max_length=512)


class UtteranceFrame(ContractModel):
    domain_hint: Domain | None = None
    speech_act: SpeechAct | None = None
    target: TargetRef = Field(default_factory=TargetRef)
    executable: bool = False
    social_prefix: str = Field(default="", max_length=512)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class SirState(ContractModel):
    """跨轮持久状态；Turn-local 意图字段禁止写入这里。"""

    slots: dict[str, Any] = Field(default_factory=dict)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    pending: list[dict[str, Any]] = Field(default_factory=list)
    memory_hints: list[dict[str, Any]] = Field(default_factory=list)


class SirDelta(SirState):
    pass


class RecallResult(ContractModel):
    status: Literal["empty", "hit", "degraded", "skipped"] = "empty"
    references: list[str] = Field(default_factory=list)
    degradation_reason: str | None = Field(default=None, max_length=256)


class IntentCandidate(ContractModel):
    intent_id: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)


class UnderstandingResult(ContractModel):
    utterance_frame: UtteranceFrame = Field(default_factory=UtteranceFrame)
    sir_delta: SirDelta = Field(default_factory=SirDelta)
    intent_candidates: list[IntentCandidate] = Field(default_factory=list)
    top2_margin: float | None = Field(default=None, ge=0.0, le=1.0)
    needs_clarification: bool = False
    model_call_id: str | None = Field(default=None, max_length=128)
    degradation_reason: str | None = Field(default=None, max_length=256)


class IntentItem(ContractModel):
    id: str = Field(min_length=1, max_length=64)
    domain: Domain
    speech_act: SpeechAct
    intent_id: str = Field(min_length=1, max_length=128)
    target: TargetRef = Field(default_factory=TargetRef)
    arguments: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    executable: bool
    risk_hint: RiskLevel = RiskLevel.LOW
    depends_on: list[str] = Field(default_factory=list)


class IntentBundle(ContractModel):
    primary_id: str | None = Field(default=None, max_length=64)
    social_prefix: str = Field(default="", max_length=512)
    items: list[IntentItem] = Field(default_factory=list, max_length=MAX_ACTION_ITEMS)
    needs_clarification: bool = False

    @model_validator(mode="after")
    def validate_primary_and_items(self) -> "IntentBundle":
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("IntentBundle.items 的 id 必须唯一")
        if self.primary_id is not None and self.primary_id not in ids:
            raise ValueError("IntentBundle.primary_id 必须引用 items 中的意图")
        return self


class ActionItem(ContractModel):
    id: str = Field(min_length=1, max_length=64)
    intent_id: str = Field(min_length=1, max_length=128)
    domain: Domain
    speech_act: SpeechAct
    target: TargetRef = Field(default_factory=TargetRef)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    dependency_kind: Literal["hard", "soft"] = "hard"


class BoundedPlan(ContractModel):
    mode: Literal["bounded"] = "bounded"
    action_items: list[ActionItem] = Field(default_factory=list, max_length=MAX_ACTION_ITEMS)
    max_items: Literal[3] = MAX_ACTION_ITEMS
    serial: Literal[True] = True

    @model_validator(mode="after")
    def validate_item_ids(self) -> "BoundedPlan":
        ids = [item.id for item in self.action_items]
        if len(ids) != len(set(ids)):
            raise ValueError("BoundedPlan.action_items 的 id 必须唯一")
        unknown_dependencies = {
            dependency
            for item in self.action_items
            for dependency in item.depends_on
            if dependency not in ids
        }
        if unknown_dependencies:
            raise ValueError(f"BoundedPlan 包含未知依赖: {sorted(unknown_dependencies)}")
        return self


class ArtifactRecord(ContractModel):
    """不可变网站内容的跨服务描述；M7 才会映射到升级后的 artifacts 表。"""

    artifact_id: str = Field(min_length=1, max_length=64)
    project_id: int = Field(gt=0)
    conversation_id: int | None = Field(default=None, gt=0)
    parent_artifact_id: str | None = Field(default=None, max_length=64)
    version: int = Field(ge=1)
    manifest_digest: str = Field(min_length=64, max_length=64)
    status: ArtifactStatus


class DeploymentRecord(ContractModel):
    deployment_id: str = Field(min_length=1, max_length=64)
    project_id: int = Field(gt=0)
    artifact_id: str = Field(min_length=1, max_length=64)
    manifest_digest: str = Field(min_length=64, max_length=64)
    environment: str = Field(min_length=1, max_length=32)
    status: DeploymentStatus


class ApprovalBinding(ContractModel):
    approval_id: str = Field(min_length=26, max_length=26)
    action: str = Field(min_length=1, max_length=128)
    target: TargetRef
    artifact_id: str | None = Field(default=None, max_length=64)
    manifest_digest: str | None = Field(default=None, min_length=64, max_length=64)
    args_hash: str = Field(min_length=64, max_length=64)
    expires_at: datetime
    fencing_token: str = Field(min_length=1, max_length=64)


class OutboxRecord(ContractModel):
    event_key: str = Field(min_length=1, max_length=160)
    aggregate_type: str = Field(min_length=1, max_length=64)
    aggregate_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class ResponseFragment(ContractModel):
    status: Literal["info", "clarify", "approval", "blocked", "success", "partial", "error"]
    text: str = Field(default="", max_length=16_000)
    reason_codes: list[str] = Field(default_factory=list)
    producer_stage: StageId
    input_version: str = SCHEMA_VERSION
    output_refs: list[str] = Field(default_factory=list)
    retryable: bool = False
    error: ErrorEnvelope | None = None


class ValidationResult(ContractModel):
    status: Literal["pass", "clarify", "needs_approval", "block"]
    response_fragments: list[ResponseFragment] = Field(default_factory=list)
    pending_action_id: str | None = Field(default=None, max_length=64)
    approval_id: str | None = Field(default=None, max_length=26)
    reason_codes: list[str] = Field(default_factory=list)


class TaskResult(ContractModel):
    task_id: str = Field(min_length=1, max_length=64)
    status: Literal["succeeded", "partial", "failed", "cancelled", "paused"]
    output_refs: list[str] = Field(default_factory=list)
    error: ErrorEnvelope | None = None


class ExecutionResult(ContractModel):
    status: Literal["succeeded", "partial", "failed", "cancelled", "paused"]
    committed: bool = False
    task_results: list[TaskResult] = Field(default_factory=list, max_length=MAX_INTERNAL_TASKS)
    tool_result_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    deployment_refs: list[str] = Field(default_factory=list)
    operation_keys: list[str] = Field(default_factory=list)
    usage_refs: list[str] = Field(default_factory=list)
    error: ErrorEnvelope | None = None


class MemoryDecision(ContractModel):
    status: Literal["stored", "skipped", "degraded", "failed"] = "skipped"
    reason_codes: list[str] = Field(default_factory=list)
    producer_stage: Literal[StageId.S7] = StageId.S7
    input_version: str = SCHEMA_VERSION
    output_refs: list[str] = Field(default_factory=list)
    retryable: bool = False
    error: ErrorEnvelope | None = None


class GuardResult(ContractModel):
    status: Literal["passed", "rewritten", "rejected", "fallback"] = "passed"
    reason_codes: list[str] = Field(default_factory=list)
    producer_stage: Literal[StageId.S8] = StageId.S8
    input_version: str = SCHEMA_VERSION
    output_refs: list[str] = Field(default_factory=list)
    retryable: bool = False
    error: ErrorEnvelope | None = None


class ArchiveResult(ContractModel):
    status: Literal["attempt_archived", "finalized", "failed"]
    reason_codes: list[str] = Field(default_factory=list)
    producer_stage: Literal[StageId.S9] = StageId.S9
    input_version: str = SCHEMA_VERSION
    output_refs: list[str] = Field(default_factory=list)
    retryable: bool = False
    error: ErrorEnvelope | None = None


class ExecutionBudget(ContractModel):
    max_model_calls: int = Field(default=0, ge=0)
    reserved_model_calls: int = Field(default=0, ge=0)
    settled_model_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_budget(self) -> "ExecutionBudget":
        if self.reserved_model_calls > self.max_model_calls:
            raise ValueError("reserved_model_calls 不得超过 max_model_calls")
        if self.settled_model_calls > self.reserved_model_calls:
            raise ValueError("settled_model_calls 不得超过 reserved_model_calls")
        return self


class StageResult(ContractModel):
    stage: StageId
    status: StageStatus
    reason_code: str = Field(min_length=1, max_length=128)
    input_schema_version: str = SCHEMA_VERSION
    output_schema_version: str = SCHEMA_VERSION
    entered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    left_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int = Field(default=0, ge=0)
    output_refs: list[str] = Field(default_factory=list)
    error: ErrorEnvelope | None = None

    @model_validator(mode="after")
    def validate_timing_and_failure(self) -> "StageResult":
        if self.left_at < self.entered_at:
            raise ValueError("StageResult.left_at 不得早于 entered_at")
        if self.status is StageStatus.FAILED and self.error is None:
            raise ValueError("failed StageResult 必须包含 ErrorEnvelope")
        return self


class StreamEvent(ContractModel):
    stream_id: str = Field(min_length=26, max_length=26)
    turn_id: str = Field(min_length=26, max_length=26)
    trace_id: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=64)
    seq: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: Literal[
        "stage",
        "task",
        "tool",
        "token",
        "state_diff",
        "approval",
        "attempt_output",
        "suspended",
        "usage",
        "capability_notice",
        "error",
        "reconnect",
        "done",
    ]
    data: dict[str, Any] = Field(default_factory=dict)
