# 第一步 · Tools / Skills / 数据库 重新设计（历史依据）

> **状态：历史依据。自 2026-08-01 起已被《SeedAI全链路重构最终实施规范.md》替代；新代码、Schema、配置、测试和生产变更必须以最终规范为准。**
>
> 定位：**当作全新项目**设计，旧代码视为已删除，不迁就任何历史命名/结构。
> 设计依据：近 5 个月行业调研（MCP 工具规范 2026、Anthropic/OpenAI Agent Skills 规范、生产级 Agent DB 建模、Chroma 集合/多租户最佳实践、Redis 分布式会话状态最佳实践）+ 本平台新网站功能 + 生产级标准。
> 本文件只覆盖**第一步**：Tools、Skills、数据库（MySQL / Chroma / Redis）。

## 已采纳的 5 项关键决策（来自用户 2026-07-31）
1. **主键**：`BIGINT UNSIGNED AUTO_INCREMENT`（非 UUIDv7）。
2. **Chroma 隔离**：**物理隔离**——每用户 / 每项目独立 Collection。
3. **`site_deploy` 保留为 CRITICAL**：本地生成产物 → 部署时上传 COS 云存储（**单一生产桶**）。
4. **删除分三层**：软删除（进回收站，可恢复）→ 回收站二次确认 → **真删除**（DB + 文件 + COS + Chroma 全清，异步 job）。
5. **`mem_store` 默认走 Memory Gate**：值得存才存，判定记入 `memory_storage_log`。

## 已确认的 6 项边角（来自用户 2026-07-31 第二批）
1. **回收站永久保留**：`recycle_bin.expires_at` = NULL，不自动过期，永远等你手动二次确认永久删。
2. **项目 purge 级联清内容，但保留统计系统**：真删只删"内容表"（projects/conversations/messages/tasks/tool_calls/sir_snapshots/session_audits/agent_runs/feedback + 本地文件 + COS + Chroma）；**统计/聚合表一律保留**（见 §3.5，无 FK 指向内容表，purge 不触碰）。
3. **COS 单一生产桶**：预览产物只写**服务器本地目录**（nginx 静态托管），不上 COS；仅 `site_deploy` 上传 COS **生产桶**（1 个）。去掉"预览桶"。
4. **回收站仅收纳项目**：会话不单独进回收站；项目 purge 级联删全部会话（含其消息/任务/审计）。**真删除后不可恢复**（回收站内恢复仅限把状态从 trashed 改回 draft）。
5. **Memory Gate 5 强触发信号**（按优先级，命中即 store；否则 skip）：
   ① **User Pin**（最强）——用户显式"记住 / 收藏"；② **Self-Correction**——Agent 犯错→被纠正→修正成功（错误+正确一并存，标记 correction）；③ **Repeated Pattern**——同事实/错误在 ≥2 个 Task 出现；④ **Decision with Rationale**——非显然选择 + 给出理由；⑤ **Post-Success Summary**——Task 结束且 `success=true`（默认产出总结）。
6. **全局 KB 严格只读**：`kb_design`/`kb_intent`/`rag_corpus` 平台共享、**禁止租户写入**（写入用管理员通道/迁移脚本）。`cache_generate` 用**输入哈希隔离**（`content_hash` 键 + TTL），避免租户相互污染缓存。

---

## 0. 统一术语与命名规范（全局唯一真相源）

| 术语 | 定义 |
|---|---|
| **Tool** | 原子能力，单一动作（一次外部系统/文件读或写）。MCP 标准 schema + 风险分级。**无业务策略**。 |
| **Skill** | 可组合"菜谱"：用策略/护栏/重试把 ≥1 个 Tool 编排成业务流。一个意图路由到**一个** Skill。 |
| **Intent** | 分类后的用户目标（l1/l2 + 可选 l3）。只做"路由"，不做执行。 |
| **Task** | Skill 运行期内的执行单元（Plan DAG 节点 / ReAct 单步）。有状态机。 |
| **SIR** | Structured Interaction Record，对话状态快照（= 旧 DST + SIR 合并后的唯一状态对象）。DST 即对其做纯函数合并。 |
| **Turn** | 一次"用户→助手"交互单元，全程审计。 |
| **Stage** | 十阶段流水线的单个阶段（S0~S9）。 |

**命名硬规则**
1. Tool / Skill 名：`snake_case`，仅 `[a-z0-9_]`，长度 ≤ 48，**全局唯一**。
2. 领域前缀：

| 前缀 | 领域 | 示例 |
|---|---|---|
| `web_` | 外部互联网 | `web_search` `web_fetch` |
| `rag_` | 向量检索 | `rag_query` |
| `mem_` | 记忆读写 | `mem_recall` `mem_store` |
| `img_` | 图像生成 | `img_generate` |
| `fs_` | 本地产物文件 | `fs_read` `fs_write` |
| `site_` | 网站产物 | `site_publish` `site_delete` `site_deploy` |
| `project_` | 项目生命周期 | `project_recycle` `project_purge` |
| `html_` | 校验 | `html_validate` |
| `browser_` | 无头浏览器 | `browser_capture` |

3. 表名/集合名/Redis key：**小写 + 下划线**，用业务域命名。
4. 枚举值用 `VARCHAR + CHECK`，固定 lowercase 英文，**禁止中文枚举值入库**。
5. 时间统一 UTC，`created_at`/`updated_at` 默认 `CURRENT_TIMESTAMP`。

### 0.1 代码结构命名规范（第四步落地约定 · 新架构概念 ↔ 代码目录）

> 本文件是设计文档（step1/step2/step3 为阶段书签，**文件名不变**）。但**第四步落地的新代码**必须按新架构重命名目录/文件，不再迁就旧 `app/agent/{core,intent,skills,tools,roles}` 结构。以下为落地命名契约。

**旧 → 新 关键重命名**

| 旧路径 | 新路径 | 对应新架构概念 |
| --- | --- | --- |
| `app/agent/core/` | `app/core/` | 运行时核心：`turn_context.py`(TurnContext 唯一真相源) / `pipeline.py`(编排器) |
| `app/agent/intent/*` | `app/router/` | 三级漏斗 `l1_rules.py`/`l2_recall.py`/`l3_llm.py`/`l4_gate.py`/`splitter.py` + `intent_catalog.json`(IntentSchema 单一真相源) |
| `app/agent/skills/*` | `app/skills/`(8 目录) | 8 Skill，与 l1 严格 1:1 |
| `app/agent/tools/*` | `app/tools/`(15 文件) | 15 Tool，风险注册于 `_registry.py` |
| `app/agent/roles/*` | 并入 `app/agent/planner.py` 或删除 | 角色重构已在 v2.2.0 落地，本次重构不再单列 role 包，角色逻辑并入 Planner/ReAct |
| `app/proxy.py` | `app/core/stream_broker.py` | SSE 解耦（原 proxy 演化） |
| `app/metrics.py` + `app/analytics.py` | `app/stats/`(每表一模块) | 统计系统（§3.5）：`ledger.py`/`qc.py`/`flow_checks.py`/`events.py`/`output_guard.py`/`degradations.py`/`intent_decisions.py`/`model_calls.py` |
| `app/config.py` | `app/config/`(yaml 三件套) | `models.yaml`(模型档位绑定) / `router.yaml`(门控 θ) / `quota.yaml`(多租户额度) + `settings.py`(yaml→pydantic) |
| `intent_catalog.json`(散落) | `app/router/intent_catalog.json` | IntentSchema 单一真相源，S4/S5/L1/L4 全读它 |

**目标目录树（第四步落地骨架）**
```
backend/app/
├── main.py                 # 单进程入口: 挂载 /api /vendor /chat(SSE)
├── config/                 # models.yaml / router.yaml / quota.yaml / settings.py
├── core/                   # 运行时核心
│   ├── turn_context.py     # TurnContext 唯一真相源
│   ├── pipeline.py         # Pipeline 编排器
│   ├── stream_broker.py    # SSE 解耦 (原 proxy)
│   └── stages/             # 十阶段 S0–S9 (文件名带 S 编号对齐文档)
│       ├── s0_gateway.py  s1_understand.py  s2_classify.py  s3_merge.py
│       ├── s4_route.py     s5_validate.py    s6_execute.py   s7_assemble.py
│       ├── s8_guard.py     s9_persist.py
├── router/                 # 三级漏斗 + 单一真相源
│   ├── l1_rules.py  l2_recall.py  l3_llm.py  l4_gate.py  splitter.py
│   └── intent_catalog.json
├── skills/                 # 8 Skill (每目录: SKILL.md+skill.yaml+policy.py+run.py)
│   ├── _registry.py  site_build/  site_design/  site_review/  doc_write/
│   └── req_clarify/  web_research/  general_chat/  project_manage/
├── tools/                  # 15 Tool (每文件一 Tool, _registry.py 风险注册)
│   └── _registry.py  web_search.py  web_fetch.py  rag_query.py  img_generate.py
│       fs_write.py  fs_read.py  site_publish.py  site_deploy.py  site_delete.py
│       html_validate.py  browser_capture.py  mem_store.py  mem_recall.py
│       project_recycle.py  project_purge.py
├── agent/                  # 执行层
│   ├── planner.py          # Plan-and-Execute + 失控护栏 (§6.2.1)
│   ├── react_loop.py       # ReAct 单步
│   └── memory_gate.py      # Memory Gate 五信号判定
├── memory/                 # 记忆 / SIR 状态
│   ├── sir.py              # SIR 合并(纯函数) + Chroma 物理隔离
│   └── store.py            # mem_store/recall 落库 (memory_storage_log)
├── hitl/                   # 人工干预 (§1.6 六类)
│   └── interrupts.py
├── stats/                  # 统计系统 (§3.5, 含新增 degradations/intent_decisions/model_calls)
├── models/                 # DB ORM: content.py / stats.py / tenant.py
├── db/                     # session.py / reset_all.py (适配全部表+Chroma+单COS)
│   └── repositories/       # ★ Repository 层: 每 MySQL 表一文件, 写该表 CRUD + 表特有查询 (§0.2)
│       ├── _base.py        # BaseRepo 通用原语 get/list/insert/update/soft_delete/hard_delete
│       ├── users.py  projects.py  conversations.py  messages.py  tasks.py
│       ├── tool_calls.py  sir_snapshots.py  session_audits.py  agent_runs.py
│       ├── feedback.py  memory_storage_log.py  recycle_bin.py  purge_jobs.py
│       └── vector_collections.py  user_model_keys.py  paused_turns.py
├── auth/                   # jwt.py / byok.py (user_model_keys 信封加密)
└── services/               # 跨切面: quota.py (配额检查) / cleanup.py (回收站/真删job)
```

**文件命名硬规则（补充）**
1. **Stage 文件**：`s{N}_{semantic}.py`（`s0_gateway.py`…`s9_persist.py`）——S 编号对齐文档 S0–S9，便于全局 grep「s4」即定位路由阶段。
2. **Skill 目录**：`app/skills/<skill_name>/` 内含 `SKILL.md`(概述) + `skill.yaml`(声明：触发/l2/工具子集/风险) + `policy.py`(护栏) + `run.py`(执行)。渐进披露四层。
3. **Tool 文件**：`app/tools/<tool_name>.py`，`@register_tool` 装饰器写入 `_registry.py`，`ToolSpec.risk` 强制一致校验。
4. **统计模块**：`app/stats/<topic>.py` 单表单模块，导出 `record_*(...)` 与 `aggregate_daily(...)`。
5. **配置**：`*.yaml` 仅放声明值，`settings.py` 负责加载+校验+默认值；禁止在代码里硬编码 θ/cost/tier。
6. **Repository 文件**：`app/db/repositories/<table>.py`，每文件一 class `<Table>Repo(BaseRepo)`，导出该表 CRUD + 表特有查询方法；**禁止在 repo 里写跨表事务**（见 §0.2）。

---

## 0.2 数据访问层（Repository）组织规范

> 用户提议：「每个 MySQL 表单独一个文件，里面写操作它的方法」。**采纳**——这是经典 Repository（DAO）模式，DB 访问集中、可测、避免散落 raw SQL。本章把这条提议钉成落地契约，并划清与 `models/`(ORM 定义)、`services/`(跨表事务)、`stats/`(聚合写入) 的分工，避免第四步落地时概念打架。

### 0.2.1 四层分工（一张表谁负责）

| 层 | 路径 | 负责 | 不负责 |
|---|---|---|---|
| **ORM 定义** | `app/models/{content,stats,tenant}.py` | 表的 SQLAlchemy declarative class（列/类型/索引/关系），**纯结构** | 不写业务逻辑、不写查询 |
| **Repository** | `app/db/repositories/<table>.py` | 该表的 CRUD + 表特有查询（如 `projects.by_user_and_status()`、`messages.append_content_path()`） | 不写跨表事务、不写 DDL |
| **Service** | `app/services/*.py` | 跨表事务 / 编排（purge 级联清多表、建会话+建项目、配额扣减+落账、回收站恢复） | 不重复写单表 CRUD（调 repo） |
| **Stats 聚合** | `app/stats/<topic>.py` | 统计明细表的**聚合写入**与 `aggregate_daily`；明细表本身仍由 repository 提供 insert | 不负责业务表 |

### 0.2.2 Repository 文件结构（模板）

```python
# app/db/repositories/projects.py
from app.db.repositories._base import BaseRepo
from app.models.content import Project
from app.db.session import db

class ProjectRepo(BaseRepo[Project]):
    model = Project
    # —— 通用 CRUD 继承自 BaseRepo: get / list / insert / update / soft_delete / hard_delete ——
    # —— 表特有查询（仅涉及 projects 单表）——
    def by_user_and_status(self, user_id: int, status: str) -> list[Project]: ...
    def active_version(self, project_id: int) -> int | None: ...
    def soft_delete(self, project_id: int) -> None:   # 覆盖基类: 置 status=trashed
        ...
```

