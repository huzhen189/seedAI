# SeedAI 全链路重构最终实施规范 v1.1

> 状态：**已确认的唯一实施规范 / Canonical Implementation Specification**
>
> 版本：1.1（完成独立闭环审查后的修订版）
>
> 日期：2026-08-01
>
> 生效状态：用户已于 2026-08-01 确认；M0 规范冻结执行中，M1 仅可在 M0 退出条件全部满足后启动
>
> 适用范围：SeedAI“通过对话创建、修改、预览、发布静态网站，兼顾普通闲聊”的前后端全链路
>
> 继承关系：以《全链路重构规划方案 v2》为骨架，合并 step1/step2/step3 的冻结决策，并吸收 C+ 领域执行策略
>
> 替代关系：本规范生效后，v2、step1、step2、step3 与《Agent全链路执行总图·完整详版》仅作为历史依据；发生冲突时以本规范为准

---

## 0. 文档控制与规范词义

### 0.1 裁决优先级

冲突按以下顺序裁决：

1. 本规范明确条款；
2. 用户已明确拍板的项目决策；
3. C+ 合并裁决；
4. step1/step2/step3 各自负责领域的冻结终态；
5. 《全链路重构规划方案 v2》；
6. 旧代码与旧文档。

同级冲突依次采用：**更严格的安全边界 → 更少的重复真相源 → 更强的可测试性 → 更低的运行复杂度**。仍无法裁决时必须创建 ADR，禁止开发者静默择一。

### 0.2 规范词义

- **MUST / 必须**：违反即不得合并或发布。
- **SHOULD / 应当**：默认执行；偏离必须记录 ADR。
- **MAY / 可以**：可选能力，不构成当前上线阻塞。
- **Turn**：用户一次有效输入及其完整 S0–S9 生命周期。
- **Session/Conversation**：多轮会话容器。
- **Stage**：S0–S9 中的一个审计阶段。
- **ActionItem**：S4 产生的用户级执行项，每Turn最多3个。
- **Task/TaskAttempt**：S6把ActionItem展开后的内部执行步骤及其一次尝试；复杂模式总Task最多20个。

### 0.3 变更流程

任何修改以下冻结项的变更必须同时更新：本规范、Schema、配置、契约测试、迁移/重置脚本与验收矩阵：

- S0–S9 阶段语义；
- 风险等级与审批规则；
- Intent/Skill/Tool 注册表；
- 数据库枚举、FK 与索引；
- SSE 事件包络；
- 模型档位和配额；
- Redis/Chroma/COS 命名与隔离规则。

---

## 1. 产品范围、目标与非目标

### 1.1 产品主目标

SeedAI 的核心业务是：

1. 用户通过自然语言创建静态网站；
2. 用户持续讨论、修改、审查并生成新版本；
3. 用户获得可访问预览；
4. 用户明确确认后发布指定版本；
5. 用户可回收、恢复或永久删除项目；
6. 同一会话内可以自然闲聊、讨论设计或询问技术问题。

### 1.2 关键需求（EARS）

- **REQ-FLOW-001**：当收到有效用户消息时，SeedAI 必须为该 Turn 执行可审计的 S0–S9 生命周期；允许阶段 `skip/no-op`，但不得绕过安全、归档与错误收口。
- **REQ-CHAT-001**：当消息属于普通闲聊或网站讨论且未形成执行授权时，系统必须进入 ChatService，不得修改项目文件、版本或发布状态。
- **REQ-SITE-001**：当消息明确授权创建或修改网站时，系统必须通过 SiteWorkflow 产生不可变版本，并在交付前完成确定性审计。
- **REQ-MULTI-001**：当一条消息包含多个有效意图时，系统必须输出唯一主意图和最多三个有效执行项；寒暄不得单独创建 Task。
- **REQ-RISK-001**：当动作属于 HIGH 或 CRITICAL 风险时，系统必须使用服务端生成且绑定目标版本的审批凭据，不得相信模型文本中的“用户已确认”。
- **REQ-RECOVER-001**：当连接中断、用户暂停或进程退出时，系统必须从最近安全 checkpoint 恢复，且不得重复执行已完成的 Tool 或重复计费。
- **REQ-DEPLOY-001**：当用户发布网站时，系统必须部署指定不可变版本，执行发布后健康检查，并在失败时恢复上一个 active 版本。
- **REQ-DATA-001**：当项目永久删除时，系统必须删除内容线数据和项目物理资源，但保留无内容 FK 的统计治理数据。
- **REQ-OBS-001**：当任一 Stage、Tool 或模型调用结束时，系统必须以 W0 真相或事务 Outbox 记录 trace、耗时、状态、错误类型和用量，不得记录密钥、Prompt 正文或明文敏感信息。
- **REQ-APPROVAL-001**：当 HIGH/CRITICAL 动作等待确认时，系统必须把审批绑定到 action、target、artifact manifest、args hash、有效期和 fencing token，并以 MySQL CAS 单次消费；自然语言“可以/继续”不得直接消费审批。
- **REQ-FINALIZE-001**：当系统向客户端发送最终正文和 `done(success)` 时，W0 终态事务必须已提交；未经过 S8 的 Provider 原始 token 不得发送给客户端。
- **REQ-PREVIEW-001**：当用户打开生成站点预览时，预览必须位于不携带平台凭证的独立 Origin，并受 iframe sandbox、平台 CSP 和短期签名保护。
- **REQ-PII-001**：当输入含 PII 时，S0 必须在进入模型、检索、Checkpoint、审计或 SSE 前生成脱敏副本；原始消息只允许在请求内存的最短作用域存在。

### 1.3 非目标

当前版本明确不做：

- 任意第三方 Skill/MCP 市场；
- 角色型 Multi-Agent（product/design/dev/qa）接力；
- 默认无限制 DAG 或并行写同一项目；
- 在生成网站前端保存真实密钥或真实敏感个人数据；
- 为生成站点托管任意后端代码、数据库或支付服务；
- 让模型自主执行发布、永久删除或跨项目操作；
- 为兼容旧 `queue.py/cascade.py/roles/agent_*` 保留双链路。

---

## 2. 冻结架构决策

### 2.1 总体裁决

1. **S0–S9 十阶段保持不变**，它是唯一生命周期骨架，不新增 S10。
2. **C+ 不是第二套 Pipeline**：IntentUnderstanding 落 S2/S4，BoundedPlan 落 S4/S5，四领域执行落 S6，ResponseComposer 落 S8。
3. **单进程 FastAPI** 作为当前部署单元；传输与 Pipeline 解耦，但不为了“微服务形式”增加网络二跳。
4. **MySQL 是业务真相源**；Redis 只保存热态、流、锁和配额；Chroma 只保存向量检索数据；本地/COS 保存网站文件。
5. **LLM 负责理解与创作，代码负责合并、校验、状态、调度、风险、版本、发布和回滚**。
6. **默认串行执行**；复杂模式可生成 DAG，但受严格上限约束。
7. **旧 Role 包删除**；角色能力分别进入领域服务、SiteSpec、Planner、Verify 与全局 Stats。
8. Python 生产、开发、CI、Docker 统一使用 **Python 3.13**；禁止用旧运行时掩盖语法或类型问题。

### 2.2 单一真相源

| 领域 | 唯一真相源 |
|---|---|
| 环境与密钥 | 环境变量 + `app/config/settings.py` |
| 模型档位 | `app/config/models.yaml` |
| 路由阈值 | `app/config/router.yaml` |
| 配额 | `app/config/quota.yaml` |
| Intent/Skill 映射 | `app/router/intent_catalog.json` |
| Tool 风险与契约 | `app/tools/_registry.py` |
| ORM/Schema | `app/models/*` + `Base.metadata` |
| 业务数据 | MySQL |
| Turn 热态与事件流 | Redis |
| 向量检索 | Chroma 注册表 +物理 Collection |
| 网站版本 | MySQL `artifacts` 元数据 + 文件 manifest/checksum |

不得在业务模块中复制阈值、Skill 映射、风险等级或模型名。

---

## 3. 系统上下文与信任边界

```text
Vue 前端
  │ POST /api/chat / GET stream / gate / project APIs
  ▼
Ingress + Auth
  ▼
StreamBroker ───────────── Redis event stream / cancel / approval / quota
  │
  ▼
Pipeline(S0–S9) ────────── TurnContext（本 Turn 唯一状态容器）
  │
  ├─ Router / IntentCatalog / SIR-DST
  ├─ ChatService
  ├─ SiteWorkflow
  ├─ ResearchService
  └─ ProjectOps
       │
       ├─ ToolRegistry → 受限 Tools
       ├─ Model Harness → 平台凭证 / BYOK(S6 only)
       ├─ Repository / Services → MySQL
       ├─ Memory Gate → Chroma
       └─ RevisionService → 本地预览 / COS生产版本
```

信任规则：

- `user_id/role/project_id/conversation_id` 的最终值必须由 JWT 和服务端归属查询确定；不得信任请求体自报。
- 模型输出、Tool 输出、网页内容、检索结果均视为不可信输入，必须通过对应 Schema/Guard。
- 前端审批只提交 `approval_id + decision`；目标、参数、revision、风险由服务端审批记录确定。

---

## 4. 目标代码结构与依赖规则

