"""TurnContext：一个已接受 Turn 在 S0-S9 中的唯一状态容器。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import (
    SCHEMA_VERSION,
    ArchiveResult,
    BoundedPlan,
    ControlEvent,
    ExecutionBudget,
    ExecutionResult,
    GuardResult,
    IntentBundle,
    MemoryDecision,
    RecallResult,
    ResponseFragment,
    SessionInfo,
    SirState,
    TrustFlags,
    UnderstandingResult,
    UserIdentity,
    ValidationResult,
)


@dataclass(slots=True)
class TurnContext:
    """各字段只能由最终规范指定的唯一 Stage 写入。

    原始输入不属于此对象。S0 负责在请求局部作用域内将其脱敏为 clean_message，随后
    释放原始引用，防止其进入后续 Prompt、日志、缓存、审计或 SSE。
    """

    schema_version: str
    trace_id: str
    stream_id: str
    turn_id: str
    client_msg_id: str
    run_epoch: int
    fencing_token: str
    user: UserIdentity
    session: SessionInfo
    clean_message: str
    trust: TrustFlags = field(default_factory=TrustFlags)
    control_event: ControlEvent | None = None
    sir_base: SirState = field(default_factory=SirState)
    sir_base_snapshot_id: int | None = None
    recall: RecallResult = field(default_factory=RecallResult)
    understanding: UnderstandingResult | None = None
    sir_after_dst: SirState = field(default_factory=SirState)
    sir_after_dst_snapshot_id: int | None = None
    sir_diff: dict[str, object] = field(default_factory=dict)
    intent_bundle: IntentBundle | None = None
    plan: BoundedPlan | None = None
    validation: ValidationResult | None = None
    execution: ExecutionResult | None = None
    sir_final: SirState = field(default_factory=SirState)
    memory_decision: MemoryDecision | None = None
    response_fragments: list[ResponseFragment] = field(default_factory=list)
    guard_result: GuardResult | None = None
    reply_draft: str = ""
    reply_final: str = ""
    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    archive_result: ArchiveResult | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"TurnContext schema_version 必须为 {SCHEMA_VERSION}")
        if not self.turn_id or not self.stream_id or not self.trace_id:
            raise ValueError("TurnContext 必须包含 turn_id、stream_id 与 trace_id")
        if not self.client_msg_id:
            raise ValueError("TurnContext 必须包含 client_msg_id")
        if self.run_epoch < 0:
            raise ValueError("TurnContext.run_epoch 不得为负数")
        if not self.fencing_token:
            raise ValueError("TurnContext 必须包含 fencing_token")
        if not self.clean_message:
            raise ValueError("TurnContext 只能保存 S0 产生的非空 clean_message")

    def increment_epoch(self, fencing_token: str) -> None:
        """仅由暂停/恢复/纠正/replan 控制流在持久 CAS 成功后调用。"""
        if not fencing_token:
            raise ValueError("新的 fencing_token 不能为空")
        if fencing_token == self.fencing_token:
            raise ValueError("run_epoch 递增必须使用新的 fencing_token")
        self.run_epoch += 1
        self.fencing_token = fencing_token