### 0.2.3 三道铁律（避免反模式）

1. **DDL 不进 repo**：建表/改表/索引语句只在 `db/reset_all.py`（开发期全量重置）与 future migration 脚本；repo 只消费 ORM class。`reset_all.py` 适配 step1 全部表 + Chroma 物理隔离 + 单 COS。
2. **跨表事务不进 repo**：任何需要 `BEGIN` 多表写（项目 purge 级联清 `projects/conversations/messages/tasks/...`、auto_start 建项目+会话、`usage_ledger`+配额扣减）一律上提到 `services/`；repo 方法保持单表、可独立调用、可被 service 在事务里组合。
3. **统计明细表的 repo 也在 `repositories/`**：`metrics_events`/`frontend_events`/`model_calls`/`intent_decisions`/`degradations`/`tool_calls`(审计副本) 的 insert 由各 `<table>.py` 提供；但**聚合 ETL / `aggregate_daily`** 在 `app/stats/`，二者不冲突（repo 管"写一行"，stats 管"算一天"）。

### 0.2.4 与 §3.7 数据分层策略的衔接

- Repository 负责 **W0 同步硬红线表**（`messages`/`projects`/`conversations`/`recycle_bin`/`purge_jobs`/`intent_decisions`/`degradations`/`user_model_keys`/`paused_turns`）与 R0/R1 直读表的**持久化原语**——这些是"丢了会破坏对话/计费/合规"的表，必须同步写、repo 直落 MySQL。
- W1 后台静默（`session_audits`/`agent_runs`/`memory_storage_log`+Chroma/`feedback`/`qc_scores`/`flow_checks`/`output_guard_log`）与 W2 队列批存（`metrics_events`/`frontend_events`/`model_calls`/`tool_calls`/`usage_ledger`副本）的**最终落库也走各自 repo**，只是调用方是后台 worker / persist_worker，而非主流程直调——repo 不关心谁调，只管"怎么存一行"。

> 一句话：**models 定义结构、repo 管单表怎么读写、service 管多表怎么一起改、stats 管怎么汇总。** 用户"每表一文件"的直觉完全正确，只是要把"跨表"和"DDL"这两个它不该管的踢出去。

---

## 1. Tools 重新设计

### 1.1 Tool 元模型（单一注册结构）
对齐 MCP `Tool` 规范，补充分级与管控字段：
```python
@dataclass(frozen=True)
class ToolSpec:
    name: str; title: str; description: str   # 含 When-to-use / When-NOT-to-use
    input_schema: dict; output_schema: dict
    risk: RiskTier            # LOW | MID | HIGH | CRITICAL
    namespace: str
    read_only: bool; idempotent: bool; open_world: bool
    requires_approval: bool   # = (risk == HIGH)
    handler: Callable
```

### 1.2 风险分级与运行时管控（核心铁律）
| 级别 | 含义 | 举例 | 运行时策略 |
|---|---|---|---|
| **LOW** | 只读、无副作用 | `web_search` `rag_query` `html_validate` `fs_read` `mem_recall` `project_recycle(action=restore)` | 自动执行，仅审计 |
| **MID** | 有副作用但**可逆/用户域内** | `img_generate` `fs_write` `site_publish`(本地预览目录) `browser_capture` `mem_store`(经 Gate) `project_recycle(action=trash)` | 自动 + 审计 + 幂等键 |
| **HIGH** | 域内不可逆 | `site_delete`（删项目内产物） | **Approval Gate**：挂起写 `ai:gate:approval:{req_id}`，前端确认/拒绝后再执行 |
| **CRITICAL** | 系统级 / 跨域 / 上生产 | `project_purge`（永久删全量） `site_deploy`（上生产 COS） | **默认拒绝**；仅管理员白名单 + 双确认放行 |

> 注册时强制校验 `requires_approval == (risk==HIGH)`；不一致启动即报错。
> **模型可见性**：Tool 全局注册 15 个，但**按 Skill 子集暴露**——LLM 在某 Skill 上下文只看到该 Skill 声明的 7–9 个 tool，故不触发 MCP「>15 工具选择退化」问题。

### 1.3 Tool 全清单（15 个）
| # | name | risk | 职责（一句话） | 关键入参 | 关键出参 |
|---|---|---|---|---|---|
| 1 | `web_search` | LOW | 联网搜索返回 Top-K 摘要 | `query, max_results` | `{results:[{title,url,snippet}]}` |
| 2 | `web_fetch` | LOW | 抓取 URL 正文（清洗） | `url` | `{title, text, lang}` |
| 3 | `rag_query` | LOW | 查向量库（按归属解析集合组）| `query, scope, top_k, filters` | `{hits:[{id,score,payload}]}` |
| 4 | `html_validate` | LOW | 单文件 HTML 静态校验 | `html` | `{errors, warnings}` |
| 5 | `fs_read` | LOW | 读本地产物文件 | `path` | `{content}` |
| 6 | `mem_recall` | LOW | 召回用户/项目记忆 | `query, scope` | `{memories}` |
| 7 | `img_generate` | MID | 文生图 | `prompt, size, count` | `{urls}` |
| 8 | `fs_write` | MID | 写产物文件（原子写+版本目录）| `path, content` | `{path, version}` |
| 9 | `site_publish` | MID | 写**本地预览目录**（nginx 静态托管，本地已生成）| `project_id, html` | `{preview_path, preview_url}` |
| 10 | `browser_capture` | MID | 无头浏览器截图（响应式校验）| `url, viewport` | `{screenshot_b64}` |
| 11 | `mem_store` | MID | **经 Memory Gate 判定**后写记忆 | `scope, kind, content` | `{stored_id \| skipped}` |
| 12 | `site_delete` | **HIGH** | 删项目内指定产物/页面（需审批）| `project_id, target` | `{deleted:[...]}` |
| 13 | `project_recycle` | MID | 项目进/出回收站（软删/恢复）| `project_id, action:trash\|restore` | `{status}` |
| 14 | `project_purge` | **CRITICAL** | **二次确认后**永久删项目（DB+文件+COS+Chroma，异步）| `project_id` | `{job_id}` |
| 15 | `site_deploy` | **CRITICAL** | 部署到 COS **生产桶**（白名单+双确认）| `project_id, artifact` | `{production_url}` |

> 旧 `agent_delete` 拆为 `site_delete`(HIGH) + `project_recycle`(MID,可恢复) + `project_purge`(CRITICAL,真删)；`cos_upload` 拆为 `site_publish`(本地预览) + `site_deploy`(COS 生产桶)。预览产物走本地目录（nginx 托管），不占 COS。

### 1.4 Tool 描述写作规范
三段式：`做啥 / Use when / Do NOT use when（指名该用哪个兄弟 tool）`。

### 1.5 Tool 错误契约与幂等规范（运行级细节）

**① 统一返回结构**（所有 Tool handler 返回，不抛裸异常）：
```python
@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict = field(default_factory=dict)     # 成功时输出(匹配 output_schema)
    error: str | None = None                      # 失败原因(不含原始堆栈, 已脱敏)
    error_code: str | None = None                 # tool_busy|bad_arg|not_found|external_5xx|risk_blocked|timeout
    retryable: bool = False                       # 是否可重试
    idempotency_key: str | None = None            # 命中幂等缓存时回填
```
- Skill 层**只判 `ok`**：`ok=False` 时按 `error_code` 决定降级/重试/终止；`retryable` 引导 Skill 有限次重试（默认 ≤3，指数退避）。
- 外部调用失败**绝不回传原始异常文本**（防注入泄露内部拓扑）；记录原始堆栈到 `app/logs/` + `metrics_events(type=tool_error)`。

**② 幂等键（MID/HIGH 必填 `idempotent_key`）**
- 组成：`ai:tool:idek:{trace_id}:{tool_name}:{sha256(args_json)}`（step1 §5.1 已有 key 模板，TTL 600s）。
- 语义：同 `trace_id` 下重复调用（如网络重发、SSE 续跑重放）直接返回首次 `ToolResult`，不二次副作用。
- 校验：注册时 `MID/HIGH` 的 Tool **必须**在 handler 内 `idempotency_key=compute(...)` 并传入计算结果；缺失启动即报错（与 `requires_approval` 同款启动自检）。

**③ 风险阻断返回**
- HIGH 未审批 → 不执行，返回 `ok=False, error_code='risk_blocked'`，并把 `ai:gate:approval:{req_id}` 推到 ctx 等待前端。
- CRITICAL 非白名单 → 返回 `ok=False, error_code='risk_blocked'`，不写入任何 job。

**④ 资源写入原子的"版本目录"约定**（防并发覆盖）
- `fs_write` / `site_publish` 一律写 `previews/{project_id}/{version}/...`，`version` 由调用方单调自增（取自 `projects.config.active_version`）；**覆盖旧版本前先保留 `v{n-1}`**（快照留 1 份），不支持原地覆盖同名文件。
- `fs_write` 落地用"临时文件 + rename 原子替换"，失败留 `.part` 可被 purge 扫掉。

---

## 2. Skills 重新设计

### 2.1 目录结构（对齐 Anthropic/OpenAI Skills 规范）
```
skills/
  site_build/   SKILL.md  policy.py  references/
  site_design/  site_review/  doc_write/  req_clarify/  web_research/  general_chat/  project_manage/
```
`SKILL.md` 头部统一 schema：
```yaml
name: site_build
title: 网站端到端生成
description: |
  从需求/设计稿生成可预览多页网站。
  Use when: 用户要"建站/生成页面/出 Demo"。
  Do NOT use when: 只问设计(用 site_design)；只审查(用 site_review)。
version: 1
intent: [{l1: build_site, l2: from_scratch}, {l1: build_site, l2: from_design}]
trigger_keywords: ["建站","网站","网页","生成页面","landing"]
risk_ceiling: HIGH          # 允许调度的最高 tool 风险
max_steps: 12
mode: adaptive              # adaptive | plan | react
```

### 2.2 Skill 全清单（8 个）
| # | skill_id | 职责 | 调度 Tool | 模式 |
|---|---|---|---|---|
| 1 | `site_build` | 端到端建站(Plan+Reflexion) | rag_query, mem_recall, img_generate, fs_write, html_validate, site_publish, browser_capture | adaptive |
| 2 | `site_design` | 设计顾问 | rag_query, mem_recall, img_generate | react |
| 3 | `site_review` | 代码审查+修复 | fs_read, html_validate, browser_capture, fs_write（site_delete 仅人工确认后）| plan |
| 4 | `doc_write` | 文档生成(Markdown) | rag_query, mem_recall, fs_write | react |
| 5 | `req_clarify` | 需求澄清+偏好抽取 | mem_store, mem_recall, rag_query | react |
| 6 | `web_research` | 联网调研 | web_search, web_fetch, rag_query, mem_store(摘要) | react |
| 7 | `general_chat` | 通用解释/闲聊 | rag_query, mem_recall(轻) | react |
| 8 | `project_manage` | 项目生命周期：进回收站/恢复/永久清理/部署 | project_recycle, project_purge, site_deploy | plan |

> `agent_build`+`agent_generate_site` 合并为 `site_build`；旧 `agent_chat` 的 Intent 切换耦合**上移路由层**；CRITICAL 工具（`project_purge`/`site_deploy`）默认任何 Skill 不可调度，仅 `project_manage` 显式提权。

### 2.3 Skill ↔ Tool 约束
- Skill 只能调注册表 Tool，禁止裸调外部 API（副作用全经 Tool 管控）。
- `risk_ceiling` 强制：`site_delete`(HIGH) 必走 Approval Gate；`project_purge`/`site_deploy`(CRITICAL) 仅 `project_manage` 可调度且需白名单。
- 每 Skill 运行起步写 `agent_runs`，结束写 token/耗时。

---

## 3. 数据库重新设计（MySQL 8，全新库，旧数据一律 reset）

> **数据库 schema 名：`seed_ai`**。所有表建在 `seed_ai` 下（`CREATE DATABASE seed_ai DEFAULT CHARSET=utf8mb4`），连接串 `.../seed_ai`。跨系统引用 ID 在 Chroma/Redis/COS 中以字符串拼接（如 `p_123_design`、`ai:session:456`），无需分布式 ID。所有外键同类型。