```text
backend/app/
├── main.py
├── config/
│   ├── models.yaml
│   ├── router.yaml
│   ├── quota.yaml
│   └── settings.py
├── transport/
│   ├── chat_api.py
│   ├── stream_api.py
│   ├── gate_api.py
│   └── stream_broker.py
├── core/
│   ├── contracts.py
│   ├── turn_context.py
│   ├── pipeline.py
│   ├── errors.py
│   └── stages/
│       ├── s0_gateway.py
│       ├── s1_recall.py
│       ├── s2_understand.py
│       ├── s3_dst.py
│       ├── s4_classify.py
│       ├── s5_validate.py
│       ├── s6_execute.py
│       ├── s7_persist_state.py
│       ├── s8_output_guard.py
│       └── s9_archive.py
├── router/
│   ├── l1_rules.py
│   ├── l2_recall.py
│   ├── l3_llm.py
│   ├── l4_gate.py
│   ├── splitter.py
│   ├── intent_catalog.json
│   └── schema.py
├── domains/
│   ├── chat/service.py
│   ├── site/{service,spec,workflow,verify,revision}.py
│   ├── research/service.py
│   └── project/service.py
├── skills/                 # 8个业务语义入口
├── tools/                  # 原子能力与唯一注册表
├── agent/
│   ├── planner.py
│   └── react_loop.py
├── memory/
│   ├── sir.py
│   ├── recall_gate.py
│   └── storage_gate.py
├── hitl/
│   ├── approvals.py
│   └── interventions.py
├── models/
├── db/repositories/
├── services/               # 跨表事务、配额、清理、部署
├── stats/
├── observability/
├── auth/
└── security/
```

依赖铁律：

- Stage 只能通过契约访问下一层，不得互相反向 import。
- S0/S3/S5/S9 禁止 import 模型 Provider；S8 的确定性规则不得依赖 Provider。
- Repository 只操作单表；跨表事务进入 Service。
- Tool 不得调用 Skill；Skill 只能调用白名单 Tool。
- 业务代码不得裸调模型 SDK、Redis、Chroma 或 COS；必须走 Harness/Adapter。
- 单个业务文件 SHOULD 小于 800 行，Pipeline SHOULD 小于 300 行。

---

## 5. 核心数据契约

所有跨模块对象必须带 `schema_version`；解析失败必须进入结构化错误，禁止“尽量猜”。

### 5.1 StageResult 与执行总则

每个 Stage 必须返回 `StageResult`，状态只能为：

```text
completed | skipped | no_op | paused | blocked | failed
```

- `skipped`：前置条件不满足，必须返回契约默认输出。
- `no_op`：Stage 已执行检查但状态未变化，必须返回 carry-forward 输出。
- 已接受的 Turn 中 S0、S8、S9 不得 skip。
- 所有状态必须记录 enter/leave、reason_code、耗时和输入/输出 Schema 版本。
- 任何 project/artifact/deployment/approval/usage 成功声明都必须以 W0 已提交为前提。
- 每次 pause/resume/correct/replan 必须递增 `run_epoch` 并获取新的 `fencing_token`；旧 token 的迟到结果只能进入审计，不得修改当前业务状态。

### 5.2 TurnContext 与字段所有权

```python
@dataclass
class TurnContext:
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
    trust: TrustFlags
    control_event: ControlEvent | None
    sir_base: SIR
    sir_base_snapshot_id: int | None
    recall: RecallResult
    understanding: UnderstandingResult | None
    sir_after_dst: SIR
    sir_after_dst_snapshot_id: int | None
    sir_diff: dict
    intent_bundle: IntentBundle | None
    plan: BoundedPlan | None
    validation: ValidationResult | None
    execution: ExecutionResult | None
    sir_final: SIR
    memory_decision: MemoryDecision | None
    response_fragments: list[ResponseFragment]
    guard_result: GuardResult | None
    reply_draft: str
    reply_final: str
    budget: ExecutionBudget
    archive_result: ArchiveResult | None
```

`raw_message` 只能存在于 S0 请求内存，不进入 TurnContext、Prompt、检索、Checkpoint、审计、日志或 SSE。S0 产生 `clean_message` 后必须释放原始引用。

字段唯一写者：

| 字段 | 写者 |
|---|---|
| 身份、Session、clean_message、trust、control_event、sir_base | S0 |
| recall | S1 |
| understanding | S2 |
| sir_after_dst、sir_diff、snapshot | S3 |
| intent_bundle、plan | S4 |
| validation | S5 |
| execution、response_fragments、artifact/deployment 结果 | S6领域Service |
| sir_final、memory_decision | S7 |
| reply_draft、guard_result、reply_final | S8 |
| archive_result | S9 |

审计由独立 `AuditSink.append(StageResult)` 写入，不允许多个 Stage 修改共享 audit list；预算通过 `BudgetLedger` 原子 reserve/settle，不允许任意改共享数值。

### 5.3 UnderstandingResult、UtteranceFrame 与 SIRDelta

```python
class UnderstandingResult:
    schema_version: str
    utterance_frame: UtteranceFrame
    sir_delta: SIRDelta
    intent_candidates: list[IntentCandidate]
    top2_margin: float | None
    needs_clarification: bool
    model_call_id: str | None
    degradation_reason: str | None
```

`UtteranceFrame` 仅在当前 Turn 有效：

```json
{
  "domain_hint": "site",
  "speech_act": "edit",
  "target": {"type": "component", "id": null, "path": "home.hero"},
  "executable": true,
  "social_prefix": "你好",
  "confidence": 0.94
}
```

`domain_hint/speech_act/target/executable/social_prefix/intent_candidates` **禁止合并进持久 SIR**，防止下一轮重放旧执行意图。SIRDelta 只包含可跨轮携带的槽、约束和 pending：

```json
{
  "schema_version": "1.0",
  "slots": {},
  "constraints": [],
  "pending": [],
  "memory_hints": []
}
```

规则：

- S2 不得修改 SIR、选择 Tool 或执行动作。
- S3 合并优先级：本轮用户显式值 > 已确认锁定 SIR > 记忆建议 > 模型推断。
- 槽置信度 `<0.60` 进入 tentative/pending，不得覆盖稳定值。
- DELETE 必须显式表达；`null` 不得同时表示未知和删除。
- S3 写不可变 provisional 快照，不推进 canonical pointer；S7 在 W0 成功后用 CAS 推进 final canonical pointer。
- S2 全部降级失败时必须返回空 delta 和 `executable=false, needs_clarification=true`，禁止用旧 SIR 执行意图兜底。

### 5.4 IntentBundle 与 BoundedPlan

```json
{
  "schema_version": "1.0",
  "primary_id": "i1",
  "social_prefix": "你好",
  "items": [
    {
      "id": "i1",
      "domain": "site",
      "speech_act": "edit",
      "intent_id": "edit_site",
      "target": {"type": "project", "id": 123, "path": "home.hero"},
      "arguments": {},
      "confidence": 0.94,
      "executable": true,
      "risk_hint": "low",
      "depends_on": []
    }
  ],
  "needs_clarification": false
}
```

- `primary_id` 是唯一主意图真相；不得再存 `is_primary`。
- S5 的 authoritative risk 必须从 IntentCatalog、Skill policy、实际 ToolRegistry action 和 target scope 计算，并取最高等级；模型只能提供 `risk_hint`。
- `domain = chat|site|research|project`
- `speech_act = ask|discuss|create|edit|review|confirm_pending_action|cancel|publish|trash|restore|purge`
- `target.type = none|project|conversation|page|component|artifact|deployment`

BoundedPlan 只保存最多 3 个**用户级 ActionItem**，不保存 SiteWorkflow 内部 `verify/preview` 步骤，也不保存审批步骤：

```json
{
  "schema_version": "1.0",
  "mode": "bounded",
  "action_items": [],
  "max_items": 3,
  "serial": true
}
```

- 寒暄不计入 ActionItem；同项目 edit 合并为一个 SiteSpecDelta。
- 用户显式主动作优先；硬依赖不是主意图；同分且目标冲突时 clarify。
- 超过 3 项不得静默丢弃，必须列出保留项和待处理项并请求一次选择。
- `publish/trash/purge` 只能是显式 primary，禁止作为 secondary 自动执行。
- 依赖必须声明 `hard|soft`；hard dependency 失败时停止或显式降级，soft 失败不连坐主交付。
- S6 可将一个 ActionItem 展开为内部 Task；复杂模式 Task 绝对上限 20，`max_plan_revisions=2`，Site Repair 固定最多 1 次。

### 5.5 ValidationResult、ExecutionResult 与响应对象

```python
class ValidationResult:
    schema_version: str
    status: Literal["pass", "clarify", "needs_approval", "block"]
    response_fragments: list[ResponseFragment]
    pending_action_id: str | None
    approval_id: str | None
    reason_codes: list[str]

class ExecutionResult:
    schema_version: str
    status: Literal["succeeded", "partial", "failed", "cancelled", "paused"]
    committed: bool
    task_results: list[TaskResult]
    tool_result_refs: list[str]
    artifact_refs: list[str]
    deployment_refs: list[str]
    operation_keys: list[str]
    usage_refs: list[str]
    error: ErrorEnvelope | None
```

`ResponseFragment/MemoryDecision/GuardResult/ArchiveResult` 至少必须包含：`schema_version, status, reason_codes, producer_stage, input_version, output_refs, retryable, error`。

### 5.6 ToolResult、ErrorEnvelope 与 SSE

```python
class ToolResult:
    schema_version: str
    status: Literal["succeeded", "failed", "unknown"]
    data: dict
    error: ErrorEnvelope | None
    idempotency_key: str | None
    metrics: dict
```

```json
{
  "schema_version": "1.0",
  "code": "site_verify_failed",
  "category": "validation",
  "what": "首页脚本运行失败",
  "why": "浏览器控制台检测到未定义变量",
  "next": "系统已保留当前稳定版本，可重试定向修复",
  "retryable": true,
  "retry_scope": "task",
  "trace_id": "...",
  "safe_details": {}
}
```

`stream_id` 使用不可猜测 ULID；Redis Stream ID 同时作为 SSE `id` 和 `event_id`，另有严格递增 `seq` 供展示：

```json
{
  "schema_version": "1.0",
  "stream_id": "01K...",
  "turn_id": "01K...",
  "trace_id": "...",
  "event_id": "1785540000000-0",
  "seq": 42,
  "timestamp": "ISO-8601",
  "type": "stage",
  "data": {}
}
```

---

## 6. 十阶段最终契约

十阶段是统一审计生命周期，**允许 skip/no-op**。普通闲聊仍有 S0–S9 审计，但不会为形式而调用记忆、Planner 或多余模型。

