"""TurnContext：一个已接受 Turn 在 S0-S9 中的唯一状态容器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .continuation import Continuation
from .contracts import (
    SCHEMA_VERSION,
    ArchiveResult,
    BoundedPlan,
    ControlEvent,
    Domain,
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
    # 用户在前端模型选择器中指定的模型（经 /api/chat 透传）；None 表示用后端默认链
    # （qwen→deepseek）。聊天回复与建站代码生成均遵循此值（"全跟 selector 走"），
    # 无效 / 未配置时回落默认链。
    model: str | None = None
    # 当前轮使用的数据库会话（由 services/turns.py 构造 context 时注入）。
    # 供 S6 在执行期复用同一事务会话做只读查询（如取最近对话拼短期记忆），
    # 避免额外开连接；为 None 时下游改用只读事务兜底（fail-soft）。
    db_session: Any | None = None
    trust: TrustFlags = field(default_factory=TrustFlags)
    control_event: ControlEvent | None = None
    sir_base: SirState = field(default_factory=SirState)
    sir_base_snapshot_id: int | None = None
    recall: RecallResult = field(default_factory=RecallResult)
    # 向量库检索到的项目/会话上下文（S1 召回时填充，供 S6/S7 参考与连续性）。
    project_context: list[str] = field(default_factory=list)
    # 向量库按 user_id 过滤召回的「用户级偏好/属性」（S1 填充，供 prompt 个性化填充，
    # 例如用户曾表达的品牌色/主题/风格，避免重复追问）。与 project_context 不同，本字段
    # 真正进入 chat respond 的 system prompt（project_context 目前尚未被消费）。
    user_context: list[str] = field(default_factory=list)
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
    # 社交寒暄前缀（S2 从消息剥离收集，S8 前置到最终回复一次，避免复合句寒暄被裁剪）。
    social_prefix: str = ""
    # 回溯控制（correct/supplement）的上一轮 turn_id：非空表示本轮是对指定 turn 的回溯重写/补充。
    prior_turn_id: str | None = None
    # S1 产出的「结构化前情窗口」（最近优先，最多 5 条）：[{turn_id, role, summary, content}]。
    # 供 T2 承接解析与 S5 上下文澄清消费；只存结构化摘要，不塞原始 transcript，防污染。
    context_gist: list[dict] = field(default_factory=list)
    # S2 解析出的跨轮承接边（一等数据结构）：independent | references。fail-soft：解析异常时为 None。
    # 承接只折进 target_slots（默认 ["site.brief"]），绝不回灌意图分类，blast radius 小。
    continuation: Continuation | None = None
    # 以下三项由 S1 依据「上一轮的事实产物」回填（不是文本猜测），供 S2/S4 域继承与 S6 产物锁定。
    prior_domain: Domain | None = None
    prior_artifact_id: int | None = None
    prior_project_id: int | None = None
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

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """运行中直发一条 SSE 帧。

        供 stage 在 LLM 流式产出时实时推送 ``token`` / ``think`` 事件(如 S6 聊天回复)。
        复用 transport.stream_broker 的全局 broker——与 turns.py 的 ``_publish`` 同一出口，
        因此断线续传(replay)也能完整还原这些中间帧。
        """
        from app.transport.stream_broker import broker

        await broker.publish(
            stream_id=self.stream_id,
            turn_id=self.turn_id,
            trace_id=self.trace_id,
            type=event_type,
            data=data,
        )

    def snapshot_state(self) -> dict[str, object]:
        """抽取 S0-S9 各节点关心的 IO 状态，供 pipeline 在节点进入前/完成后做边界快照。

        返回的是**原始字段引用**（未序列化）。真正的安全序列化/截断由
        ``app.core.pipeline._log_safe`` 负责——调用方必须在 stage.run 前后分别调用本方法，
        否则原地修改 Pydantic 对象会导致前后两份快照串味。
        """
        return {
            "clean_message": self.clean_message,
            "recall": self.recall,
            "project_context": self.project_context,
            "user_context": self.user_context,
            "understanding": self.understanding,
            "sir_base": self.sir_base,
            "sir_base_snapshot_id": self.sir_base_snapshot_id,
            "sir_after_dst_snapshot_id": self.sir_after_dst_snapshot_id,
            "sir_diff": self.sir_diff,
            "prior_domain": self.prior_domain,
            "prior_artifact_id": self.prior_artifact_id,
            "prior_project_id": self.prior_project_id,
            "intent_bundle": self.intent_bundle,
            "slot_stack": self.understanding.slot_stack if self.understanding else None,
            "plan": self.plan,
            "validation": self.validation,
            "execution": self.execution,
            "budget": self.budget,
            "memory_decision": self.memory_decision,
            "response_fragments": self.response_fragments,
            "guard_result": self.guard_result,
            "reply_draft": self.reply_draft,
            "reply_final": self.reply_final,
            "social_prefix": self.social_prefix,
            "sir_after_dst": self.sir_after_dst,
            "sir_final": self.sir_final,
            "archive_result": self.archive_result,
            "continuation": self.continuation,
        }