### 3.1 核心业务表
```sql
users (
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  email VARCHAR(255) UNIQUE, display_name VARCHAR(120),
  status ENUM('active','disabled') DEFAULT 'active',
  preferences JSON NULL,
  created_at, updated_at TIMESTAMP
)

projects (
  id BIGINT UNSIGNED PK AUTO_INCREMENT, user_id BIGINT UNSIGNED FK→users(id),
  name VARCHAR(255) NOT NULL,
  status ENUM('draft','building','ready','trashed','archived') DEFAULT 'draft', -- trashed=在回收站(软删)
  config JSON NULL, requirement_doc JSON NULL,
  trashed_at TIMESTAMP NULL,        -- 进入回收站时间(=软删)
  expires_at TIMESTAMP NULL,        -- 回收站可选自动清理(默认NULL=永久保留待二次确认)
  deleted_at TIMESTAMP NULL,        -- 硬删时间(非NULL=已永久删除)
  created_at, updated_at TIMESTAMP,
  KEY idx_user (user_id, created_at DESC)
)

conversations (
  id BIGINT UNSIGNED PK AUTO_INCREMENT, project_id BIGINT UNSIGNED FK, user_id BIGINT UNSIGNED FK,
  title VARCHAR(255),
  mode ENUM('chat','build','design','review','doc') DEFAULT 'chat',
  status ENUM('active','archived','trashed','deleted') DEFAULT 'active',
  metadata JSON NULL,
  trashed_at TIMESTAMP NULL, deleted_at TIMESTAMP NULL,
  created_at, updated_at TIMESTAMP,
  KEY idx_project (project_id, created_at DESC),
  KEY idx_user_status (user_id, status, updated_at DESC)
)

messages (
  id BIGINT UNSIGNED PK AUTO_INCREMENT, conversation_id BIGINT UNSIGNED FK, project_id BIGINT UNSIGNED FK,
  turn_no INT NOT NULL,
  role ENUM('user','assistant','system','tool') NOT NULL,
  content LONGTEXT, content_summary VARCHAR(512) NULL,   -- 长内容存摘要,不塞整段工具结果
  content_path JSON NULL,             -- 本轮生成的文件引用数组(与正文区分),见下
  model VARCHAR(64) NULL, token_input INT DEFAULT 0, token_output INT DEFAULT 0, latency_ms INT DEFAULT 0,
  sir_snapshot JSON NULL,           -- 该轮结束后 SIR 全量快照(回滚点)
  created_at TIMESTAMP,
  KEY idx_conv (conversation_id, created_at),
  KEY idx_conv_turn (conversation_id, turn_no)
)
```
`content_path` 结构（JSON 数组，存本轮 Tool 落地的产物引用，状态机与 `tasks.status` 对齐）：
```json
[
  {
    "path": "previews/123/v2/index.html",      // 相对本地根/对象键
    "uri": "https://cdn/.../index.html",        // 可访问地址(预览/生产)
    "kind": "html|image|css|js|doc|asset",      // 类型
    "source_tool": "site_publish|fs_write|img_generate|site_deploy",
    "status": "pending|ready|failed|deleted",   // 与 tasks.status 对齐
    "version": "v2",                            // 版本目录
    "size_bytes": 12345,
    "created_at": 1785432100
  }
]
```
> 作用：正文 `content` 描述"做了什么"，`content_path` 描述"产出了哪些文件、在哪、什么状态"。回收站/永久删除时，可据此精确回收 `path` 对应的物理资源（本地文件 / COS 对象），无需反查正文。
```

#### 3.1.1 全局枚举字典与默认值约定（实现层关闭歧义）

> 避免第四步写 SQLAlchemy/DDL 时字段取值"各写各的"。以下枚举为**全表唯一合法值集**，新代码不得引入未列值；任何状态机迁移不得跨越未声明路径。

| 表.字段 | 枚举值（精确字符串/整型字面） | 默认 | 说明 / 迁移约束 |
| --- | --- | --- | --- |
| `projects.status` | `draft` \| `active` \| `trashed` \| `purging` \| `deleted` | `draft` | `draft→active` 任意；`active→trashed`（回收站，可 `trashed→active` 恢复）；`trashed→purging`（二次确认后异步真删）；`purging→deleted`（job 完成不可逆）。**禁止** `draft/active` 直跳 `deleted` |
| `projects.config.active_version` | 整型（≥1） | `1` | `fs_write`/`site_publish` 写入 `previews/{pid}/v{n}` 的 `n`；每次"发布新版本"单调 +1，不支持回退写入旧版本号（旧版本保留为快照） |
| `conversations.status` | `active` \| `archived` \| `trashed` | `active` | 会话软删随项目 `trashed` 联动进回收站，不独立 `purging` |
| `messages.role` | `user` \| `assistant` \| `system` \| `tool` | —（NOT NULL，必填） | `tool` = 工具调用结构化回显（写入 `content_path` 与 IO 摘要，不入对话正文流但存库） |
| `messages.content_path[].kind` | `html` \| `css` \| `js` \| `md` \| `image` \| `json` \| `other` | — | 文件类型标签，决定预览渲染方式 |
| `messages.content_path[].source_tool` | 见 §1.3 Tool 全清单名（`fs_write`/`img_generate`/…） | — | 溯源：哪个工具产出 |
| `messages.content_path[].status` | `active` \| `deleted` \| `pending` | `active` | `deleted`=已回收/被覆盖；`pending`=暂存（如 cancel 中断产物），可恢复或扫清理 |
| `messages.metrics` JSON | 见 §3.5 `qc_scores` 6 维键（relevance/completeness/accuracy/safety/efficiency/experience + overall） | `{}` | 落库即聚合记录，非空时复盘引用 |
| `tasks.status` | `pending` \| `running` \| `done` \| `failed` \| `cancelled` | `pending` | `pending→running→done/failed`；`running→cancelled`（用户中断，已完成子 Task 保 `done`） |
| `tasks.source` | `planner` \| `replanner` \| `user_split` \| `default` | `default` | 谁产出的这个 Task（Plan-and-Execute / 否定后重排 / 用户中途拆分） |
| `agent_runs.status` | `running` \| `completed` \| `failed` \| `aborted` | `running` | `aborted`=用户/超时中止 |
| `trash_items.restore_status` | `pending` \| `restored` \| `purged` | `pending` | 回收站条目处理态 |
| `user_model_keys.status` | `active` \| `disabled` \| `invalid` | `active` | `invalid`=写入探针校验失败（§6.2） |
| `vector_collections.status` | `ready` \| `building` \| `archived` \| `dropped` | `ready` | `building`=首次灌库；`archived`=冷归档（§4.5） |

- **NULL 约定**：状态/枚举字段一律 `NOT NULL + DEFAULT`；软删标记用 `deleted_at TIMESTAMP NULL`（NULL=未删）；统计表（`qc_scores`/`flow_checks`/`metrics_daily`/`usage_ledger`/`output_guard_log`）**无软删列**，记录永久保留（除非人工审计清洗）。
- **迁移闭合**：以上枚举与 step2 §2.2 `IntentSchema` 的 `risk_level`(`low|mid|high|critical`)、step3 §2 `ai:gate:approval:{req_id}`(`pending|approved|rejected`) 共同构成系统级状态机；任何新表字段若要引入状态，必须登记到此字典并标注迁移路径，作为第四步实现与 `lint_intents.py` 之外的第二项启动自检（`db_enum_check`）。

### 3.2 执行与审计表
```sql
tool_calls (            -- 每次工具调用(审计+成本)
  id BIGINT UNSIGNED PK AUTO_INCREMENT, message_id BIGINT UNSIGNED FK, conversation_id BIGINT UNSIGNED FK,
  tool_name VARCHAR(48) NOT NULL,
  args JSON NOT NULL DEFAULT '{}', result JSON NULL, result_summary VARCHAR(512) NULL,
  risk_level ENUM('low','mid','high','critical') NOT NULL,
  status ENUM('pending','success','error') DEFAULT 'pending',
  duration_ms INT DEFAULT 0, created_at TIMESTAMP,
  KEY idx_msg, KEY idx_tool_time (tool_name, created_at DESC), KEY idx_conv (conversation_id, created_at)
)

tasks (                 -- 执行单元(Plan DAG / ReAct 步; 自引用表达依赖)
  id BIGINT UNSIGNED PK AUTO_INCREMENT, conversation_id BIGINT UNSIGNED FK,
  parent_task_id BIGINT UNSIGNED NULL FK→tasks(id),
  intent VARCHAR(64) NULL, title VARCHAR(255),
  kind ENUM('plan','react') NOT NULL,
  status ENUM('pending','running','done','failed','blocked') DEFAULT 'pending',
  deps JSON NULL, priority TINYINT DEFAULT 5, payload JSON NULL,
  created_at, updated_at TIMESTAMP,
  KEY idx_conv (conversation_id, status), KEY idx_parent (parent_task_id)
)

sir_snapshots (         -- DST 快照链(逐轮保留,可回滚,铁律)
  id BIGINT UNSIGNED PK AUTO_INCREMENT, conversation_id BIGINT UNSIGNED FK, user_id BIGINT UNSIGNED FK,
  turn_no INT NOT NULL, snapshot JSON NOT NULL,
  prev_snapshot_id BIGINT UNSIGNED NULL FK→sir_snapshots(id),
  created_at TIMESTAMP,
  KEY idx_conv_turn (conversation_id, turn_no DESC)
)
-- Redis 热路径 ai:sir:snap:{cid}(LIST LTRIM 10); MySQL 为持久真相源,重启以之为准

session_audits (        -- 阶段审计(PHASE 9)
  id BIGINT UNSIGNED PK AUTO_INCREMENT, conversation_id BIGINT UNSIGNED FK, turn_no INT NOT NULL,
  stage VARCHAR(32) NOT NULL,           -- S0..S9
  event JSON NOT NULL,                  -- {start,end,tokens,cost,status,error?}
  created_at TIMESTAMP,
  KEY idx_conv_turn (conversation_id, turn_no, stage)
)

agent_runs (            -- Skill 运行观测
  id BIGINT UNSIGNED PK AUTO_INCREMENT, conversation_id BIGINT UNSIGNED FK,
  skill_id VARCHAR(48) NOT NULL, stage VARCHAR(32) NULL,
  status ENUM('running','done','failed') DEFAULT 'running',
  token_input, token_output INT, cost_usd DECIMAL(10,6),
  started_at, ended_at TIMESTAMP NULL,
  KEY idx_conv (conversation_id, started_at DESC)
)

memory_storage_log (    -- Memory Gate 存储判定日志(PHASE 7)
  id BIGINT UNSIGNED PK AUTO_INCREMENT, user_id BIGINT UNSIGNED FK, project_id BIGINT UNSIGNED NULL,
  collection VARCHAR(64) NOT NULL, doc_id VARCHAR(120) NULL,
  decision ENUM('store','skip') NOT NULL, reason VARCHAR(255) NULL,
  created_at TIMESTAMP, KEY idx_user (user_id, created_at DESC)
)

feedback ( id BIGINT UNSIGNED PK AUTO_INCREMENT, conversation_id BIGINT UNSIGNED FK, message_id BIGINT UNSIGNED FK,
  user_id BIGINT UNSIGNED FK, rating TINYINT, comment VARCHAR(512) NULL, created_at TIMESTAMP )

usage_ledger ( id BIGINT UNSIGNED PK AUTO_INCREMENT, user_id BIGINT UNSIGNED FK, conversation_id BIGINT UNSIGNED NULL,
  model VARCHAR(64), input_tokens, output_tokens INT, cost_usd DECIMAL(10,6), created_at TIMESTAMP,
  KEY idx_user_time (user_id, created_at DESC) )
```

### 3.3 回收站与永久清理（决策 4 + 边角 1/2/4）
```sql
recycle_bin (           -- 回收站: 仅收纳「软删的项目」, 永久保留(边角1: expires_at=NULL 不自动过期)
  id BIGINT UNSIGNED PK AUTO_INCREMENT, user_id BIGINT UNSIGNED FK→users(id),
  resource_type ENUM('project') NOT NULL DEFAULT 'project',   -- 边角4: 只收项目, 会话不进回收站
  resource_id BIGINT UNSIGNED NOT NULL,
  original_name VARCHAR(255) NULL,        -- 前端展示用
  trashed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP NULL,              -- 边角1: 默认NULL=永久保留, 等你手动二次确认
  purge_state ENUM('pending','purging','purged','restored') DEFAULT 'pending',
  created_at TIMESTAMP,
  KEY idx_user (user_id, trashed_at DESC), KEY idx_resource (resource_type, resource_id)
)

purge_jobs (            -- 永久删除异步任务(跨 DB/文件/COS/Chroma)
  id BIGINT UNSIGNED PK AUTO_INCREMENT, user_id BIGINT UNSIGNED FK,
  resource_type ENUM('project') NOT NULL DEFAULT 'project',
  resource_id BIGINT UNSIGNED NOT NULL,
  status ENUM('queued','running','done','failed') DEFAULT 'queued',
  progress JSON NULL,                     -- {db:done, files:done, cos:done, chroma:done}
  error VARCHAR(512) NULL,
  created_at, started_at, finished_at TIMESTAMP NULL,
  KEY idx_user, KEY idx_status
)
```

**删除三态流程**
```
用户"删除项目"
  → project_recycle(action=trash)         [MID]
      projects.status='trashed', trashed_at=now, 写 recycle_bin(pending). (会话不进回收站, 仍可读/用直到 purge)
  → （回收站 UI 展示, 可 project_recycle(action=restore) 恢复 → status='draft', 删 recycle_bin 行。边角4: 仅状态恢复, 真删除后不可恢复）

回收站"二次确认永久删除"
  → project_purge                          [CRITICAL, 白名单+双确认]
      → 建 purge_jobs(running)
      → 顺序清理(边角2: 只删内容表+物理资源, 统计表一律保留):
         1) DB 内容表: projects + 其下 conversations/messages/tasks/tool_calls/sir_snapshots/session_audits/agent_runs/feedback (级联)
         2) 本地文件: 项目预览目录 (site_publish 产出) + 工程文件
         3) COS: 生产桶下该项目对象 (仅 site_deploy 上传过的; 预览在本地不在COS)
         4) Chroma: delete_collection p_{pid}_design / p_{pid}_code / p_{pid}_memory
      → recycle_bin.purge_state='purged', purge_jobs='done'
      ※ 统计/聚合表 §3.5 全程不触碰