| Stage | 唯一职责 | 输入 | 输出 | LLM | 主要副作用 | skip/失败出口 |
|---|---|---|---|---|---|---|
| S0 网关 | 鉴权、归属、脱敏、限流、幂等、控制事件、审计初始化 | HTTP 请求/JWT | 身份、Session、clean message、sir_base、trace/stream/turn | 否 | W0 Turn、幂等、配额预留 | 400/401/403/409/429；控制事件转干预矩阵 |
| S1 召回 | Recall Gate 与按需检索 | clean message、Session、sir_base | RecallResult | 仅模糊门控可用轻量模型 | Chroma/缓存只读 | 无信号 skipped；失败降级空召回 |
| S2 理解 | 提取 Turn-local 语义与持久槽变化 | 消息、sir_base、Recall | UnderstandingResult | 规则可直出；模糊时 intent_lite | 无业务写入 | 结构重试最多2次；失败 executable=false |
| S3 合并 | 纯函数 DST、provisional快照与回滚锚点 | sir_base、sir_delta | sir_after_dst、sir_diff | 否 | provisional snapshot | 冲突回滚；低置信pending；无变化no_op |
| S4 分类 | L1–L4门控、IntentBundle、BoundedPlan、Skill映射 | sir_after_dst、UtteranceFrame | IntentBundle、BoundedPlan | 复用S2结果；歧义可intent_strong一次 | ActionItem草案 | 低置信clarify；切分失败降级单意图 |
| S5 校验 | 必填、格式、归属、权限、风险 | Plan、Catalog、身份、ToolRegistry | ValidationResult | 否 | W0 pending action/approval/pause | 非pass时S6 skipped，但仍进入S8/S9 |
| S6 执行 | 领域分发、W0 Operation、Tool、Artifact/Deployment、网站验证 | 合法Plan、SIR、预算、fencing | ExecutionResult、ResponseFragments | 按领域 | 业务Service是project/artifact/deployment唯一写者 | retry/replan/partial/cancel/pause |
| S7 回写 | 生成执行事实SIR delta、推进canonical SIR、Storage Gate | 已提交ExecutionResult、sir_after_dst | sir_final、MemoryDecision | 模糊存储判定可用轻量模型 | SIR/会话热态/Chroma | 仅长期记忆失败可降级；业务W0失败不得伪报成功 |
| S8 出口 | ResponseComposer、脱敏、合规、格式、一致性 | Validation fragments、Execution fragments、sir_final | reply_final、GuardResult | 默认否；风险回复才调用intent_lite | W1 outbox output_guard | rewrite/reject/template fallback |
| S9 归档 | 提交W0终态+Outbox、归档attempt/finalize、释放资源 | 全量Context | ArchiveResult | 否 | MySQL W0、Outbox、清热态 | W1/W2补偿；W0失败只能终态error |

### 6.1 分支、Turn 状态与终态规则

S5 分支必须闭环：

| ValidationResult | S6 | S7 | S8 | S9 |
|---|---|---|---|---|
| pass | 执行 | 正常回写 | 装配执行回复 | archive + finalize |
| clarify | skipped | no_op | 装配唯一澄清问题 | archive waiting_clarification |
| needs_approval | skipped | no_op | 装配审批卡 | archive waiting_approval |
| block | skipped | no_op | 确定性拒绝模板 | finalize blocked |

Turn 状态机：

```text
accepted → running
running → waiting_clarification | waiting_approval | paused
waiting_* | paused | recovery_pending → running（run_epoch + 1）
recovery_pending → needs_manual
needs_manual → running | failed | cancelled（仅管理员受审计resolution）
running → completed | failed | cancelled | blocked
```

`waiting_*` 和 `paused` 是非终态。S9 分为：

- `archive_attempt(turn_id, run_epoch)`：每个 epoch 幂等一次；
- `finalize_turn(turn_id)`：只允许终态执行一次；最终 assistant message、终态 SSE 与最终资源释放均以 turn_id 唯一。

默认正文安全策略：S6/ChatService 可以流式生成到服务端耐久 partial buffer，但未经 S8 的 Provider 原始 token 不得发给客户端。S8 完成且 W0 final message/usage/turn 提交后，才把 `reply_final` 分块发送 `token`，随后发送唯一 `done`。后续可新增增量 Guard，但必须具有同等确定性安全证明。

### 6.2 S0 细则

- `client_msg_id` 在用户域内唯一：数据库 `UNIQUE(user_id, client_msg_id)`；重复 ID 且请求摘要一致时返回原 stream，不一致返回 409。
- 输入正文默认上限 8,000 字符；超限应明确报错或按配置截断并置 `truncated=true`，不得静默。
- S0 可以识别 stop/cancel/correct/supplement 等控制事件，但不得做业务意图分类。
- S0 必须在任何模型、检索、Checkpoint、日志、审计和 SSE 前产生脱敏 `clean_message`；`raw_message` 不得持久化、不得进入异常正文，并在请求作用域结束前释放。
- Injection 标记只降低信任级别；是否拒绝由确定性安全规则决定。

### 6.3 S1 Recall Gate

触发信号：新会话且存在结构化偏好、明确“按之前/继续/那个”、跨会话引用、复杂任务经验、事实型研究、当前项目代码定位。

- 普通寒暄、明确发布/删除、无需历史的简单 edit 可以 skip。
- 用户偏好优先读取结构化 MySQL/Redis 快照；只有语义检索需求才调用 Chroma。
- 最大 Top-K 5；召回内容必须带 scope、owner、source、score，不得跨用户/项目。

### 6.4 S2/S4 去重与模型调用预算

- S2 负责“这句话表达了什么变化”；S4 负责“系统最终路由为何”。
- 每 Turn 的 `intent_lite` 解释调用默认最多 1 次，S2 与 S4 共用结果，不得各调用一次。
- 仅当 `0.50 ≤ confidence < 0.85` 且 Top-2 margin `<0.08` 时，允许 `intent_strong` 升档一次。
- L4 主意图阈值：`high=0.85`、`low=0.50`；从意图阈值 `0.70`。
- 高于high直路由；中间区间低风险可执行并标low_conf，高风险必须确认；低于low必须clarify。
- L1可零模型确定性直路由的白名单仅限会话控制和显式publish/trash/restore/purge；build/edit等创作规则只缩窄候选，不能截断多意图检测。
- 每周校准盲测集：目标直路由误判率<2%、clarify率<15%；阈值只允许保守收窄并受router.yaml min/max钳制，自动放宽必须人工ADR。

### 6.5 S5 四层校验

顺序固定：

1. 必填槽完整；
2. 格式和枚举合法；
3. 业务状态、目标存在性、版本与项目归属；
4. 用户权限、Tool 风险、审批要求、配额与执行预算。

业务语义的 `confirm_pending_action` 只能确认唯一未过期的 `pending_action_id + action_hash + target_id + artifact_id`；它不等于风险审批。HIGH/CRITICAL 审批只能通过Gate API记录决定并推进到approved，随后由S6在创建operation时消费；Approval必须绑定 `approval_id + action + target_id + artifact_id + manifest_digest + args_hash + expires_at + fencing_token`。目标、参数、版本或 manifest 改变后原确认自动失效。

### 6.6 S7/S9 职责分离

- S3 保存 provisional SIR 快照；S7 只生成执行事实 delta、推进 canonical SIR pointer 并执行长期记忆决策，不修改 project/artifact/deployment 真相。
- Domain UnitOfWork 是 Project/Artifact/Deployment 唯一写者；ApprovalService 是 approvals/decisions 唯一写者；UsageLedgerService 是 usage_ledger 唯一写者。Stage只能调用服务，S9只提交Turn/message/final refs/outbox，不重写领域事实。这些W0失败时S8不得声称成功。
- S9 每 Turn 归档 attempt；仅终态 finalize，不等待会话关闭。会话关闭只做摘要与资源收口。

---

## 7. C+ 意图与多意图策略

### 7.1 判定不是“闲聊/建站”二分类

系统按 `domain × speech_act × target` 判断。

- “深色网站有什么优点” → `site × ask`，进入 ChatService，不改文件。
- “把首页改成深色” → `site × edit`，进入 SiteWorkflow。
- “那就按刚才建议改” → `site × confirm_pending_action`，必须绑定上一轮唯一 pending action；不能消费发布/删除审批。
- “你好，帮我建摄影站” → social_prefix + `site × create`，寒暄不建 Task。

铁律：**讨论网站不等于修改网站；提出建议不等于授权执行。**

### 7.2 多意图合并

- 社交前缀吸收到最终语气。
- 非执行解释可作为 ResponseFragment，不单独启动 Agent。
- 同一项目多个 edit 合并为一个 SiteSpecDelta，一次修改、一次 Verify。
- 研究后建站按 `Research → Site` 串行。
- “修改并发布”先执行 `Edit → Verify → Preview`；随后为指定 artifact/manifest 创建独立 CRITICAL Approval，审批成功后恢复 Deploy，不把审批建模为普通 Plan Task。
- 主意图或其传递 hard dependency 失败时停止主交付或显式降级；仅非依赖/soft从意图失败不连坐主交付，S8必须说明部分失败。
- 冲突意图、目标不明、跨项目危险组合只能问一个受控澄清问题。

### 7.3 IntentCatalog

每条 Intent 必须声明：

```json
{
  "intent_id": "edit_site",
  "domain": "site",
  "speech_acts": ["edit"],
  "l1": "site_build",
  "l2": "edit_site",
  "risk_level": "low",
  "required_slots": ["project_id", "change_request"],
  "optional_slots": [],
  "shared_slots": [],
  "slot_formats": {},
  "skill": "site_build",
  "produces": ["artifact_version", "preview_url"],
  "dependencies": [],
  "max_steps": 12
}
```

Catalog 必须支持 Schema 校验、热加载前 dry-run、CI 唯一性检查和版本回滚；生产热更新只允许 admin，且必须写 `kb_change_log`。

---

## 8. S6 领域执行策略

S6 首先按领域分发，不默认进入自由 ReAct。

### 8.1 ChatService

适用：普通闲聊、技术问答、网站讨论、设计建议、当前网站评价。

- 通常一次执行模型调用；不创建网站版本。
- 可以读取当前 SiteSpec/版本摘要，但不得直接调用写文件或发布 Tool。
- site_design 的普通讨论只生成 draft SiteSpec ResponseFragment；只有用户明确“记录/采用/应用”时才持久化 requirement_doc。
- 闲聊默认不写长期记忆；只有 User Pin、自纠错、跨任务重复偏好、带理由决策、成功总结才进入 Storage Gate。

### 8.2 SiteWorkflow

固定子流程：

```text
Spec → Produce/Edit → Verify → Repair(max 1) → Preview
```

1. **Spec**：把本轮明确指令合并进 `projects.requirement_doc`（SiteSpec）；低置信要求进入 pending。
2. **Produce/Edit**：新建生成完整版本；修改优先生成受控 patch，禁止原地覆盖 active 版本。
3. **Verify**：执行 HTML/CSS/JS、资源、浏览器 console/network、死链、响应式、SEO、a11y、安全和依赖版本检查。
4. **Repair**：只根据确定性错误做一次定向修复；仍失败时 ExecutionResult 必须为 partial/failed，分别记录 attempted_artifact_id 与 fallback_artifact_id，明确说明本轮修改未生效；不得把上一稳定版本标成新产物。
5. **Preview**：原子写入不可变目录，生成 manifest/checksum 和 preview URL。

网站产物正确性属于 S6 Verify；S8 只检查“返回用户的回复”。

### 8.3 ResearchService

- 只在用户明确要求最新资料、事实查询、竞品研究或 SiteWorkflow 声明硬依赖时触发。
- 搜索/抓取结果必须保留来源和时间；网页内容视为不可信。
- ReAct 最多 5 步；相同动作/参数不得重复；无法证实时必须标明不确定性。

### 8.4 ProjectOps

纯代码处理：发布、回收、恢复、永久删除、版本切换。

- `restore` 为服务方法，不需要 LLM Tool。
- 发布和purge的authoritative risk为`critical`，只能从显式主意图进入。
- 发布指定 artifact revision；上传后执行健康检查，成功才切 active 指针，失败恢复旧指针。
- 永久删除按幂等 job 分步执行，不得在 HTTP 请求内同步完成。

### 8.5 ReAct/Planner 适用边界

- 简单 chat、简单 edit、发布、回收、恢复不得启动 Planner。
- Research 可使用受限 ReAct。
- 复杂多页站、跨页面依赖且 BoundedPlan 无法表达时才可 Planner。
- ReAct最大5步；复杂Plan最大20个内部Task；`max_plan_revisions=2`；Site Repair最多1次。
- 单Task硬超时300秒；连续3个no-op步骤或相同state hash重复3次立即中止；同一Tool 10秒内最多5次。单Tool retry最多3次，必须按error_code白名单，指数退避含jitter，非幂等或unknown结果不得盲重试。
- 执行预算达到80%停止非必要research/repair和升档，90%只完成已启动原子调用，100%停止派发新Task并交付已完成结果。

---

## 9. Skill 与 Tool 最终规范

### 9.1 8 个业务 Skill

对外保留 8 个业务语义，内部复用四领域服务：

| Skill | 执行域 | 说明 |
|---|---|---|
| site_build | SiteWorkflow | 新建/修改静态站 |
| site_design | SiteWorkflow.Spec | 设计讨论转结构化 SiteSpec |
| site_review | SiteWorkflow.Verify | 显式审查入口 |
| req_clarify | SiteWorkflow.Spec | 受控需求澄清 |
| web_research | ResearchService | 联网研究 |
| general_chat | ChatService | 闲聊与非执行讨论 |
| project_manage | ProjectOps | 项目生命周期 |
| doc_write | Chat/Site 输出模式 | 文档产物，不建立独立 Agent |

每个 Skill 目录必须包含 `SKILL.md + skill.yaml + policy.py + run.py`；`skill.yaml` 声明 intent、Tool 子集、风险上限、输入输出 Schema 和预算。

### 9.2 16 个原子 Tool

step1 的 15 Tool 加入 C+ 必需的 `asset_import`，保留既有稳定 Tool ID 并升级实现语义：

| Tool | 风险 | 最终职责 |
|---|---|---|
| web_search | low | 搜索并返回来源元数据 |
| web_fetch | low | 受限抓取、大小/域名/超时控制 |
| rag_query | low | Chroma scope 隔离检索 |
| fs_read | low | 仅允许项目工作区读取 |
| mem_recall | low | 结构化记忆读取 |
| html_validate | low | 升级为整站语法/SEO/a11y/安全审计 |
| browser_capture | low | 升级为截图+console+network+关键交互审计 |
| img_generate | mid | 图像生成并保存来源/成本 |
| asset_import | mid | 上传、MIME/文件名消毒、压缩、WebP/AVIF、manifest |
| fs_write | mid | 临时文件、原子 rename、patch、checksum |
| site_publish | mid | 创建本地不可变预览，不代表生产发布 |
| mem_store | mid | 仅接受 Storage Gate 决策后的数据 |
| site_delete | high | 对不可变版本建立 tombstone；文件修改必须产生新Artifact |
| project_recycle | mid | 项目进入回收站，可逆且审计；恢复走服务方法 |
| project_purge | critical | 永久删除，双确认与 step-up authentication |
| site_deploy | critical | 生产发布，绑定 artifact+manifest、审批、健康检查、回滚 |

风险语义：

- low：只读、无业务副作用，归属校验后自动执行。
- mid：可逆业务副作用，自动执行但必须W0操作账本、审计和幂等。
- high：用户域内高影响动作，单次 Approval Gate。
- critical：生产/永久/跨域动作，白名单路径、step-up authentication、双确认、默认拒绝。

业务风险不替代平台安全。ToolRegistry 还必须声明 `sandbox_profile, egress_profile, filesystem_profile, redaction_profile, max_input_bytes, max_output_bytes, timeout, retry_policy, owner_resolver`。例如 web_fetch 必须阻断 SSRF/DNS rebinding/内网重定向；browser_capture 必须在无平台 Cookie 的隔离浏览器运行；fs_* 必须防 symlink/junction/TOCTOU；asset_import 必须防伪 MIME、SVG 脚本、EXIF和压缩炸弹。

所有Tool必须返回ToolResult，不抛裸异常；mid/high/critical必须先写W0 operation ledger并使用稳定业务幂等键。每个有副作用Tool还必须声明 `reconcile_strategy, probe(operation_key), compensate(result_ref), unknown_timeout, manual_resolution_policy`。恢复时先probe；无法收敛的unknown使Turn进入recovery_pending/needs_manual，禁止自动重试和成功finalize。Skill不得调用未声明Tool，ToolRegistry启动时校验风险、审批、幂等、沙箱、reconcile和Schema。

---

## 10. 数据模型、Repository 与状态机

### 10.1 规范表集合与 purge 边界

所有业务主键统一 `BIGINT UNSIGNED AUTO_INCREMENT`；外部 `turn_id/stream_id/approval_id` 使用不可猜测 ULID 并设唯一索引。

**租户账号线，不随项目 purge**：

`users, user_model_keys`

**项目内容/运行线，随项目 purge**：

`projects, conversations, messages, attempt_messages, turns, turn_checkpoints, artifacts, deployments, tasks, tool_calls, sir_snapshots, session_audits, agent_runs, memory_storage_log, feedback, usage_ledger, recycle_bin, paused_turns, approvals, approval_decisions, authorization_grants`

**操作控制线，项目删除后保留最小无内容记录并按保留期清理**：

`purge_jobs, project_tombstones, outbox_events, admin_audit_log`

**治理/统计线，无内容 FK，purge 保留**：

`metrics_daily, metrics_events, qc_scores, flow_checks, output_guard_log, degradations, intent_decisions, model_calls, kb_change_log, vector_collections`

治理线禁止保存 prompt、reply、Tool args/result、文件路径、带 query 的 URL、项目名、输入摘要、state excerpt 或原始异常正文；跨 purge 标识必须使用独立统计盐产生的 pseudonymous ID。

最终规范删除重复用途表：

- `traces/trace_events` → `turns + session_audits + Redis event stream`；
- `usage_logs` → `usage_ledger + model_calls`；
- `user_states` → `turns + turn_checkpoints + Redis hot state`；
- `frontend_events` → `metrics_events(event_type='frontend_*')`。

在下一次 schema reset 前必须先更新 ORM/Repository/schema_check；否则禁止执行 reset。

### 10.2 关键枚举与状态机

数据库使用**逻辑枚举 + VARCHAR/CHECK**；wire/DB 统一小写，UI 可显示大写标签。Pydantic、CHECK 与前端类型从同一枚举定义生成。

- Project：`draft|active|trashed|purging`
- Conversation：`active|archived`（会话不进入独立回收站）
- Turn：`accepted|running|waiting_clarification|waiting_approval|paused|recovery_pending|needs_manual|completed|failed|cancelled|blocked`
- Task：`pending|running|done|failed|cancelled`
- AgentRun：`running|completed|failed|aborted`
- Artifact：`building|verified|preview_ready|failed|deleted`
- Deployment：`pending|uploading|health_checking|succeeded|failed`；当前active由Project指针表达，不把旧行改回active
- UserModelKey：`active|disabled|invalid`，另用 `last_validated_at` 表示是否验证，不保留冲突的 `is_valid`
- VectorCollection：`ready|building|archived|dropped`
- Approval：`pending_first|first_confirmed|pending_second|approved|rejected|expired|consumed|invalidated`
- ContentPath：`pending|ready|failed|deleted`

合法转换：

```text
Project: draft→active; draft|active→trashed; trashed→draft|purging; purging→物理删除
Task: pending→running|cancelled; running→done|failed|cancelled；终态重试创建新attempt
Artifact: building→verified|failed; verified→preview_ready|failed; preview_ready→deleted(tombstone)
Deployment: pending→uploading→health_checking→succeeded|failed；发布和回滚都创建新Deployment，不重新激活旧行
Approval: high为pending_first→approved→consumed；critical为pending_first→first_confirmed→pending_second→approved→consumed；任一未终态可rejected/expired/invalidated
```