```
> 永久删除**异步**：前端轮询 `purge_jobs` 进度；失败可重试（各步幂等）。`project_purge` 只建 job 并立即返回 `job_id`，不阻塞请求
<arg_key:6124c78e>replace_all</arg_key:6124c78e>
<arg_value:6124c78e>false

---

### 3.4 Memory Gate 决策逻辑（影响 `mem_store` / `memory_storage_log`）
`mem_store` 默认 `skip`；仅命中下列 **5 强触发信号**（按优先级，命中即 `store`）才写入，并记 `memory_storage_log`：
1. **User Pin**（最强）：用户显式"记住 / 收藏 / 记下来"。
2. **Self-Correction**：Agent 犯错 → 被纠正 → 修正成功（存"错误+正确"一对，标 `correction=true`）。
3. **Repeated Pattern**：同一事实 / 错误在 ≥2 个 Task 出现（跨 `tasks` 计数触发）。
4. **Decision with Rationale**：非显然选择 + 给出理由。
5. **Post-Success Summary**：Task 结束且 `success=true`（默认存产出总结）。

> 过程性中间结果、纯噪音、未达上述信号的 → `skip`。`memory_storage_log.decision` 必填 `store|skip`，`reason` 写触发了哪条信号。

### 3.5 统计系统表（永久保留，不参与 purge）
> **边角 2 铁律**：项目永久删除**绝不破坏统计系统**。以下表为「统计/聚合」视角，仅引用 `user_id`（必要时 `project_id` 仅作维度列、**无外键**），不依赖任何内容表；`purge_jobs` 不删除它们。覆盖：调用/成本/延迟、会话结果 6 维打分（relevance/completeness/accuracy/safety/efficiency/experience，**用户最终拍板的 6 维口径，不沿用老系统 7 维 `scoring.py`**）、`message` 流程复查（含日志翻阅）、前端自定义事件、S8 安全判定、**意图路由决策（供门控校准）、降级记录（产品决策核心）、模型调用性能（跨 purge 留存）**。

```sql
metrics_daily (         -- 按日/用户/模型/维度的聚合统计
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,          -- 无 FK, 仅维度
  stat_date DATE NOT NULL,
  model VARCHAR(64) NULL,
  dimension VARCHAR(48) NULL,                -- skill|tool|intent|stage|endpoint|session
  dimension_key VARCHAR(64) NULL,
  calls INT DEFAULT 0, success INT DEFAULT 0,
  token_input BIGINT DEFAULT 0, token_output BIGINT DEFAULT 0,
  cost_usd DECIMAL(12,6) DEFAULT 0,
  latency_p50_ms INT, latency_p90_ms INT, latency_p99_ms INT,
  -- 会话结果 6 维打分聚合(见 qc_scores 表, 此处存日均值 0~100; 用户拍板口径: relevance/completeness/accuracy/safety/efficiency/experience)
  score_relevance_avg    DECIMAL(5,2),  -- 相关性(是否切题/满足真实需求)
  score_completeness_avg DECIMAL(5,2),  -- 完整性(是否做全了)
  score_accuracy_avg     DECIMAL(5,2),  -- 准确性(内容/代码是否正确无误)
  score_safety_avg       DECIMAL(5,2),  -- 安全性(安全/合规)
  score_efficiency_avg   DECIMAL(5,2),  -- 效率(资源/性能)
  score_experience_avg   DECIMAL(5,2),  -- 体验(可读性/易用性/用户体验)
  score_overall_avg      DECIMAL(5,2),  -- 综合(派生)
  -- 业务 KPI (来自内容表离线聚合, 与统计域无 FK 冲突)
  projects_created   INT DEFAULT 0,     -- 当日新建项目数
  sites_deployed     INT DEFAULT 0,     -- 当日 site_deploy 次数
  deploy_success     INT DEFAULT 0,     -- 部署成功数
  avg_turns_per_project DECIMAL(6,2) DEFAULT 0,  -- 单项目平均轮次
  multi_intent_rate DECIMAL(5,4) DEFAULT 0,     -- 多意图占比 (0~1)
  partial_failure_rate DECIMAL(5,4) DEFAULT 0,  -- DAG 部分失败占比
  -- 路由质量 (供 §1.5 auto_calibrate 校准)
  misroute_rate      DECIMAL(5,4) DEFAULT 0,    -- 被 HITL 纠正率
  l4_soft_confirm_rate DECIMAL(5,4) DEFAULT 0, -- L4 软确认占比
  fallback_rate      DECIMAL(5,4) DEFAULT 0,    -- 落 chat 兜底占比
  -- 降级 (§9.2, 产品决策核心)
  degradation_count  INT DEFAULT 0,             -- 当日降级发生次数
  degradation_accepted_rate DECIMAL(5,4) DEFAULT 0, -- 用户见到告知后继续占比
  -- 安全/合规 (S8)
  guard_blocked      INT DEFAULT 0,             -- 拦截数
  guard_warned       INT DEFAULT 0,             -- 告警数
  undisclosed_mock_rate DECIMAL(5,4) DEFAULT 0,-- 漏告知 Mock 占比(应为0)
  -- 满意度 / 前端
  csat_avg           DECIMAL(3,2) DEFAULT NULL, -- 平均满意度 (1~5, 人工/自动)
  nps                INT DEFAULT NULL,          -- 净推荐值
  avg_page_load_ms   INT DEFAULT NULL,          -- 产物页平均加载耗时
  sse_reconnect_avg  DECIMAL(5,2) DEFAULT NULL,-- 平均重连次数
  created_at TIMESTAMP,
  UNIQUE KEY uq (stat_date, user_id, model, dimension, dimension_key)
)

metrics_events (        -- 细粒度事件流(埋点原始), 供离线聚合
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  event_type VARCHAR(48) NOT NULL,           -- tool_call|stage|agent_run|feedback|ratelimit|intent_classify|rag_hit|output_guard|frontend_*
  event_json JSON NOT NULL,
  occurred_at TIMESTAMP,
  KEY idx_user_time (user_id, occurred_at DESC), KEY idx_type (event_type, occurred_at)
)

qc_scores (             -- 会话结果 6 维打分(逐会话/消息), 永久保留, 仅 user_id 维度无 FK
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  project_id BIGINT UNSIGNED NULL,           -- 仅维度, 无 FK
  conversation_id BIGINT UNSIGNED NULL,      -- 仅维度, 无 FK(即使会话被清, 评分保留)
  message_id BIGINT UNSIGNED NULL,           -- 仅维度, 无 FK
  dimension VARCHAR(24) NOT NULL,            -- relevance|completeness|accuracy|safety|efficiency|experience|overall（6 基维 + overall 派生）
  score TINYINT NOT NULL,                    -- 0~100
  model_used VARCHAR(64) NULL,               -- 打分模型(如 intent_lite-judge)
  rationale VARCHAR(512) NULL,               -- 判分理由
  auto BOOLEAN DEFAULT TRUE,                 -- true=自动评, false=人工复核
  created_at TIMESTAMP,
  KEY idx_user_dim (user_id, dimension, created_at DESC),
  KEY idx_conv (conversation_id, created_at DESC)
)

flow_checks (           -- 每次 message 流程复查(可翻阅日志 log 读取), 永久保留, 仅维度无 FK
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  conversation_id BIGINT UNSIGNED NULL,
  message_id BIGINT UNSIGNED NULL,
  turn_no INT NULL,
  check_source ENUM('log_review','stage_audit','trace') NOT NULL,  -- 复查数据来源
  stage VARCHAR(32) NULL,                    -- S0..S9 (若源自阶段审计)
  issues JSON NULL,                          -- [{code, severity, stage, message, state_excerpt}]
  state_excerpt JSON NULL,                   -- 内联当时那一小段 SIR 摘录(自包含溯源, A1决策下DST表随purge删,故复查记录需自带最小上下文)
  log_ref VARCHAR(255) NULL,                 -- 关联日志位置(如 app/logs/xxx.log:123)
  passed BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMP,
  KEY idx_user_time (user_id, created_at DESC), KEY idx_msg (message_id)
)

frontend_events (       -- 前端按钮级自定义时间上报 / 页面访问时长等维度, 纯统计无 FK
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  project_id BIGINT UNSIGNED NULL,           -- 仅维度
  event_name VARCHAR(64) NOT NULL,           -- 自定义事件名(如 btn_deploy_click / page_view / modal_open)
  category VARCHAR(32) NULL,                 -- click|view|duration|custom
  page VARCHAR(128) NULL,                     -- 当前页面路由
  element_id VARCHAR(128) NULL,              -- 触发元素 id
  duration_ms INT NULL,                      -- 页面访问时长 / 停留时长
  payload JSON NULL,                         -- 任意自定义维度
  occurred_at TIMESTAMP,
  KEY idx_user_time (user_id, occurred_at DESC), KEY idx_name (event_name, occurred_at)
)

output_guard_log (      -- S8 安全/合规判定明细(用户决策4: 判定结果可记录), 统计域无 FK, purge不删
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,          -- 维度(无 FK)
  conversation_id BIGINT UNSIGNED NULL,
  message_id BIGINT UNSIGNED NULL,
  input_excerpt VARCHAR(255) NULL,           -- 输出草稿摘要(非全文, 防泄露)
  category ENUM('toxic','compliance','unsafe') NOT NULL,  -- 毒性|合规违例|不安全内容
  decision ENUM('allow','rewrite','reject') NOT NULL,     -- 通过|改写后发|拒发
  reason VARCHAR(512) NULL,
  model_used VARCHAR(64) NOT NULL DEFAULT 'intent_lite',  -- 判定模型(固定 intent_lite)
  confidence DECIMAL(5,4) NULL,
  occurred_at TIMESTAMP,
  KEY idx_user_cat (user_id, category, occurred_at DESC),
  KEY idx_decision (decision, occurred_at DESC)
)

degradations (          -- 降级记录 (§9.2, 产品决策核心数据源), 无 FK, 永保
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,          -- 无 FK, 仅维度
  project_id BIGINT UNSIGNED NULL,
  conversation_id BIGINT UNSIGNED NULL,
  message_id BIGINT UNSIGNED NULL,
  feature VARCHAR(64) NOT NULL,              -- 被降级的功能 (login|member|backend|search|payment...)
  tier ENUM('T0','T1','T2','mock','static') NOT NULL,  -- 降级实现档 (§9.2.1)
  intent_l1 VARCHAR(32) NULL,                -- 归属意图
  limitation VARCHAR(512) NULL,              -- 降级限制说明 (会进对话「能力说明」段)
  upgrade_hint VARCHAR(512) NULL,            -- 升级路径 (§9.2.4: 本地数据库版/第三方BaaS/平台托管)
  accepted BOOLEAN NULL,                      -- 用户见到 capability_notice 后是否继续 (NULL=未知/未提示)
  via_event BOOLEAN DEFAULT FALSE,           -- 是否经 SSE capability_notice 事件告知
  created_at TIMESTAMP,
  KEY idx_user (user_id, created_at DESC),
  KEY idx_feature (feature, created_at DESC)
)

intent_decisions (      -- 意图路由决策明细, 供 §1.5 auto_calibrate 校准 θ, 无 FK, 永保
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  conversation_id BIGINT UNSIGNED NULL,
  message_id BIGINT UNSIGNED NULL,
  l1_hits JSON NULL,                        -- [{intent, score}] L1 规则命中 (可空)
  l2_hits JSON NULL,                        -- [{intent, score}] L2 向量召回 top5
  l3_hits JSON NULL,                        -- [{intent, score}] L3 LLM 分类
  chosen_intent VARCHAR(32) NOT NULL,       -- 终判意图 (l1)
  chosen_confidence DECIMAL(5,4) NOT NULL,  -- 终判置信
  gate_stage ENUM('L1','L2','L3','L4') NOT NULL,  -- 最终在哪一极定的
  was_soft_confirm BOOLEAN DEFAULT FALSE,   -- L4 软确认?
  hitl_corrected BOOLEAN DEFAULT FALSE,     -- 后续被 HITL 纠正?
  corrected_to VARCHAR(32) NULL,             -- 纠正成的意图
  is_multi BOOLEAN DEFAULT FALSE,            -- 多意图?
  dag_size TINYINT NULL,                     -- Task DAG 节点数
  occurred_at TIMESTAMP,
  KEY idx_user_time (user_id, occurred_at DESC),
  KEY idx_chosen (chosen_intent, occurred_at DESC)
)