等待审批/补槽不新增 `tasks.status=blocked`，由 Turn 状态、paused_turns、depends_on 和 ValidationResult 表达。

### 10.3 Turn、Approval 与 Operation 真相

`turns` 至少包含：`turn_id,user_id,conversation_id,client_msg_id,request_digest,stream_id,trace_id,status,run_epoch,fencing_token,last_event_id,terminal_error_code,lock_version`，并设置 `UNIQUE(user_id,client_msg_id)`、`UNIQUE(stream_id)`。

`turn_checkpoints` 至少包含：`turn_id,run_epoch,schema_version,code_version,config_version,plan_revision,plan_hash,task_id,task_input_hash,dependency_output_hashes,completed_operation_keys,tool_result_refs,sir_before_id,sir_after_id,artifact_ids,approval_id,usage_refs,response_fragment_refs,last_committed_event_id`。

`approvals` 是审批真相源，至少包含：`approval_id,turn_id,plan_revision,action,target_type,target_id,artifact_id,manifest_digest,args_hash,risk_level,step,challenge_nonce_hash,status,expires_at,created_by,decided_by,decided_at,consumed_at,fencing_token,lock_version`。Gate决策只把Approval推进到approved；S6恢复时在同一UoW内完成 `approved→consumed + 创建operation ledger + Turn恢复running`。high允许 `pending_first→approved`；critical使用两个独立nonce走完整双确认。

resume递增epoch时，ApprovalService原子生成 `authorization_grant(approval_id,approved_plan_hash,old_epoch,new_epoch,expires_at)`；S6校验grant，而不是要求旧Approval fencing等于新fencing。Redis仅用于等待通知。

`attempt_messages` 保存 waiting_clarification/waiting_approval/paused 的已Guard非终态输出，唯一键为 `(turn_id,run_epoch,kind)`；它不是最终assistant message。archive_attempt成功后可发送 `attempt_output`，随后发送非终态 `suspended` 并关闭当前流；恢复后创建新epoch，只有最终epoch写final message和done。

`tool_calls` 中有副作用的调用是 W0 operation ledger：调用前以稳定 `operation_key` 预占为 running，完成后写 succeeded/failed/unknown 与 result reference。恢复时必须先查账本，不得仅依赖 Redis TTL 幂等键。

### 10.4 Artifact 与 Deployment 分离

Artifact 表示不可变内容；Deployment 表示某环境的一次发布。

`artifacts` 至少包含：`project_id,conversation_id,parent_artifact_id,version,site_spec_revision,site_spec_hash,manifest,manifest_digest,checksums,vendor_manifest_version,capability_manifest,status,preview_path,trace_id`。

`deployments` 至少包含：`project_id,artifact_id,manifest_digest,environment,status,previous_deployment_id,health_report,object_prefix,started_at,finished_at`。

Project 分别保存：

- `head_artifact_id`：最新稳定预览/后续编辑基线；
- `published_artifact_id`：当前生产内容；
- `active_deployment_id`：当前成功部署。新发布或回滚都创建新的succeeded Deployment；Deployment终态与两个Project指针在同一MySQL事务切换。

规则：

- `UNIQUE(project_id,version)`；版本号在数据库事务内通过 project lock_version/CAS 分配。
- 路径：`previews/{uid}/{pid}/v{n}/...`；Artifact 只存 path/object key，短期 `preview_url` 动态签发，不作为真相字段。
- manifest 使用 canonical UTF-8 JSON，路径排序；`manifest_digest=SHA-256(canonical_manifest)`。
- 新版本只追加，不覆盖vN；删除文件必须产生copy-on-write新Artifact；普通版本操作只能整体tombstone，head/published/仍被引用版本不得删除。只有进入受审计project purge、撤销全部指针并通过generation fencing后，才允许物理删除。
- `messages.content_path[]` 只追加稳定 `ref_id,artifact_id,kind,object_key,status,manifest_digest`；若需要独立状态 CAS，应升级为 `message_artifact_refs` 表。

### 10.5 Repository、UnitOfWork 与 Saga

- Repository 接收调用方 AsyncSession，只操作单表，禁止 commit/rollback/begin、禁止调用其他 Repo、禁止向领域层泄漏延迟加载 ORM。
- Service/UnitOfWork 是事务唯一所有者；W0 业务变更与 outbox_events 必须同事务提交。
- CAS 使用 `UPDATE ... WHERE id=:id AND status=:from AND lock_version=:expected`；affected rows 不等于1必须返回 conflict。
- MySQL 与 COS/Chroma 不宣称分布式事务；Deploy/Purge 使用持久 Saga、幂等 step、补偿动作和可重入状态。
- 所有生产 MySQL Engine 必须 `pool_pre_ping=True,pool_recycle=1800`。
- `schema_check` 验证表、字段、唯一索引、FK、ON DELETE、CHECK、禁止表与统计表内容禁令。

### 10.6 Project purge 顺序

`project_tombstones` 无Project FK，至少包含 `project_id,purge_generation,status,created_at,completed_at`，保留期不得短于Outbox/日志最长重放窗口。`Project CAS trashed→purging + tombstone upsert + purge_jobs创建` 必须在同一MySQL事务完成；`project_tombstones` 与 `purge_jobs` 都设置 `UNIQUE(project_id,purge_generation)`。所有资源写入和Outbox sink在写前、提交前均校验tombstone/generation。

```text
1. 同一事务CAS trashed→purging、递增purge_generation、upsert tombstone、创建唯一purge_job并冻结新Turn/发布/写入；
2. 启动该purge_job；所有Outbox sink校验tombstone/generation，取消或scrub旧generation的pending/processing事件并等待失效；
3. 撤销签名与active deployment；
4. 按vector_collections注册表删除项目Chroma；
5. 删除本地Artifact、私有COS Artifact与备份索引；
6. 删除生产COS所有object versions、delete markers和未完成multipart；
7. LIST验证所有项目COS前缀为空，再反向验证Chroma/本地无canary；
8. MySQL事务清空head/published指针并删除项目内容线；
9. purge_jobs=succeeded；任一步失败保持purging并记录step/error，允许幂等重试。
```

`projects.user_id → users.id ON DELETE RESTRICT`；项目子表 `project_id → projects.id ON DELETE CASCADE`；`purge_jobs.resource_id` 不建项目 FK。项目物理删除后，完成状态由 purge_jobs 表达，不保留 `projects.status=deleted`。

---

## 11. Redis、Chroma、文件与写入策略

### 11.1 Redis 键

全部使用 `ai:` 命名空间：

```text
ai:stream:{stream_id}                    Redis Stream，事件回放
ai:cancel:{turn_id}
ai:clients:{stream_id}
ai:gate:approval:{approval_id}             审批等待通知缓存，非真相
ai:lock:conv:{conversation_id}
ai:lock:project:{project_id}                带fencing token
ai:sir:{conversation_id}
ai:sir:snap:{conversation_id}
ai:turn:{turn_id}:checkpoint
ai:tool:idem:{operation_key}
ai:ratelimit:user:{uid}:rpm
ai:ratelimit:user:{uid}:token_daily
ai:session:{conversation_id}
ai:stream:persist
ai:stream:error
ai:stats:*
```

事件流统一使用 Redis Stream（XADD/XREAD/XRANGE）。配置冻结：`stream_maxlen=5000`（Redis `MAXLEN ~` 近似裁剪）、`stream_ttl_seconds=7200`。活跃 Stream 每次 XADD 刷新 TTL，只有终态后开始固定过期。Redis Stream ID 字符串同时作为 SSE `id/event_id`；`seq` 仅用于展示。`after` 早于现存第一条时必须发送 `reconnect{reason:"gap",reset_required:true}`，禁止伪装连续。过期后从 MySQL turns/messages/tasks/checkpoints 恢复。`ai:stream:persist` 使用 consumer group、XACK、XAUTOCLAIM；`ai:stream:error` 是 DLQ。

### 11.2 Chroma 物理隔离

- 用户：`u_{uid}_mem`
- 项目：`p_{pid}_design`, `p_{pid}_code`, `p_{pid}_memory`
- 全局只读：`kb_design`, `kb_intent`, `rag_corpus`
- 生成缓存：默认按租户隔离；`cache_key=HMAC(cache_secret,uid|provider|model_revision|prompt_version|input_hash|tool_config_hash|safety_policy_version)`，TTL 7天

缓存文档必须带 `owner_user_id,privacy_class,expires_at`，查询同时验证注册表 owner 与 metadata owner。跨租户缓存默认关闭，仅允许确定性标记为 `public-cache-safe` 且不含用户输入、项目内容、BYOK输出和授权资源的请求。Chroma 无原生 TTL 时由 cleanup job 删除过期文档。

`vector_collections` 至少记录 `logical_scope,owner_type,owner_id,physical_name,embedding_model,dimension,schema_version,state,last_snapshot_at,dropped_at`；查询和 purge 必须按注册表，不得只拼集合名。embedding 固定 `text-embedding-v3`、1024维；未知集合 reset 时 fail-closed 保留并阻止“重置完全成功”的结论。用户偏好反转使用 `superseded_by`，项目方向否定标 `stale`；代码索引按 AST/symbol 增量切片并使用幂等 chunk ID。

### 11.3 文件/COS

- 本地预览是第一交付路径；`ARTIFACT_DIR` 必须位于持久卷，写入使用临时目录→fsync→校验→原子rename。Artifact进入preview_ready前必须完成第二耐久副本确认：写入同一COS桶的私有 `artifacts/{uid}/{pid}/{artifact_id}/{manifest_digest}/` 前缀并使用签名访问；未完成副本时只能保持verified。每日加密备份作为第三层恢复手段。
- 预览必须由独立无凭证 Origin 提供；主站 Cookie Domain 不得覆盖预览域；iframe 使用最小 sandbox，默认禁止 top-navigation、弹窗、下载和任意网络；平台注入 CSP 不允许生成内容覆盖。
- COS单桶分为私有Artifact前缀与生产前缀：私有前缀只供签名预览/恢复，生产前缀供发布站点；两者键都包含uid/pid/artifact_id/manifest_digest，禁止覆盖旧版本。发布健康检查验证manifest、关键资源、浏览器console/network和版本标识；成功后才切published/active deployment，失败保持旧指针。
- 外部 CDN 禁用；依赖库存放 `/vendor/libs/`，锁版本、来源、SHA256 和 CVE。页面引用 vendor 时，site_deploy 必须同步对应文件到 COS；缺失 `missing_vendor_on_deploy` 必须 block。
- purge 必须删除 COS 所有 object versions、delete markers 和未完成 multipart；普通 DELETE marker 不算物理删除成功。

### 11.4 写入等级

- **W0 同步真相**：turns、messages、attempt_messages、project/conversation、tasks、authoritative tool_calls、artifacts、deployments、usage_ledger、recycle/purge job、approvals/decisions/authorization_grants、paused_turns、turn_checkpoints、user_model_keys、intent_decisions、degradations、outbox_events。
- **W1 至少一次**：session_audits、agent_runs、memory_storage_log/Chroma、feedback、qc_scores、flow_checks、output_guard_log。W1不阻断正常回复，但必须通过事务Outbox、幂等sink、reconciler和DLQ保证可补偿，不能称为“可丢”。
- **W2 可重建遥测**：metrics_events、model_calls统计视图和聚合输入。authoritative tool_calls/usage_ledger 永远不是仅W2数据。

S9同一MySQL事务提交W0终态和W1/W2 Outbox；最终正文与终态事件也写入带稳定 `terminal_event_key` 的Outbox。Dispatcher至少一次XADD到Redis，前端按event_id/terminal_event_key去重；规范只承诺**业务有效一次（effectively-once）**，不宣称MySQL与Redis物理exactly-once。只有W0提交后才能发送final token和done(success)。Worker按2秒或200条批刷，消费成功XACK，失败最多5次后DLQ并告警。模型调用用call_id幂等落usage ledger；副作用Tool用operation_key幂等落操作账本。

---

## 12. StreamBroker、API 与前端协议

### 12.1 API

- `POST /api/chat`：请求 `{client_msg_id,conversation_id,message,expected_conversation_version?}`，响应 `text/event-stream`；第一条 reconnect 事件返回 turn_id/stream_id/reused。
- `GET /api/streams/{stream_id}`：使用 `after` 或 `Last-Event-ID`（after优先）重连/跨设备回放。
- `GET /api/turns/{turn_id}`：Stream过期后的最终状态和恢复入口。
- `POST /api/turns/{turn_id}/control`：`stop|pause|resume|correct|supplement|discard`；approve不属于control。
- `GET /api/gate/pending`：从MySQL恢复待审批项。
- `POST /api/gate/{approval_id}`：提交 `approve|reject + decision_nonce`；重复消费返回409。
- `POST /api/projects/{project_id}/purge`：仅创建job，返回202；`GET /api/purge-jobs/{job_id}` 查询step/status/error。
- `POST /api/admin/operations/{operation_key}/resolve`：仅管理员step-up后提交 `confirmed_succeeded|confirmed_failed|compensated`；同一UoW更新operation ledger、Turn状态并签发新fencing epoch，写admin_audit_log。未裁决前禁止重试、补偿和成功finalize。
- 项目/版本只读查询可走独立REST；发布、回收、恢复、purge、版本切换等mutation必须进入统一ActionService，生成可审计Turn/SystemAction，复用S0幂等、S5风险、S6 ProjectOps和S9，禁止API直调Repo/Tool绕过审批。结构化mutation可把S1–S4标记deterministic no-op，但不能跳过S5/S9。
- 基于Cookie的Gate、发布、purge接口必须执行CSRF防护和step-up authentication。

### 12.2 事件类型

`stage, task, tool, token, state_diff, approval, attempt_output, suspended, usage, capability_notice, error, reconnect, done`；心跳使用 SSE 注释 `:heartbeat`，15秒一次，不进入业务序号。每个Stream最多一个终态 `done` 或 `error`。`token` 只能承载已通过S8并已完成W0提交的 `reply_final` 分块，不得承载Provider原始token。HTTP headers发出后的错误统一使用SSE ErrorEnvelope。

### 12.3 多设备与断连

- 同一 stream 的第二订阅者加入现有生成，不重调 Provider、不重复计费。
- 最后一个客户端离开后进入短暂宽限；仅确认无客户端且策略要求时才 cancel。
- StreamBroker 无权修改 SIR 或项目；只管理传输、回放、订阅和控制信号。

### 12.4 前端展示

后台保留 S0–S9，前端产品视图压缩为五阶段：

1. 理解需求（S0–S4）
2. 检查条件（S5）
3. 构建网站（S6 Produce/Edit）
4. 检查并生成预览（S6 Verify/Preview + S8）
5. 完成/等待操作（S9 / approval）

前端三层：

- Stage Rail：五阶段产品进度；开发模式可展开 S0–S9。
- Activity Panel：Task/Tool、输入摘要、输出、耗时和错误。
- Chat Thread：正文、能力说明、预览、审批和下一步。

离线消息进入 IndexedDB，状态 `pending|queued|failed|draft-saved`；恢复后按 client_msg_id 串行幂等提交。

前端必须使用单一 StreamReducer：以 `(stream_id,event_id)` 去重，以 `seq` 检测缺口，replay 与实时事件先排序后归并；终态后必须调用 Turn API 对账。`state_diff` 只能按 version/CAS 应用，乱序必须请求快照。Approval UI 支持过期、失效、第一/第二次确认和重认证；preview URL 过期必须重新签发，不得把签名URL当永久字段。协议TypeScript类型在M2由后端Schema生成，M3提供事件模拟器，不能等M9再对接。

---

## 13. 干预、审批、暂停与恢复

### 13.1 控制事件与合法重入

控制事件为 `stop|pause|resume|supplement|correct|discard`；Approval decision只走Gate API，不属于控制事件。

fencing切换顺序固定：stop/pause/correct请求只写requested状态；当前Worker在安全点使用旧token提交checkpoint/收口；随后CAS切换Turn状态并递增run_epoch；resume取得新token。不得先失效旧token再要求旧Worker写checkpoint。

| 事件 | 合法路径 |
|---|---|
| stop | 写stop_requested；旧Worker安全收口/检查点提交后CAS为cancelled并递增epoch；S7/S8/S9收口 |
| pause | checkpoint并进入paused；不再派发Task，S9 archive_attempt但不finalize |
| resume | 校验schema/code/config/checkpoint，从首个未完成且仍有效的S6 Task恢复 |
| supplement | S2→S3→S4→S5→受影响S6子图→S7→S8→S9 |
| correct | 回滚绑定快照，再走S2→S3→S4→S5→失效闭包对应S6子图→S7→S8→S9 |
| discard | 先cancel当前执行；如涉及资源删除，作为新的受审计ProjectOps action进入S5 |
| approval decision | Gate CAS到approved；plan/args/manifest未变时由S6同一UoW消费并创建/恢复operation，变化则invalidated并回S5 |

输入哈希、依赖版本和 operation key 未变化的已完成 Task/Tool 不得重复执行。correct/supplement 导致输入变化时计算 invalidation closure：旧Task保留历史，新建 `plan_revision,input_hash,attempt_no,supersedes_task_id,dependency_output_hash` 的 TaskAttempt，只执行受影响Task及后继。旧fencing token的迟到结果不得进入当前ExecutionResult。

### 13.2 Approval Gate

- high：单次审批，TTL 30分钟，绑定action/target/artifact/manifest/args/fencing。
- critical：白名单路径、step-up authentication、两个独立challenge/nonce的顺序确认；两次不得由同一请求完成。
- 自然语言“可以/继续”只能确认普通pending action，不能消费Approval。
- 超时默认拒绝；刷新/跨设备从MySQL `/api/gate/pending` 恢复。
- MySQL approvals/approval_decisions 是真相；Redis仅通知。Gate只决策到approved；S6启动operation时CAS消费且只能一次。若operation已存在则恢复原执行，任何目标变化使approval invalidated。

### 13.3 Checkpoint 与 partial 持久化

Checkpoint 完整字段见 §10.3。Redis 是热副本，MySQL `turn_checkpoints/turns/tasks/tool_calls/artifacts/approvals/usage_ledger` 是长期恢复依据。

长生成每5秒或累计256字符（先到者）把**脱敏后的内部 draft partial**写入加密的 turn_checkpoints/outbox，不写最终 assistant message、不向客户端发送。Checkpoint W0 提交后更新 last_committed_event_id；恢复时先查 operation ledger 和 usage ledger，再决定复用、补偿或执行。

---

## 14. 模型、配额、BYOK 与降级

### 14.1 模型档位

- `intent_lite`：默认 qwen-turbo，用于S2/S4模糊理解及必要的S8回复风险判定。
- `intent_strong`：默认 qwen-plus，仅Top-2难分时一次升档。
- `exec_standard`：默认 hy3；`exec_pro`：默认 qwen；`exec_ultra`：默认 deepseek且按量计费。
- `exec_standard|pro|ultra` 仅S6使用并按用户设置选择；Planner默认使用用户档，不得无授权硬切pro。
- embedding：`text-embedding-v3/1024`，平台凭证。

默认映射只写入models.yaml，不硬编码在业务代码。前端必须明确提示 `exec_ultra` 按量计费；BYOK时提示“由你的Key计费”。

模型调用预算分开：`understanding_lite_calls≤1`、`understanding_strong_calls≤1`、`output_guard_lite_calls≤1`。S8规则每次执行；只有回复风险规则命中或高风险公开输出时才使用自己的guard预算。

### 14.2 配额冻结值

当前 free/pro/max 均使用：