model_calls (           -- 模型调用明细, 无 FK (与 usage_ledger 刻意分开: 跨 purge 留存), 永保
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  model VARCHAR(64) NOT NULL,               -- hy3|qwen|deepseek|intent_lite|intent_lite-judge|embedding
  tier VARCHAR(16) NULL,                    -- free|paid (套餐内/按量)
  stage VARCHAR(16) NULL,                   -- S2|S4|S5|S6|S8|qc...
  purpose VARCHAR(32) NULL,                 -- classify|generate|judge|embed|route
  ttft_ms INT NULL,                         -- time-to-first-token
  latency_ms INT NULL,                       -- 端到端耗时
  prompt_tokens INT DEFAULT 0,
  completion_tokens INT DEFAULT 0,
  cost_usd DECIMAL(12,6) DEFAULT 0,
  error_code VARCHAR(32) NULL,              -- 非 NULL=失败
  occurred_at TIMESTAMP,
  KEY idx_user_time (user_id, occurred_at DESC),
  KEY idx_model (model, occurred_at DESC)
)
```

#### 3.5.1 统计表"是否真要建"评估（用户决策 · 评估结论）

> 原则：`usage_ledger` 是**有 FK 的明细账**（随 purge 级联删，归内容线）；其余统计表分两类——**聚合/质量核心**（必建）+ **事件原始流**（可改为 ETL 或降级）。
> **可不必建表 / 可降级项**：

| 表名              | 评估                                                    | 结论                          |
| --------------- | ----------------------------------------------------- | --------------------------- |
| `metrics_daily` | 离线聚合结果，BI/看板核心                                  | **必建**（预聚合，查询快）              |
| `qc_scores`     | 6 维打分（relevance/completeness/accuracy/safety/efficiency/experience + overall），质量闭环核心；**用户最终拍板口径（非老系统 7 维 `scoring.py`）** | **必建**                        |
| `flow_checks`   | 流程复查/日志溯源，是"统计不被破坏"铁律的承载                       | **必建**                        |
| `output_guard_log` | S8 安全判定明细，合规审计必需                              | **必建**（用户决策 4 点名要可记录）          |
| `frontend_events` | 前端行为埋点，量极大、非核心                                   | **本期可降级**：先用 `metrics_events` 的 `event_type='frontend_*'` 承载；若量爆增再做独立表（预留字段）。**暂缓建表**。 |
| `metrics_events` | 细粒度原始事件流，写入高频；实时看板不直接用它                      | **可降级为"仅写日志 + 离线 ETL"**：`app/logs/metrics_events.jsonl` 落盘，离线任务批灌 `metrics_daily`，不实时建表。但若要在线实时查单事件（如查某次 intent 误判）则**需建**（建议建，成本低、排障快）。 |
| `usage_ledger`  | 有 FK 指向 content，是"明细账"，随 purge 级联删                   | **必建**（内容线一部分，非纯统计）。           |

> **本期最终落地清单（MVP）**：必建 `metrics_daily` + `qc_scores` + `flow_checks` + `output_guard_log` + `usage_ledger`；`frontend_events` 暂缓（合并进 `metrics_events`）；`metrics_events` **建议建**（实时排障用，成本低）。即统计系统最少 4 表 + 1 明细账，前端事件与原始事件流先在事件表里跑，后续按需拆。

#### 3.5.2 本期补充新增的统计表（在 3.5.1 MVP 基础上，2026-07-31 增量）

> 在已冻结的 MVP 上，**新增 3 张表 + `metrics_daily` 扩维**，均属「统计/聚合」视角（无 FK、跨 purge 永保），补齐产品决策与模型治理盲区。

| 表名 | 解决什么 | 结论 |
| --- | --- | --- |
| `degradations` | §9.2 降级策略落地后，**哪些功能被降级、用户是否接受、是否该做真版**——这是产品路线决策的核心数据源 | **必建**（降级必落，否则 `degradations[]` 仅留对话无统计） |
| `intent_decisions` | §1.5 `auto_calibrate` 阈值校准的**唯一数据源**：每轮 L1/L2/L3 候选分 + 终判 + 是否被 HITL 纠正 → 算出每意图误判率反调 θ | **必建**（无它校准只能盲调） |
| `model_calls` | 3 档模型（hy3/qwen/deepseek）+ intent_lite + embedding 的**性能/成本根因**（ttft/latency/error/by stage）。**刻意与 `usage_ledger` 分开**：后者有 FK 随 purge 删，本表无 FK 跨 purge 留存 | **必建**（模型治理必需） |
| `metrics_daily` 扩维 | 在原 **6 维打分**（relevance/completeness/accuracy/safety/efficiency/experience，用户拍板口径，不沿用老系统 7 维）+ 延迟基础上，新增 **业务 KPI**（projects_created/sites_deployed/deploy_success/avg_turns/multi_intent_rate/partial_failure_rate）、**路由质量**（misroute_rate/l4_soft_confirm_rate/fallback_rate）、**降级**（degradation_count/accepted_rate）、**安全**（guard_blocked/warned/undisclosed_mock_rate）、**满意度/前端**（csat_avg/nps/avg_page_load_ms/sse_reconnect_avg） | **随 `metrics_daily` 一并建**（列扩展，非新表） |

> **`model_calls` vs `usage_ledger` 边界（重要）**：`usage_ledger`(§3.2) 是**有 FK 的明细账**，随会话 purge 级联删，记「谁在哪轮花了多少 token/钱」；`model_calls` 是**无 FK 的性能采样**，跨 purge 留存，记「哪个模型/阶段/用途的 ttft/延迟/错误率」。两者互补：对账看 `usage_ledger`，模型健康看 `model_calls`。
> **`intent_decisions` 与 `metrics_events` 边界**：`intent_decisions` 是**结构化逐轮路由记录**（专为校准 θ 设计，列固定）；`metrics_events` 中 `event_type='intent_classify'` 是**原始埋点**（供即席查询）。落库双写：路由阶段先写 `intent_decisions`（结构化），同时发 `metrics_events` 原始事件。
> **降级闭环**：S5 Planner 产出 `degradations[]` → 落 `degradations` 表（feature/tier/limitation/upgrade_hint/accepted）→ `metrics_daily.degradation_count/accepted_rate` 日聚合 → 管理后台「降级热力图」指导是否真做某功能。
> **监控补充**：`metrics_events` 含 `intent_classify`（供 §1.5 auto_calibrate 阈值校准）、`rag_hit`（供 step1 §4.4 KB 命中率）、`output_guard`（S8 拦截统计），是校准与运维的单一数据源。

> **三条统计线并存，均不随 purge 清**：
> - `usage_ledger`（§3.2，有 FK 指向会话）= 明细账，随内容表级联删；
> - `metrics_daily` / `metrics_events` / `intent_decisions` / `model_calls` / `degradations` = 聚合/路由/模型/降级视角，**均无 FK，永保**；
> - `qc_scores` / `flow_checks` / `frontend_events` / `output_guard_log` = 质量打分 / 流程复查 / 前端行为 / S8 安全判定，均**仅 `user_id` 维度 + 可选 `project_/conversation_/message_id` 维度列但无 FK**，故会话/项目被 purge 后这些质量与行为数据仍保留，满足"统计系统不被破坏"铁律。
> **6 维打分方式（用户最终拍板口径，不沿用老系统 7 维 `scoring.py`）**：每次会话/消息结束经 `qc_scores` 落 **7 维（6 基维 + overall 派生）**——`relevance`（相关性，是否切题/满足用户真实需求）/ `completeness`（完整性，是否做全了）/ `accuracy`（准确性，内容/代码是否正确无误）/ `safety`（安全性，安全/合规）/ `efficiency`（效率，资源/性能）/ `experience`（体验，可读性/易用性/用户体验）+ `overall`（综合派生）；`metrics_daily` 每日聚合均值；`safety` 维同时由 `output_guard_log` 回填。完整覆盖"切题没 / 做完没 / 准确没 / 安全没 / 高效没 / 体验好没"。打分可模型自动（auto=true，用 `intent_lite-judge`）或人工复核（auto=false）。**流程复查**：`flow_checks` 可由日志翻阅器（读 `app/logs/*.log`）或阶段审计（`session_audits`）驱动，对每条 message 跑规则检查，命中 issue 即记 `passed=false` + `log_ref`。

### 3.6 表关联一致性检查（FK 全景 & 跨系统引用）
**内容表 FK 链（可级联）**
```
users(1) ──< projects(user_id) ──< conversations(project_id, user_id)
conversations(1) ──< messages(conversation_id, project_id)
messages(1) ──< tool_calls(message_id, conversation_id)
messages(1) ──< feedback(message_id, conversation_id)
conversations(1) ──< tasks(conversation_id)           tasks(1) ──< tasks(parent_task_id) 自引用
conversations(1) ──< sir_snapshots(conversation_id, user_id)
conversations(1) ──< session_audits(conversation_id)
conversations(1) ──< agent_runs(conversation_id)
projects(1) ──< recycle_bin(resource_id, user_id)     [仅当 resource_type='project' 时语义关联]
```
- **关联校验结论**：所有内容表通过 `conversation_id`/`project_id`/`user_id` 两级聚合到 `projects`，再级联到 `users`；`purge_jobs` 仅需沿 `projects → conversations → (messages, tasks, tool_calls, sir_snapshots, session_audits, agent_runs, feedback)` 顺序删，**Fully connected，无孤儿引用**（除统计表故意无 FK）。
- **统计表（故意断开 FK）**：`metrics_* / qc_scores / flow_checks / output_guard_log / usage_ledger` 仅以 `user_id`（+可选维度列）引用，`purge_jobs` 不删它们 → 满足"统计不被破坏"。`frontend_events` 本期暂缓（合并进 `metrics_events.event_type='frontend_*'`，见 §3.5.1）。
- **跨系统引用**：Chroma 集合名 `p_{project_id}_*`/`u_{user_id}_mem`、Redis key `ai:session:{conv_id}`、COS 对象键 `{project_id}/...` 均由整数 ID 拼接，**与主键同类型（BIGINT 字符串化）**，可双向解析。
- **messages.content_path ↔ 物理资源**：`content_path[].path` 精确指向本地/COS 资源 → 回收/永久删除时按 path 回收，无需解析正文。
- **潜在缺口排查**：① `tool_calls.args/result` 不冗余存文件引用（已在 `messages.content_path` 聚合）→ OK；② `feedback` 同时 FKing `conversation_id`+`message_id`，与 `messages` 一致 → OK；③ `sir_snapshots.prev_snapshot_id` 自引用构成回溯链，不依赖 messages → OK（但 A1 决策下该表随 purge 删，溯源改由 `flow_checks.log_ref`+`state_excerpt` 承载，见 §3.5）。
- **第三步增补表（在 `seed_ai` 库，定义见 step3 §5.4/§6.1）**：① `user_model_keys`（BYOK 信封加密 key，FK→users，purge 用户时删）；② `paused_turns`（用户主动 Hold 长存，关联 `conversations`，purge 项目时级联清）。二者均属内容表语义，纳入 purge 链路顺序（user_model_keys 随 user、paused_turns 随 project/conversation 级联）。

### 3.7 数据分层策略：热读取 / 后台静默 / 队列批存（读写分流总纲）

> 前三节只定义了"存哪"（MySQL/Chroma/Redis），本节定义"**怎么读、怎么写最不拖慢主流程**"。所有 Agent 执行走的是 S0–S9 同步流水线，**任何一步打 DB 都不能成为延迟瓶颈**。据此把每个实体分流到三条读通道、三条写通道，并明确哪些必须同步落盘、哪些可放手异步。

#### 3.7.1 读通道：哪些必须走 Redis 热读取（R0）

> 原则：**每轮必读、且读后仍要立刻用**的核心小记录 →  Redis 命中缓存，未命中再回源 MySQL/Chroma 并回填，TTL 内直接读、变更即 bust。其余（历史列表、审计复盘、看板聚合）走 MySQL/Chroma 直读（R1/R2），不占热层。

| 实体（源） | Redis 热 key 模板 | 类型/TTL | 缓存内容 | Bust 触发 | 回源 |
| --- | --- | --- | --- | --- | --- |
| `users` | `ai:user:{uid}` | HASH / 600s | profile、quota_tier、status、preferences 摘要 | 改资料/配额/BYOK 状态变更 | `SELECT … FROM users WHERE id=` |
| `projects` | `ai:project:{pid}` | HASH / 300s | status、config、active_version、requirement_doc 摘要 | `fs_write`/`site_publish`/`project_update`/状态流转 | `projects WHERE id=` |
| `conversations` | `ai:conv:{cid}` | HASH / 600s | mode、status、title、project_id | 建/改名/归档/软删 | `conversations WHERE id=` |
| SIR 当前态 | `ai:sir:{cid}` | STRING(JSON) / 3600s | 当前轮合并后共享槽 | 每轮 S3 合并后重写 | `sir_snapshots` 链（重启以 MySQL 为真相源） |
| DST 快照链 | `ai:sir:snap:{cid}` | LIST(LTRIM 10) / 7200s | 近 10 轮快照 | 每轮结束 push | `sir_snapshots` |
| Task 运行时 | `ai:task:{tid}` | HASH / 86400s | status、payload、进度 | Task 状态变更 | `tasks WHERE id=` |
| Turn 上下文 | `ai:turn:{tid}` | HASH / 3600s | Stage 进度、中间态 | Stage enter/leave 更新 | 内存（仅运行期） |
| 速率/额度 | `ai:ratelimit:user:{uid}:rpm` `ai:ratelimit:user:{uid}:cost_daily` | ZSET/STRING / 60s·86400s | 滑动窗口、日成本 | 每次请求增量 | `usage_ledger`（仅对账用） |
| RAG 结果 | `ai:cache:rag:{hash}` | STRING / 86400s | 向量召回结果 | 集合变更/手动清 | Chroma `query` |
| 生成缓存 | `ai:cache:gen:{hash}` | STRING / 按 content_hash | `cache_generate` 去重结果 | 输入变化（哈希隔离） | 重新生成 |
| 审批工单 | `ai:gate:approval:{rid}` | STRING / 1800s | pending/approved/rejected | HIGH tool 确认/拒绝（Lua） | （无 MySQL 对应，纯 Redis 态） |
| 删除 job 进度 | `ai:purge:{jid}` | HASH / 86400s | db/files/cos/chroma 各步 | 每步完成 | `purge_jobs WHERE id=` |
| 取消/在线/锁 | `ai:cancel:{tid}` `ai:clients:{tid}` `ai:lock:{op_id}` | STRING/SET/STRING / 短 TTL | 中断标志、SSE 客户端数、写锁 | 事件触发 | （运行期态，无持久） |

> **现状缺口修正**：原 §5.1 只缓存了 session/SIR/task/turn，**漏了 `users`/`projects`/`conversations` 三张每轮必读的实体**——每次 Turn 都直打 MySQL。本表补 `ai:user/ai:project/ai:conv` 三个热 key，使 S0 网关、S4 路由、S6 执行全程零主表读（仅回源一次后走缓存）。

#### 3.7.2 写通道 A：后台静默处理（W1，落库不阻塞主流程）

> 原则：**用户不等待其落库结果、丢失也不影响本次交付**的写 → 丢进后台 worker，主流程直接继续。这些写大多异步、可重试、至少一次。

| 实体 | 为何可静默 | 落库时机 | 耐久性备注 |
| --- | --- | --- | --- |
| `session_audits` | 阶段审计，SSE 已实时推前端，落库仅为复盘 | Turn 结束后后台刷 | 可由 `ai:stream:persist` 承载（见 §3.7.3） |
| `agent_runs` | Skill 运行观测，结束态才重要 | run 结束后台写 | 运行期读走 `ai:task` |
| `memory_storage_log` + Chroma `u_{uid}_mem` | Memory Gate 命中才写，低频，用户不等 | Gate 判定后后台 embed+upsert | 失败仅丢一条记忆，可重试 |
| `feedback` | 用户打分，fire-and-forget | 提交即后台写 | 不影响对话 |
| `qc_scores` | 6 维自动打分，LLM judge 慢 | Turn 结束后台跑 judge 再写 | 前端延迟展示，非阻塞 |
| `flow_checks` | 流程复查（读日志），重 | 后台 log-reviewer 批量跑 | 仅审计用 |
| `output_guard_log` | S8 合规判定明细，低频 | 判定后后台写 | 合规审计，至少一次即可 |
| `mem_store`（Chroma 集合写） | 同 `memory_storage_log` 行 | 后台 upsert | — |

#### 3.7.3 写通道 B：队列批存（W2，高频写先入 Redis Stream 再批量落 MySQL）

> 原则：**高频、 append-only、可丢失重放**的原始流 → 先 `XADD ai:stream:persist`，由 `persist_worker` 批量 `INSERT` 落 MySQL，**绝不在主流程里单行写**。这是"单独丢队列一起存储"的落地形态。

| 实体 | 缓冲 Stream | Flush 策略 | 说明 |
| --- | --- | --- | --- |
| `metrics_events` | `ai:stream:persist`(topic=metrics) | 每 2s 或 ≥200 条 | 全量埋点原始流，量最大 |
| `frontend_events` | 同上(topic=fe) | 同上 | 前端遥测，可丢可不重 |
| `model_calls` | 同上(topic=model) | 同上 | 模型性能采样，高频 |
| `tool_calls`（审计） | 同上(topic=tool) | Turn 结束前 drain | 审计/成本溯源，随 Turn 事务边界刷 |
| `usage_ledger`（持久副本） | 同上(topic=ledger) | 每 5s 或 ≥100 条 | **实时额度门由 `ai:ratelimit:cost_daily` 承担**，MySQL 副本仅对账，可滞后 |

> 队列机制：`ai:stream:persist` 为 Redis Stream；`persist_worker` 单消费者（`XREADGROUP` + `XACK`），批量多行 `INSERT`，失败重试并保留 pending。每个 entry 带 `topic` + `payload_json`，worker 按 topic 路由到对应表。`metrics_daily` 等**聚合结果**不在队列里写——它由离线 ETL 从 `metrics_events`/`model_calls` 算好后**一次性写**（批存的批存）。

#### 3.7.4 全局分流矩阵（每个实体的读/写通道一目了然）

> 速查：R0=Redis 热读 / R1=MySQL 直读 / R2=Chroma 直读；W0=同步关键写 / W1=后台静默 / W2=队列批存。`-`=无此向。

| 实体 | 读通道 | 写通道 | 备注 |
| --- | --- | --- | --- |
| `users` | **R0**(ai:user) | W0 | 每请求必读，变更同步 |
| `projects` | **R0**(ai:project) | W0 | 配置/状态变更同步 |
| `conversations` | **R0**(ai:conv) | W0 | 软删/归档同步 |
| `messages` | R1(历史) | **W0** | 用户对话，不能丢，**同步落盘**（不在队列） |
| `content_path`(messages 列) | R1 | W0 | 随 messages |
| `tool_calls` | R1 | **W2** | 审计，队列批存 |
| `tasks` | **R0**(ai:task) | W0→W2 | 运行期读 Redis；终态同步落，审计副本可队列 |
| `sir_snapshots` | R2(回源) | **W1** | MySQL 真相源，后台刷 |
| `session_audits` | R1 | **W1/W2** | 后台或队列 |
| `agent_runs` | R0(运行期) | **W1** | 结束态后台写 |
| `memory_storage_log` | R1 | **W1** | 后台 |
| `feedback` | R1 | **W1** | 后台 |
| `qc_scores` | R1 | **W1** | 后台 judge |
| `flow_checks` | R1 | **W1** | 后台 |
| `output_guard_log` | R1 | **W1** | 后台 |
| `usage_ledger` | R1 | **W2** | 实时门走 Redis，副本队列 |
| `metrics_daily` | R1(看板) | 批(ETL) | 离线聚合一次性写 |
| `metrics_events` | R1 | **W2** | 队列 |
| `frontend_events` | R1 | **W2** | 队列 |
| `intent_decisions` | R1 | **W0** | 校准 θ 必需，Turn 内同步 |
| `model_calls` | R1 | **W2** | 队列 |
| `degradations` | R1 | **W0** | 降级闭环记录，随 Turn 交付 |
| `recycle_bin` | R1 | W0 | 低频，同步 |
| `purge_jobs` | **R0**(ai:purge) | W0 | job 真相源同步，进度热读 |
| `user_model_keys` | R0(可并入 ai:user) | W0 | BYOK，敏感 |
| `paused_turns` | R0(可并入 ai:turn) | W0 | 用户 Hold 态 |
| `mem_store`(Chroma `u_{uid}_mem`) | R2 | **W1** | 后台 upsert |
| `kb_*` / `rag_corpus`(Chroma) | **R2**(+R0 cache) | 管理通道(admin) | 运行期只读，命中走 `ai:cache:rag` |
| `p_{pid}_*`(Chroma 各集合) | **R2** | W1/S5 内 | 生成/检索直读 Chroma |
| `cache_generate`(Chroma+Redis) | **R0**(ai:cache:gen) | W1 | 哈希隔离，命中免生成 |

#### 3.7.5 队列与持久化 worker 实现要点

- **单一写缓冲**：所有 W2 实体统一 `XADD ai:stream:persist`，不搞多个 stream，worker 按 `topic` 分表写入，简化运维。
- **Turn 边界强一致**：`messages`（W0）与 `tool_calls`（W2）虽分通道，但**同一 Turn 的 `tool_calls` 在响应用户前由 worker drain 完**（Turn 结束钩子 `await persist_worker.flush(turn_id)`），保证"用户看到回复时，审计也已落库"，避免审计丢条。
- **至少一次 + 幂等**：Stream 消费用 `XACK`；MySQL 侧对可重放的表（`usage_ledger`/`metrics_events` 等）写时用 `(user_id, occurred_at, 业务指纹)` 唯一约束或 `INSERT … ON DUPLICATE KEY UPDATE`，防 worker 崩溃重投导致的重复。
- **不进队列的硬红线**：`messages`/`projects`/`conversations`/`recycle_bin`/`purge_jobs`/`intent_decisions`/`degradations`/`user_model_keys`/`paused_turns` 一律 **W0 同步**，因为丢失会直接破坏对话、计费、合规校准或安全。
- **purge 安全**：队列里的 W2 记录若指向已被 purge 的项目（如 `metrics_events` 带 `project_id` 维度），写入不报错（维度列无 FK）；`model_calls`/`metrics_daily` 等统计线本就跨 purge 永保，不受影响（呼应 §3.5 铁律）。
- **背压**：Stream 长度超阈值（如 5w）时 `persist_worker` 提高批大小并告警；Redis 内存不足则降级为"直接落 MySQL"（跳过缓冲）保不丢。

#### 3.7.6 写后失败补偿：reconciler（A5 · 对齐老系统 `reconciler.py`）

> §3.7.5 只说"失败重试并保留 pending"，但缺**独立的失败补偿/对账器**。W2 队列批存（`persist_worker`）在 MySQL 临时不可达、唯一键冲突、字段越界时会失败——必须能自愈且不丢事件。

- **失败分级入队**：`persist_worker` 消费 `ai:stream:persist` 失败 → 该 entry 移入 **`ai:stream:error`**（独立 Stream），附 `error_reason` + `retry_count`。
- **reconciler 定时补偿**：每 ~30s 扫 `ai:stream:error`，按 `retry_count` **指数退避重试**（上限 N=5）；成功即移除并 `XACK` 原 pending。超 N 次仍失败 → 原文 payload 写入 `app/logs/persist_failed.jsonl` + 发 `metrics_events(type=persist_error)` 告警，**不阻塞主流程、不丢事件原文**（可人工排查/重放）。
- **崩溃恢复**：服务启动时 `reconciler` 先扫 `ai:stream:error` + `ai:stream:persist` 残留 pending（衔接 §3.7.5 的"至少一次"），避免上次宕机丢事件。
- **与 §8 灾备衔接**：`persist_failed.jsonl` 纳入备份范围，可重放。
- **约束**：reconciler 只做"至少一次"补偿，**不修改业务状态**；幂等靠 §3.7.5 的唯一键 / `ON DUPLICATE KEY UPDATE`（重投不产生重复行）。

#### 3.7.7 队列批存的 Redis 实时统计视图（用户要求：统计系统可查，但不建新表，单独存 Redis）

> 需求：W2 队列批存的事件（`metrics_events`/`model_calls`/`tool_calls`/`usage_ledger` 副本/`frontend_events`）最终落 MySQL，但**实时**想看"队列积压多少 / 最近批了哪些 / 各 topic 吞吐"——**不应为此新建 MySQL 表**。方案：维护**独立的 Redis 统计结构**，统计系统/管理后台实时读它。

- **队列深度（实时）**：`ai:stats:persist:pending` = `XLEN ai:stream:persist`（积压深度）；`ai:stats:persist:error` = `XLEN ai:stream:error`（失败积压，告警用）。
- **topic 计数与心跳**：`ai:stats:events:{topic}:total`（每 `XADD` +1，INCR）；`ai:stats:events:{topic}:last_flush`（worker flush 时更新时间戳）。
- **最近事件流（实时可查，不查 MySQL）**：`ai:stats:events:{topic}:recent` = 定长 Redis List（`LPUSH` + `LTRIM 5000`），存最近 N 条原始事件紧凑摘要（`topic/occurred_at/指纹`）；管理后台"实时事件流"视图直接读它。
- **当日实时计数**：`ai:stats:daily:{date}:{topic}:count`，供"今日已批存 X 条"秒级展示（**权威聚合仍在 MySQL `metrics_daily`**，Redis 仅实时近似）。
- **读取方**：`app/stats/` 实时看板模块**优先读 `ai:stats:*`**（秒级），历史/重分析读 MySQL（权威）。**全程不新增任何 MySQL 表**（满足约束）。
- **与 reconciler 衔接**：reconciler 处理 `ai:stream:error` 时同步更新 `ai:stats:persist:error`，使失败积压在统计视图可见。

---

### 3.8 大表扩容与冷热归档（支撑"后期表很大、随时扩容"）

> 用户问：有些表后期可能很大，支不支持随时扩容？**支持**——靠"在线 DDL + 分区 + 冷热分离 + 预留分片键"四件套，扩容不中断业务。

**会显著增长的表（重点对象）**
- `messages`（单项目可能上万条，含 `content` 与 `content_path` JSON）→ 核心大表。
- `tool_calls`（每次工具调用一行，高频）→ 海量。
- `metrics_events` / `frontend_events` / `model_calls`（事件流）→ 天文级，但属 W2 队列批存。
- `sir_snapshots`（每轮一快照）、`feedback`、`agent_runs`。

**扩容手段（按成本从低到高）**
1. **在线 DDL（无锁）**：加列/加索引统一走 `gh-ost` 或 `pt-osc`，不锁表、业务不中断；严禁 `ALTER TABLE` 直接改大表。
2. **分区表**：`messages` 按 `user_id` 哈希子分区 + `created_at` 月度范围分区；`tool_calls`/`metrics_events` 按 `created_at` 月度 `RANGE` 分区（脚本每月自动 `ADD PARTITION`）；查询带时间/用户条件即只扫相关分区。
3. **冷热分离**：`messages`/`tool_calls` 超过 6 个月的历史行迁到同库 `_archive` 表（或列式存储），热表只留近期；前端"历史记录"页读归档表，主对话流读热表，互不拖累。
4. **统计明细保留期**：`metrics_events`/`frontend_events`/`model_calls` 明细保留 90~180 天后进冷存或删除（聚合结果已在 `metrics_daily` 永保，§3.5），避免明细无限膨胀。
5. **预留分片键**：所有表主键/热点查询均带 `user_id`；真到亿级再上**按 `user_id` 一致性哈希分库分表**，`user_id` 即 shard_key，设计时已对齐（不破坏现有单库形态）。

**纪律**：扩容/归档操作只经迁移脚本 + §8 备份掩护；分区维护窗口由定时任务自动跑，不占人工。

### 3.9 用户画像维护策略（何时、怎么维护）

> 用户问：用户画像如何维护？什么时候维护？**结论：画像 = 实时聚合视图，不养第二份真相；显式设置实时写、隐式行为异步落、语义记忆走 Memory Gate。**

**画像由三部分构成**
1. **显式设置**（权威）：模型档位 / 主题 / BYOK 状态 / 配额 tier，来自 `users` 表字段（§3.1）。
2. **隐式行为画像**（聚合）：常用意图分布、降级接受度、技能使用频率、平均 turns、满意度 `csat`——聚合自 `intent_decisions` / `degradations` / `metrics_daily` / `qc_scores` / `feedback`。
3. **长期语义记忆**：Memory Gate 命中的 `mem_store` 摘要（Chroma），构成"这个用户偏好 X"的语义画像。

**存哪**
- 热层 `ai:user:{uid}`（§5.1，含 `preferences` 摘要，TTL 600s，改即 bust）。
- **不新建大表**：画像不物化第二份，查询时由 ①②③ 实时聚合；仅在需加速时日更一张可选 `user_profile_snapshot`（由 ETL 从统计表算，松散一致）。

**何时维护（时机矩阵）**
| 时机 | 触发 | 动作 | 通道 |
|---|---|---|---|
| 显式设置变更 | 用户改模型/主题/BYOK | 写 `users` + bust `ai:user` | W0 同步 |
| 每轮交互结束 | S9 持久化后 | 增量写 `intent_decisions`/`degradations`/`feedback` | W1 后台 |
| 每日 ETL | 离线任务 | 刷新 `metrics_daily` + 可选 `user_profile_snapshot` | 离线 |
| Memory Gate 命中 | S6 写 `mem_store` | 长期语义记忆进 Chroma `mem_store` | W1 + Chroma |
| 用户销户 | GDPR 删除 | 清 `users` + `mem_store` + 画像快照 | W0 + purge |

**纪律**：画像只读聚合、**不反向写业务表**；BYOK 等敏感只存状态标志不存明文；用户可随时删画像（销户级联清）。

#### 3.9.1 画像变动更新语义（后期有变动，怎么更新）

> 时机矩阵（§3.9）只回答"何时写"，本小节回答"**写的时候是覆盖、追加还是版本化、冲突怎么消**"。三类画像成分的更新语义不同：

| 画像成分 | 存储 | 更新语义 | 冲突/反转处理 |
|---|---|---|---|
| **① 显式设置** | `users` 字段 | **覆盖式**（后值覆盖前值，强一致 W0，热缓存 `ai:user` 同步 bust） | 无历史包袱；如需审计留痕进 `metrics_events`，但画像本身只存"当前值" |
| **② 隐式行为画像** | 统计明细 + `user_profile_snapshot` | **明细追加 + 聚合覆盖**：单事件（`intent_decisions`/`degradations`/`feedback` 一条）追加进明细（不可变事实流）；每日 ETL 从明细**重算并覆盖**快照；近期偏好用滚动窗口（如近 7/30 天），老数据自然滑出 | 不回溯改写历史明细；画像始终可由明细在任意时刻重建（重建即最新） |
| **③ 长期语义记忆** | Chroma `mem_store` | **增量添加 + 反转 supersede**：Gate 命中→新增一条记忆 | 用户**显式反转**偏好（"之前喜欢蓝，现在改红"）→ 触发 Self-Correction 信号 → 定位旧记忆打 `superseded_by` 指向新记忆、旧条降权/软删，避免"又蓝又红"矛盾；用户主动删记忆→硬删 |

**画像整体更新纪律**
- **不回溯改写历史明细**：`intent_decisions`/`degradations`/`metrics_events` 是不可变事实流，画像变动只追加新事实，不 UPDATE 旧行。
- **画像 = 当前快照，可重建**：永远能从明细重算得到最新态，不依赖"上一次画像值"。
- **敏感偏好只存标志**（BYOK 状态），不存推导出的明文偏好。
- **可遗忘**：用户销户 / 主动删记忆即抹除语义部分，画像回到"空"。

---

## 4. Chroma（向量）重新设计 — 物理隔离

### 4.1 集合策略（决策 2：每用户 / 每项目独立 Collection）
**用户级（每用户 1 个，注册时自动建）**
- `u_{user_id}_mem` — 用户跨会话偏好 / episodic 记忆

**项目级（每项目一组，建项目时自动建）**
- `p_{project_id}_design` — 本项目设计模式 / 组件库 / 历史错误
- `p_{project_id}_code` — 本项目生成代码片段（复用）
- `p_{project_id}_memory` — 本项目长期记忆 / 上下文

**全局共享（平台资产，所有租户只读检索，不按租户拆，边角 6：严格只读、禁止租户写入；需在管理系统可视化操作，见 §4.4）**
- `kb_design` — 全局设计模式 / 组件库（仅管理员/迁移脚本可写）
- `kb_intent` — 意图分类样本 / 路由参考（仅管理员/迁移脚本可写）
- `rag_corpus` — 通用 RAG 文档（仅管理员/迁移脚本可写）
- `cache_generate` — 生成缓存（去重加速，**输入哈希隔离**，边角 6）

> **物理隔离收益**：项目永久删除 = `delete_collection("p_{pid}_design"/"_code"/"_memory")`，干净无残留；用户删 = `delete_collection("u_{uid}_mem")`。
> **代价**：Collection 数随用户/项目增长（数百~数千），Chroma 每集合一目录，自托管可接受；监控集合数，冷项目集合可归档。
> **全局 KB 不按租户拆**（平台共享参考，非用户数据，写入走管理员通道，Agent/租户只读）。

### 4.5 集合生命周期与冷归档（物理隔离的运维细节）
- **集合注册表**（非 Chroma 内建，独立 MySQL 表 `vector_collections` 登记，提供归属/创建时间/最后访问）：
  ```sql
  vector_collections (
    id BIGINT UNSIGNED PK AUTO_INCREMENT,
    scope ENUM('user','project','global') NOT NULL,
    owner_id BIGINT UNSIGNED NOT NULL,         -- user_id 或 project_id
    collection VARCHAR(80) NOT NULL,            -- p_123_design / u_456_mem / kb_design
    embedding_model VARCHAR(64) NOT NULL DEFAULT 'text-embedding-v3',  -- 创建即固定(用户决策1)
    dim INT NOT NULL DEFAULT 1024,             -- 与 text-embedding-v3 一致, 创建即固定
    status ENUM('active','archived','deleted') DEFAULT 'active',
    last_accessed_at TIMESTAMP,
    created_at, updated_at TIMESTAMP,
    UNIQUE KEY uq_collection (collection)
  )
  ```
  > 该表**无 FK**（owner 可能已 purge），仅维度登记；提供集合数上限监控（如 >5000 告警）。
- **冷归档策略**：`last_accessed_at` 超过 90 天且非 global → 标记为 `archived`，其向量数据导出至对象存储冷层（或就地保留但移出热索引），释放 Chroma 内存/句柄；用户再次访问时懒加载回 `active`。
- **建集合幂等**：`get_or_create_collection(name, embedding, dim)` 先查 `vector_collections`，已存在且模型一致直接返回，避免重复创建导致维度冲突；模型不一致→报错（禁止混 embedding）。全平台 embedding 锁 **`text-embedding-v3`**（云端，服务器硬件不行不做本地推理），维度默认 **1024**，`config/models.yaml` 可覆写为 768/512 但**集合创建后不可变**。
- **删除联动**：`project_purge` 清 DB 后，由 purge job 在 `vector_collections` 定位 `scope=project & owner_id=pid` 的 3 个集合执行 `delete_collection` 并置 `status='deleted'`；`recycle_bin` 恢复项目则把对应集合 `archived→active`。

---

### 4.3 访问控制与 `cache_generate` 输入哈希隔离（边角 6）
- **全局 KB 读写分离**：`kb_*` / `rag_corpus` 通过独立管理员客户端（特殊 API key）写入，运行时 `rag_query` 仅 `get`/`query`，**不暴露 add/update 接口给 Skill/Tool 链**（代码中以 `read_only_client` 单例访问，普通工具拿不到写句柄）。
- **`cache_generate` 输入哈希隔离**：缓存键 = `sha256(normalized_prompt + model + size + seed)`（去除无关节键、统一空白）。写入 metadata 带 `content_hash` + `owner_scope=global`；检索先按 `content_hash` 精确命中（同输入极速返回），未命中才走相似度兜底。**所有租户共享同一缓存集合但键唯一、互不覆盖**；TTL 7 天自动过期。禁止把租户私有内容写入 `cache_generate`（仅缓存"相同或近似输入→相同输出"的确定性生成结果）。

### 4.4 全局 KB 管理系统可视化操作（用户需求：能在管理端可视化运维）
> 全局 KB（`kb_design`/`kb_intent`/`rag_corpus`）**严格只读于 Agent 运行时**，但需在**管理后台（admin）**提供可视化 CRUD，由管理员/运营操作。设计要点：
- **独立管理通道**：后台走 `admin_chroma_client`（高权限 key，区别于运行时 `read_only_client`），权限由 `admin` 角色 Session 校验（双因子/白名单），**绝不**下发给普通 Skill/Tool。
- **管理端能力清单**：
  1. **集合浏览**：列出 4 个全局集合 + 各集合文档数/维度/embedding 模型/创建时间。
  2. **文档增删改查**：按 `doc_id` 查询/编辑 `document`+`metadata`+`content_hash`；支持单条删除、批量导入（Excel/JSON/Markdown）。
  3. **命中率观测**：展示各 KB 近 30 天被 `rag_query` 召回次数（来自 `metrics_events` where event_type='rag_hit' & collection=）、Top 查询词、低命中（无结果）查询告警。
  4. **版本/回滚**：每次编辑写 `kb_change_log`（见下），可一键回滚到历史版本。
  5. **人工质检**：对 KB 文档打 `needs_review` 标记，运营复核后清除。
- **审计表**（归属统计域，无 FK、purge 不删）：
```sql
kb_change_log (         -- 全局 KB 变更审计(管理系统可视化操作留痕)
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  collection VARCHAR(64) NOT NULL,           -- kb_design|kb_intent|rag_corpus
  doc_id VARCHAR(120) NULL,                  -- 新增为NULL, 编辑/删除填原id
  action ENUM('create','update','delete','rollback') NOT NULL,
  actor_user_id BIGINT UNSIGNED NOT NULL,    -- 操作管理员(无FK, 仅维度)
  before_json JSON NULL, after_json JSON NULL,
  reason VARCHAR(255) NULL,
  created_at TIMESTAMP,
  KEY idx_collection (collection, created_at DESC), KEY idx_actor (actor_user_id, created_at DESC)
)
```
- **与运行时隔离**：管理端写 → `kb_change_log` 留痕 → 向量生效；运行时 `rag_query` 只读不写、拿不到 `admin_chroma_client`。即"可视化操作"与"Agent 只读"不冲突。

### 4.2 统一 Metadata Schema（集合内仍保留过滤键）
```
kind       STRING  -- preference|episode|component|error|code|memory
domain     STRING  -- build|design|review|doc...
success    INT     -- 0/1(仅 code/error)
created_at INT     -- unix 秒(永不混 ISO)
lang       STRING  -- zh|en
version    STRING
visibility STRING  -- public|internal|private
```
全局集合额外加 `source STRING`。
**检索**：`rag_query` 按 `(user_id, project_id, scope)` 解析**应查集合组**后并发查、按 score 归并；先 metadata 过滤再相似度；`n_results` 5–20；code 域启用 hybrid（dense+lexical）重排。每个集合**创建即固定 embedding 模型 + 维度**写入集合 metadata，禁止混模型。

---

### 4.6 向量过期与删除策略（哪些会过期、怎么删）

> 用户问：向量库过期数据有没有删除，如何删除？**答：分四类，只有缓存类自动过期，其余按事件联动删，全局 KB 永不删。**

| 类别 | 集合/域 | 是否过期 | 删除机制 |
|---|---|---|---|
| **生成缓存** | `cache_generate` | ✅ 自动过期（TTL，如 24h~7d，输入哈希隔离） | 写入带 `expire_at` metadata；后台 `chroma_gc` 每小时扫 metadata<now 批量 `delete`；命中复用、未命中重算 |
| **长期记忆** | `mem_store` | ❌ 不过期 | 仅三种情况删：① 用户主动删记忆 ② 项目 `purge` 联动 ③ 用户销户。由 Memory Gate 五信号保证"只存值得存" |
| **项目集合** | `components/error_patterns/intents/user_preferences/project_memory/project_code`（scope=project） | ❌ 随项目生命 | `project_purge` 清 DB 后由 purge job 定位 `vector_collections` 执行 `delete_collection`（§4.5） |
| **全局 KB** | `kb_design`/`kb_intent`/`rag_corpus` | ❌ 永不删 | 严格只读（边角 6）；更新走管理员通道全量重灌，不清不自动过期 |

**删除安全纪律**
- 物理删除前先 `vector_collections.status='deleted'`（软标记），异步执行；误删靠 §8 备份（Chroma 集合导出）恢复。
- `chroma_gc` 与 purge job 均**幂等**（按 `(collection, metadata 指纹)` 去重），崩溃重跑不重复删。
- 监控：集合数、各集合向量数、过期待删队列长度，汇入 `metrics_daily`。

### 4.7 代码 / 文档切片策略（RAG 喂库与代码分析，含"改代码怎么切"）

> 用户问：修改代码时怎么进行代码切片？**答：不整文件重切——按 diff 定位受影响函数/段落，只对这些 chunk 增量重算 + upsert，未变 chunk 不动。** 这是 RAG 索引与 `review_code`/`rag_query` 一致性的关键。

**切片单元（语言感知）**
- **结构化代码**（py/ts/js/java/go/...）：**AST/语法级切片**——以函数/类/方法为 chunk 单元，保留签名 + docstring + body；跨文件调用靠 symbol 元数据关联。
- **文档/标记**（md/html/css/json/sql）：**语义段落切片**——按标题层级（`#`/`##`）或固定 token 窗口（512~1024 token）+ 重叠（128 token）滑窗。
- **超大单文件**（>阈值 KB）：先按函数切，函数内仍超 embedding 上限再按行块切，保证单 chunk ≤ 模型上限（留余量）。

**chunk 元数据（写入 Chroma metadata，供 §4.2 过滤）**
`file_path, language, symbol, kind(func|class|method|doc|section), start_line, end_line, project_id, content_hash, chunk_index, total_chunks`

**增量切片（核心：改代码时怎么办）**
1. **首次全量**：`slice_and_index(path)` 切全文件，逐 chunk `upsert`。
2. **后续增量**：传 `diff`（git diff / 行区间）→ 定位受影响的函数/段落 → **只对这些 chunk 重算 embedding + upsert 覆盖**，未变的不动。
3. **chunk id 设计**：`id = hash(file_path + symbol + content_hash)`。内容不变 → id 不变 → 天然幂等 upsert；行号漂移不影响检索（start_line 仅元数据）。
4. **删除的代码块**：diff 显示某 symbol 消失 → 按该 chunk id `delete`。

**embedding 一致性**：同一集合固定模型（§4.2）；切片与检索必须用同模型，否则相似度失真。

---

### 4.8 项目记忆变动更新规则（后期有变动，怎么更新）

> §4.5/§4.6 已覆盖项目集合的"生命周期/冷归档/删除"，但**没说"项目活着、用户后期改需求/改设计/迭代版本时，项目记忆怎么跟着变"**。本小节补这块活更新规则。
> **结论：项目记忆会随项目演进持续更新；按成分分语义更新（需求文档版本化 patch、设计记忆追加+反转 supersede、产物只追加不覆盖、代码 diff 增量），冲突以最新一轮 SIR/requirement_doc 为准。**

**① 项目记忆的范围**（跨轮、跨会话、绑该项目）
1. 需求文档 `projects.requirement_doc`（DB JSON，结构化需求）
2. 设计 / 语义决策（Chroma `p_{pid}_*` 集合的 `project_memory` / `user_preferences` 域）
3. 产物快照指针 `messages.content_path`（DB，带 `version` 标记）
4. 项目代码 / 组件库（Chroma `project_code` / `components`，scope=project）

**② 变动源（哪些操作触发项目记忆变动）**
| 变动源 | 触发阶段 | 受影响成分 |
|---|---|---|
| 需求澄清 / 补充 | S5 clarify 后 | ① requirement_doc |
| 设计方向变更（"改红主题""换布局"） | S6 生成前/中 | ② 设计记忆 |
| 重新生成 / 迭代新版本 | S6 出新版本 | ③ 产物快照 + ① 部分需求固化 |
| 用户否定上轮 + 新指令 | 任意中断（§1.6 / step2 §7） | ② 相关记忆标 `stale` |
| 项目 purge / 恢复 | 回收站操作 | ①②③④ 整体删 / 恢复 |

**③ 更新语义（核心规则）**
- **需求文档（DB）**：**可版本化原地 patch**（JSON Merge Patch 语义，只改变更字段）+ 保留可选 `requirement_doc_history`（记每次变更 diff）以便回溯；强一致 W0，因为后续生成强依赖它。
- **设计 / 语义记忆（Chroma 项目集合）**：类 `mem_store` 但 `scope=project`。新增默认**追加**；同维度**反转**（"主题从蓝改红"）→ 旧条打 `metadata.superseded_by` 指向新条、旧条降权；用户否定 → 打 `metadata.stale=1` 降权不参与检索。
- **产物快照（content_path）**：**只追加不覆盖**。每版本生成新 `content_path` 条目（带 `version` 标记），旧版本保留可回滚（除非 purge）。呼应 step3 §9「产物版本化」与用户"能增删改查"。
- **代码 / 组件（Chroma `project_code`）**：按 §4.7 diff 驱动**增量 upsert**，symbol 级覆盖/删除，chunk id=hash 天然幂等。

**④ 冲突解决**
- **需求 vs 设计记忆矛盾**（需求写"简约"但设计记忆写"炫酷"）→ 以**最新一轮 SIR / `requirement_doc` 为准**，旧记忆降权；S5 校验若检测到矛盾 → 触发澄清，**不静默覆盖**。
- **多会话并发改同一项目** → 最后写入 + 版本号胜出；用 `projects.updated_at` + 乐观锁（`UPDATE ... WHERE updated_at=:old`）防丢失更新。

**⑤ 与 purge / 冷归档的关系**
- 项目记忆随 `project_purge` 整体删（Chroma `delete_collection` + DB 行删）；`recycle_bin` 恢复 → Chroma `archived→active` + DB 软删恢复（§4.5）。
- 冷归档：90 天未访问 → 项目集合 `archived`（§4.5），再次访问懒加载，**记忆不丢**。

---

## 5. Redis（状态）重新设计

### 5.1 命名空间（前缀 `ai:` + 域 + key_type + id）
> 全局统一 `ai:`；**会话类 key 必带 TTL 且活跃 `EXPIRE` 续期**；淘汰策略 **volatile-ttl**（绝不用 allkeys-*）。

| Key 模板 | 类型 | TTL | 用途 |
|---|---|---|---|
| `ai:session:{conv_id}` | HASH | 3600 | 会话工作态(model/user/mode)，活跃续期 |
| `ai:user:{uid}` | HASH | 600 | **用户档案/配额/状态热缓存**（§3.7.1，每请求必读，变更即 bust） |
| `ai:project:{pid}` | HASH | 300 | **项目配置/状态/活跃版本热缓存**（§3.7.1，每 Turn 必读） |
| `ai:conv:{cid}` | HASH | 600 | **会话元数据热缓存**（§3.7.1：mode/status/title） |
| `ai:sir:{conv_id}` | STRING(JSON) | 3600 | SIR 热路径当前态 |
| `ai:sir:snap:{conv_id}` | LIST | 7200 | DST 快照链(LTRIM 10) |
| `ai:turn:{turn_id}` | HASH | 3600 | 单 Turn 上下文/Stage 进度 |
| `ai:task:{task_id}` | HASH | 86400 | Task 运行时状态 |
| `ai:tool:idek:{trace_id}:{name}:{hash}` | STRING | 600 | 工具幂等结果缓存 |
| `ai:cancel:{turn_id}` | STRING | 3600 | 取消标志("1"=中止) |
| `ai:clients:{turn_id}` | SET | 3600 | 在线 SSE 客户端计数 |
| `ai:lock:{op_id}` | STRING | 35 | 步骤执行锁 |
| `ai:ratelimit:user:{uid}:rpm` | ZSET | 60 | 滑动窗口 RPM |
| `ai:ratelimit:user:{uid}:cost_daily` | STRING | 86400 | 每日成本固定窗口 |
| `ai:gate:approval:{req_id}` | STRING | 1800 | HIGH tool 待确认工单 |
| `ai:purge:{job_id}` | HASH | 86400 | 永久删除 job 进度（db/files/cos/chroma 各步）|
| `ai:active_sessions` | ZSET | 3600 | 全局活跃会话(score=ts) |
| `ai:cache:rag:{hash}` | STRING | 86400 | RAG 结果缓存 |
| `ai:cache:gen:{hash}` | STRING | by content_hash | `cache_generate` 生成去重缓存（输入哈希隔离，§3.7.1） |
| `ai:stream:persist` | STREAM | — | **写缓冲队列**（W2 实体：`metrics_events`/`frontend_events`/`model_calls`/`tool_calls`/`usage_ledger` 先入流，由 `persist_worker` 批量落 MySQL，§3.7.3） |
| `ai:stream:error` | STREAM | — | **写后失败补偿队列**（`persist_worker` 失败 entry 移入，由 `reconciler` 退避重试，§3.7.6） |
| `ai:stats:persist:pending` | STRING | — | `XLEN ai:stream:persist`，队列积压深度（实时，§3.7.7） |
| `ai:stats:persist:error` | STRING | — | `XLEN ai:stream:error`，失败积压深度（实时，§3.7.7） |
| `ai:stats:events:{topic}:total` | STRING | — | 各 topic 累计批存条数（INCR，§3.7.7） |
| `ai:stats:events:{topic}:recent` | LIST(定长5000) | — | 最近事件流摘要，管理后台实时事件视图直读（§3.7.7） |
| `ai:stats:daily:{date}:{topic}:count` | STRING | 86400×2 | 当日实时批存计数（§3.7.7） |
| `ai:agent:events` | PUBSUB | — | Agent 协调事件总线 |

### 5.2 关键模式
- **原子并发 / 计数**：Lua `CHECK_AND_TRACK`（查并发→加 ZSET→设过期）。
- **活动续期**：每次请求 `EXPIRE ai:session:{cid} 3600`。
- **取消传播**：SSE 断连 → `SET ai:cancel:{turn_id}=1` → Worker 轮询中止；仅末客户端离开且未正常结束才 cancel。
- **审批门**：HIGH tool → 写 `ai:gate:approval:{req_id}=pending` → SSE 推前端 → `approved` 继续 / `rejected` 取消。
- **清理进度**：`project_purge` 异步 job 每步完成写 `ai:purge:{job_id}`（db/files/cos/chroma 各字段）。

### 5.3 并发与一致性 Lua 契约（关键 key 实现细节）
- **取消传播（C1 升级）**：SSE 断连 **或** 用户主动干预（§1.6）均 `SET ai:cancel:{turn_id}=1 EX 3600`。Worker 在下述 checkpoint 轮询：每个 Task 起步前、每次工具调用前、每步 ReAct Thought 前。命中即抛 `TurnCancelled`（非异常降级），当前 Task 标 `cancelled`，已 `done` 的 Task 保留。
- **审批门（Approval Gate）原子推进**：HIGH tool 触发时原子 `SETNX ai:gate:approval:{req_id} pending`；前端确认/拒绝走 Lua `if GET==pending then SET approved/rejected` 防止重复推进；超时（1800s）未决 → 默认 `rejected`（fail-safe）。
- **并发写锁**：`fs_write`/`site_publish` 对同一 `path` 用 `ai:lock:{op_id}`（TTL 35s，调用方持锁轮询续期 `PEXPIRE`）。拿不到锁即 `ok=False,error_code='tool_busy',retryable=true`，由 Skill 退避重试，防同一文件并发覆盖。
- **幂等读锁（RAG/Search 去重）**：同一 `trace_id` 的 `web_fetch`/`rag_query` 用 `ai:tool:idek:*` 做结果缓存；命中则 Skill 直接读缓存，省一次外部调用。
- **限流**：`ai:ratelimit:user:{uid}:rpm` 用 ZSET 滑动窗口（member=timestamp, 每隔 60s `ZREMRANGEBYSCORE` 过期），超阈返回 429 + `Retry-After`；`cost_daily` 固定窗口累加（防单租户烧穿预算）。

---

## 6. 路由映射速查（Intent → Skill → Tools）
| Intent (l1/l2) | Skill | 关键 Tools |
|---|---|---|
| `build_site/*` | `site_build` | rag_query, img_generate, fs_write, html_validate, site_publish |
| `design_advice/*` | `site_design` | rag_query, img_generate |
| `review_code/*` | `site_review` | fs_read, html_validate, browser_capture, fs_write |
| `doc_generate/*` | `doc_write` | rag_query, fs_write |
| `requirement/*` | `req_clarify` | mem_store, mem_recall, rag_query |
| `web_qa/*` | `web_research` | web_search, web_fetch, rag_query |
| `chat/*` | `general_chat` | rag_query, mem_recall |
| `project/trash` `project/restore` | `project_manage` | project_recycle |
| `project/purge` | `project_manage` | project_purge |
| `project/deploy` | `project_manage` | site_deploy |

> 意图层只"路由到哪个 Skill"，**不决定执行细节**；执行细节全在 Skill 内。

---

## 7. 第一步确认清单（已全部拍板，schema 冻结）

**5 项主决策** ✅ 已采纳
1. 主键 `BIGINT UNSIGNED AUTO_INCREMENT`
2. Chroma 物理隔离（每用户/每项目独立 Collection）
3. `site_deploy` 保留为 CRITICAL（本地生成 → 上传 COS 生产桶）
4. 删除三态（软删进回收站 → 二次确认 → 真删异步 job）
5. `mem_store` 默认走 Memory Gate

**6 项边角** ✅ 已确认
1. 回收站永久保留（不自动过期）
2. 项目 purge 级联清**内容表**，统计系统（§3.5 无 FK 表）一律保留
3. COS **单一生产桶**；预览产物走本地目录（nginx 托管），不上云
4. 回收站仅收项目；真删后不可恢复；恢复回 `draft`
5. Memory Gate 5 强触发信号（User Pin > Self-Correction > Repeated Pattern > Decision with Rationale > Post-Success Summary）
6. 全局 KB 严格只读（管理员通道写）；`cache_generate` 输入哈希隔离（sha256）

**本轮增量补强（用户 11:25 补充）** ✅ 已落地
- **`messages.content_path`**（JSON 数组）：存本轮生成的文件引用（path/uri/kind/source_tool/status/version/size），与正文 `content` 区分；回收/永久删除时按 path 精确回收物理资源。
- **统计系统扩维**（§3.5）：① `metrics_daily` 增 6 维打分日均值（relevance/completeness/accuracy/safety/efficiency/experience + overall）；② 新增 `qc_scores`（会话结果 6 维打分 relevance/completeness/accuracy/safety/efficiency/experience + overall，用户最终拍板口径，非老系统 7 维 `scoring.py`，可自动/人工）；③ 新增 `flow_checks`（每次 message 流程复查，来源含**日志翻阅 log_review**，记 `log_ref`）；④ 新增 `frontend_events`（前端按钮级自定义时间上报 / 页面访问时长 / 停留时长等任意维度，无 FK 纯统计）。
- **表关联一致性检查**（§3.6）：画全 FK 链，确认内容表全连接无孤儿引用；统计表故意断开 FK 以保 purge 后留存；跨系统引用（Chroma/Redis/COS）用整数 ID 字符串化可双向解析。
- **全局 KB 管理系统可视化**（§4.4）：后台 `admin_chroma_client` 提供集合浏览/文档 CRUD/命中率观测/版本回滚/人工质检，并落 `kb_change_log` 审计；与运行时 `read_only_client` 严格隔离。

**决策 A（用户 2026-07-31 已选 A1）**
- `sir_snapshots` **归内容表，随项目 purge 删**（不改为统计域）。DST 历史溯源改由两条路径保障，不依赖 DST 表存活：① `flow_checks.log_ref` 锚定常驻运维日志 `app/logs/*.log`（按日滚动、不在 purge 范围）；② `flow_checks.issues[].state_excerpt` 在落库时**内联当时那一小段 SIR 摘录**，使复查记录自包含（详见 §3.5 / §3.6）。

> ✅ **第一步 Tools / Skills / 数据库 schema 已彻底冻结**（含 A1 在内的全部决策）。下一步进入**第二步**：代码结构落地（registry / skill 加载器 / pipeline stage 骨架 / 迁移脚本 `reset_all.py` 重建 13 内容表 + 5 统计必建表 + Chroma + 单 COS 生产桶客户端 + 全局 KB 管理端）。