- 用户每日预算 5,000,000 token；项目每日预算 5,000,000 token；
- 60 RPM/用户；最大并发定义为“同一用户同时处于running或waiting_provider的Turn”5个，而不是打开的Conversation数量；
- **每 Turn** 执行预算 2,000,000 token；
- 80%：禁止Planner升档及非必要repair/research；90%：只完成已启动原子调用；100%：拒绝新调用并返回结构化429。

RPM在S0原子扣减；并发Turn使用Redis lease（获取/续期/释放）并由MySQL turns reconciler回收崩溃租约；每次Model Harness调用前使用Redis Lua按 `call_id` 原子reserve用户/项目/Turn预算，结束后按Provider真实usage settle，多退少补。MySQL usage_ledger是对账真相，reconciler修正Redis漂移；每日窗口使用UTC。Redis不可用时平台计费模型必须MySQL fallback或fail-closed，不得无限fail-open。BYOK仍计入RPM、并发和反滥用token上限。

### 14.3 BYOK

只覆盖S6三个执行档；不得覆盖intent/embedding。AES-256-GCM使用12B随机IV、16B tag，AAD绑定 `user_id+key_id+provider+kek_version`；密文保存iv/tag/kek_version/fingerprint/rotation/validation时间。主密钥64 hex，支持双版本解密和在线重加密，缺失生产启动失败。明文Key只在单次Harness调用作用域存在，不进入日志、Span、Prompt持久化或响应。

provider/model/base_url必须来自allowlist，禁止任意base_url导致SSRF或凭证外送。BYOK失效/限流不得静默切平台付费Key；必须返回结构化错误或取得明确同意。user_model_keys不随项目purge，只随用户撤销或账号删除处理。

### 14.4 静态能力降级

- L0：原生 HTML/CSS/JS、静态图表、浏览器存储、Markdown、静态部署。
- L1：登录/CMS/表单/订单/支付/聊天等生成 Mock 或 localStorage/IndexedDB 演示，并同时通过对话、页面 `demo-notice`、SSE `capability_notice` 告知限制。
- L2：服务端代码、真实密钥、真实敏感数据、域名备案证书代办；不得伪装完成。

每个受限功能必须产生 `CapabilityDecision(feature,tier,implementation,limitation,upgrade_hint,notice_required)`，同时写入 SiteSpec、Artifact capability_manifest、SSE capability_notice 和 degradations。L2 在S5/S6确定性阻断；L1若页面缺少可见demo-notice不得preview/deploy。用户拒绝Mock时对应ActionItem进入cancelled，不得偷偷降级。`undisclosed_mock_rate` 的分母为全部L1 Artifact功能、分子为缺任一三处告知者，必须为0。

---

## 15. 安全、可观测性与灾备

### 15.1 安全

- 密码使用Argon2id（兼容迁移可读bcrypt后重哈希）；禁止明文或可逆密码。
- JWT access 30分钟、refresh 7天；refresh带jti/family_id并轮转、检测reuse；登出/改密递增token_version并撤销family。Redis不可用时高风险Gate/purge fail-closed。
- RBAC至少user/admin；`/admin/*`独立中间件。所有资源做owner校验；管理操作写admin_audit_log。
- Gate、purge、生产发布要求CSRF防护和step-up authentication。
- 预览/下载必须使用短期签名URL和独立无凭证Origin；生产公开站点除外。
- 生成站点禁止注入平台Key、用户Key或真实PII。
- 路径Tool必须canonicalize并防symlink/junction/TOCTOU后确认仍位于项目工作区。
- `html_validate`检查危险标签、外链、密钥、CSP、已知漏洞依赖；`browser_capture`使用隔离容器和受限egress。
- 安全日志不得保存prompt/output/excerpt；必要取证只保存分类、hash和受控safe_details。

### 15.2 可观测性

- `trace_id` 贯穿 HTTP、Stage、Task、Tool、LLM、Repository 和 Stream。
- 日志统一 `getLogger("app.<module>")`，JSON结构化，按日滚动，禁止明文密钥/PII。
- Model Harness 记录 TTFT、总延迟、token、cost、成功率和错误类型。
- 指标包含p50/p90/p99、路由误判率、clarify率、生成成功率、自动修复率、部署成功率、回放成功率、Outbox/DLQ积压；label只允许低基数枚举，禁止user/project/trace作为指标label。
- `/healthz`仅进程存活；`/readyz`判断是否接收新Turn；受鉴权运维端点暴露依赖、队列和熔断；OpenTelemetry/Prometheus用于指标和Trace。
- 初始SLO：API非模型请求p99<500ms；SSE首个进度事件p99<1s；事件回放成功率≥99.9%；部署不破坏旧active版本=100%；W0终态伪成功=0。告警持续5分钟触发，恢复持续5分钟解除；每条告警必须有runbook和责任角色。

### 15.3 优雅停机与熔断

- SIGTERM 后停止接收新 Turn，给活跃客户端发送 reconnect，等待最多25秒；超时任务写 checkpoint。
- 管理 drain 最多60秒；随后停止 Worker、刷持久队列、逆序释放资源。
- Provider 连续超时/5xx 触发熔断并使用配置的 fallback；分类模型失败回退规则+low_conf；向量失败回退结构化偏好/缓存。
- S8 模型失败不能跳过确定性安全规则。

### 15.4 灾备

- MySQL：每日全量+binlog，RPO<5分钟、RTO<30分钟；备份凭证与生产部署凭证隔离，静态加密并跨账号保存。
- Chroma：每日与变更前快照，记录snapshot_generation；丢失时可从MySQL来源文档重建。
- COS：版本化、生命周期与跨区/跨账号副本；本地ARTIFACT_DIR每日加密备份并校验manifest checksum。
- Redis：普通缓存实例可RDB 15分钟；关键Stream/checkpoint/persist queue必须部署到启用 `appendonly yes,appendfsync everysec` 的专用实例。AOF是实例级配置，不能描述为单Key开启。
- 恢复顺序：MySQL→Artifact/COS→Chroma→Redis热态重建；running Turn转recovery_pending后按checkpoint/tool ledger恢复。
- 备份中的已purge内容只在固定备份保留窗口内隔离存在，普通恢复不得重新暴露，保留期到期后销毁。
- 每季度在隔离环境演练，验证checksum、FK、active deployment、purge tombstone并记录实测RPO/RTO和报告路径。

---

## 16. 测试与质量门禁

### 16.1 测试层级

1. 单元：纯函数 DST、路由门控、BoundedPlan、风险矩阵、状态机、ResponseComposer。
2. 契约：IntentCatalog、Tool/Skill Schema、SSE、Error、SiteSpec、manifest。
3. 集成：真实MySQL/Redis/Chroma，模型使用可控Stub；验证UoW、W0+Outbox、W1至少一次、W2可重建和Saga补偿。
4. E2E：真实API/SSE/前端，覆盖建站、修改、闲聊、多意图、审批、断线恢复、预览隔离和发布回滚；必须断言DB、文件、事件、调用次数和账本，不以“收到done”作为唯一通过条件。
5. 安全：注入、越权、CSRF、SSRF/DNS rebinding、路径穿越/junction、SVG/压缩炸弹、密钥泄露、恶意预览、审批重放和跨租户缓存。
6. 恢复：在Tool成功/W0前、W0后/SSE终态前分别强杀进程；Redis重启、Provider超时、Outbox/DLQ积压、发布失败、purge中断。
7. 负载：60RPM、并发5、事件背压、长站点生成和多设备订阅；持续时间、数据规模和p99阈值写入测试配置。

### 16.2 冻结回归数量

- 现有 DST 39 项必须全绿；
- 路由/单意图单元测试总量目标至少 200；
- 多意图标注盲测用例至少24，和阈值调参集隔离，记录版本、正负样本比例、分母及95%置信区间；
- 全链路E2E至少20场景，每个场景包含事件/DB/Artifact/usage断言；
- 后端 Ruff+mypy，前端 ESLint+Prettier+Vitest；
- 核心模块覆盖率至少 70%，状态机/风险门/DST 至少 90%。

### 16.3 必测场景

- 纯闲聊只调用一次执行模型，不写网站版本；
- 网站讨论不执行修改；
- 明确 edit 生成新版本并保留旧版本；
- 寒暄+建站只生成一个网站 Task；
- 多 edit 合并一次执行；
- research→build 顺序正确；
- 从意图失败仍交付主结果；
- high暂停审批、critical双确认；跨用户、过期、重放、目标/manifest变化、Redis重启均不得绕过；
- stop/correct后只创建受影响Task的新attempt，旧fencing结果被拒收；
- 断线重连不重调Provider、不重复Tool和计费；Stream gap明确要求状态重置；
- 未经S8的原始正文从未发送；W0失败不得发送done(success)；
- 恶意预览无法读取主站存储、Cookie或调用Gate/发布/purge API；
- 发布失败published/active deployment不变，健康检查校验manifest和浏览器错误；
- purge删除MySQL、Redis、Chroma、本地、COS所有versions/delete markers/multipart及私有缓存，治理聚合保留且无excerpt；
- 所有错误都使用ErrorEnvelope并具备what/why/next。

---

## 17. 实施里程碑、提交与回滚

原则：新 Pipeline 是唯一目标路径；每个里程碑独立本地 commit，不 push。不得长期维护新旧双链路。

每个里程碑任务必须填写 DRI 角色（Backend/Frontend/QA/Security/Ops）、强制 Reviewer、目标日期、CI Job、证据 URI 和回滚负责人；缺任一字段不得标 completed。

| 里程碑 | 交付内容 | 前置 | 退出标准/回滚点 |
|---|---|---|---|
| M0 规范冻结 | 本规范、ADR、字段级Schema、Requirement→Test矩阵 | 无 | 用户确认；所有P0裁决完成；旧文档标记历史 |
| M1 基线修复 | Python3.13容器、clean HEAD、secret scan、移除旧confirmed旁路 | M0 | 干净worktree import/build/test通过 |
| M2 契约骨架 | contracts、Turn/Approval/Artifact/Deployment/Outbox、Pipeline、StageResult | M1 | 十阶段空跑；后端Schema生成TS类型；事件模拟器可用 |
| M3 Transport/S0 | POST SSE、StreamBroker、Turn幂等、Budget reserve、控制事件 | M2 | 断线/gap/多设备/重复提交/PII测试通过 |
| M4 S1–S4 | Recall、SIR/DST、UtteranceFrame、IntentBundle、BoundedPlan | M3 | 39 DST+路由盲测/多意图指标达标 |
| M5 S5/HITL | 四层校验、Approval持久状态机、PausedTurn、CSRF/step-up | M4 | high/critical所有入口无绕过 |
| M6 Tool平台 | Registry、Harness、Operation Ledger、沙箱/egress/fs profile、vendor校验 | M5 | 16 Tool契约与安全测试全绿 |
| M7 S6 Domains | Chat/Site/Research/ProjectOps、Artifact/Deployment/Purge Saga | M6 | 领域集成测试通过；尚不宣称全E2E完成 |
| M8 S7–S9 | Canonical SIR、Memory Gate、ResponseComposer、W0+Outbox、finalize | M7 | 审计、计费、effectively-once终态和补偿闭环；核心API E2E通过 |
| M9 前端闭环 | 五阶段Rail、Reducer、Approval、离线队列、独立预览Origin | M8 | 浏览器/viewport矩阵、刷新/跨设备/恶意预览E2E通过 |
| M10 运维安全 | BYOK轮换、熔断、SLO告警、备份恢复 | M9 | 恢复演练、安全测试、runbook通过 |
| M11a 切换准备 | 生产数据清单、备份、影子环境恢复、reset dry-run | M10 | checksum/FK/恢复验证通过，用户收到影响清单 |
| M11b 小流量切换 | 新链路小流量、旧链停止写入 | M11a | 指标与E2E达标，可一键回退旧版本 |
| M11c 删除旧链 | 删除旧agent/queue/cascade/roles与兼容参数 | M11b | clean HEAD全门禁通过 |
| M11d 生产重置 | 仅在仍确有必要时执行reset_all并重建 | M11c | 再次破坏性确认后单独执行；失败按恢复Runbook处理 |

数据库 reset 只允许在 M11d 且完成以下条件后：

1. ORM/Repository/schema_check 与本规范表集合一致；
2. reset_all dry-run 报告无未知删除对象；
3. 用户收到所有受影响路径、库、Redis DB、Chroma集合清单；
4. 用户再次明确确认生产清空；
5. 备份或用户明确接受不可恢复；
6. 重置后立即执行 seed、启动、自检和 E2E。

---

## 18. 最终验收矩阵

| Req ID | MUST验收 | DRI/Reviewer | Test ID/CI Job | 证据路径（实施时生成） | 阻塞 |
|---|---|---|---|---|---|
| REQ-FLOW-001 | 十阶段唯一、StageResult与skip闭环 | Backend/QA | CT-STAGE-001 / backend-contract | `artifacts/acceptance/stage-contract.json` | M2 |
| REQ-CHAT-001 | 闲聊/讨论/执行不混淆 | Backend/QA | RT-INTENT-001 / router-blindset | `artifacts/acceptance/intent-matrix.json` | M4 |
| REQ-MULTI-001 | 最多3个ActionItem、hard/soft依赖与部分交付 | Backend/QA | RT-MULTI-001 / multi-intent | `artifacts/acceptance/multi-intent.json` | M4 |
| REQ-SITE-001 | Artifact不可变、manifest可复现 | Backend+Frontend/QA | IT-ARTIFACT-001 / site-domain | `artifacts/acceptance/artifact-manifest.json` | M7 |
| REQ-APPROVAL-001 | 所有入口无隐式high/critical动作 | Security+Backend/QA | SEC-APPROVAL-001 / security-gates | `artifacts/acceptance/approval-security.json` | M5 |
| REQ-RECOVER-001 | 干预/崩溃后不重复Tool/计费 | Backend+Ops/QA | REC-TURN-001 / recovery-chaos | `artifacts/acceptance/recovery.json` | M8 |
| REQ-FINALIZE-001 | 未Guard正文不外发，W0失败无success done | Backend+Security/QA | SEC-OUTPUT-001 / output-finalize | `artifacts/acceptance/output-finalize.json` | M8 |
| REQ-PREVIEW-001 | 恶意预览无法访问主站凭证/API | Security+Frontend/QA | SEC-PREVIEW-001 / preview-sandbox | `artifacts/acceptance/preview-sandbox.json` | M9 |
| REQ-DATA-001 | purge逐存储清零、治理聚合无内容 | Backend+Ops/Security | IT-PURGE-001 / purge-canary | `artifacts/acceptance/purge-proof.json` | M10 |
| REQ-OBS-001 | W0+Outbox审计/用量可对账 | Backend+Ops/QA | IT-OUTBOX-001 / accounting | `artifacts/acceptance/accounting.json` | M8 |
| REQ-DEPLOY-001 | 健康检查失败不切active且可回滚 | Backend+Ops/QA | E2E-DEPLOY-001 / deploy-recovery | `artifacts/acceptance/deploy.json` | M10 |
| REQ-PII-001 | 原始PII不进入模型/日志/缓存/SSE | Security/QA | SEC-PII-001 / privacy-scan | `artifacts/acceptance/pii-scan.json` | M3 |

上线硬门槛：

- 所有 MUST 条款具备测试或审计证据；
- P0/P1安全问题为0；
- 路由直通误判率 `<2%`，clarify率 `<15%`；
- 部署失败不得破坏旧 active 版本；
- `undisclosed_mock_rate=0`；
- 干净 Git worktree 可完成安装、启动、测试和构建；
- 不存在 `pass/TODO/NotImplementedError` 或省略错误处理的生产路径。

---

## 19. 冲突裁决与术语统一

| 冲突 | 最终裁决 |
|---|---|
| C+是否替代十阶段 | 否；C+嵌入S2/S4/S6/S8 |
| S2/S4都做分类 | S2提取语义变化，S4最终路由/切分；共享一次intent调用预算 |
| S4任意DAG vs BoundedPlan | 默认最多3项串行；复杂模式才DAG，绝对上限20 |
| S1首轮总召回 vs 按需 | 结构化偏好可轻读；向量召回必须过Gate |
| S8纯规则 vs 每次LLM | 规则必跑；回复风险命中才调用独立guard预算；正文Guard后再外发 |
| 网站质量检查放S8 | 网站审计属于S6 Verify；S8只守回复 |
| Task blocked状态 | 不增加blocked；使用Turn/paused_turns/depends_on/validation |
| Vector状态两套枚举 | `ready|building|archived|dropped` |
| usage_ledger是否统计永保 | 它是有FK W0计费明细，随内容purge；model_calls/metrics保留 |
| Redis LIST vs Stream回放 | 统一Redis Stream；Redis ID=SSE event_id，另有seq |
| 15 Tool是否足够 | 保留既有15个稳定ID并加asset_import，共16个 |
| project_recycle风险 | 可逆操作定为mid；purge/deploy才是critical |
| 8/9 Skill数量 | 最终8个业务Skill，内部4执行域，无Role Agent |
| Artifact是否表示部署 | 否；Artifact是不可变内容，Deployment是环境发布 |
| 审批真相存储 | MySQL approvals/decisions；Redis仅通知；critical双nonce |
| W1审计是否可丢 | 不可丢；W0+Outbox提交，W1至少一次，W2仅可重建遥测 |
| S9是每轮还是会话结束 | 每Turn归档；会话结束仅做摘要/释放 |
| 数据库名大小写 | 以DATABASE_URL为准，代码和规范不得硬编码库名 |
| Python运行时 | 全环境Python 3.13 |
| step3 Stage Rail命名 | 后台严格S0网关/S1召回/S2理解/S3合并/S4分类/S5校验/S6执行/S7回写/S8出口/S9归档 |

---

## 20. 实施前最终检查清单

- [ ] 本规范经用户确认并标记生效。
- [ ] v2/step1/step2/step3 页首标记“已被本规范替代”。
- [ ] 创建 ADR 目录与决策模板。
- [ ] 修复 Python/Docker 版本和干净 HEAD 缺失依赖。
- [ ] ORM删除重复traces/trace_events/usage_logs/user_states，新增turns/checkpoints/approvals/deployments/outbox并补Artifact可复现字段。
- [ ] FK/唯一索引/状态转换/Purge矩阵形成机器可读Schema并由schema_check验证。
- [ ] IntentCatalog、16 Tool稳定ID、8 Skill及平台安全profile冻结。
- [ ] Pipeline/StageResult/Turn finalization契约测试先于业务实现完成。
- [ ] 后端SSE Schema生成前端类型；Reducer与事件模拟器冻结。
- [ ] Approval、PII、预览隔离、恢复、配额reserve/settle、发布回滚、purge canary测试先写。
- [ ] 每个里程碑填写DRI/Reviewer/CI/证据URI/回滚负责人。
- [ ] 生产reset保持dry-run，直到M11d再次获得明确确认。

---

## 21. 来源追踪

本规范合并并裁决以下文件：

1. `全链路重构规划方案v2.md`：十阶段、TurnContext、Guardrails 与总体迁移骨架；
2. `Agent全链路执行总图·完整详版.md`：阶段原理、红线和职责分离；
3. `step1_工具技能与数据库设计.md`：Tool/Skill、数据、Repository、存储与统计；
4. `step2_路由意图与执行流程设计.md`：路由、SIR/DST、多意图、S5/S6、模型档位；
5. `step3_运行时基础设施设计.md`：StreamBroker、SSE、恢复、配额、BYOK、前端、灾备和测试；
6. `artifacts/十阶段与C加合并裁决-2026-08-01.md`：C+ 与十阶段的最终关系。

本规范不是摘要，而是以上设计的**唯一可执行终态候选**。用户确认并完成M0机器契约冻结后，它成为后续代码、测试、数据库重置和生产发布的唯一依据。
