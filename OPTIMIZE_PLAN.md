# SeedAI 优化方案（命名统一 · 意图召回 · 删除体系 · 角色重构）

> 作者：Senior Developer｜日期：2026-07-27
> 范围：基于 2026-07-27 全链路测试（20/20 通过，但暴露 6 类质量短板）+ 用户 5 条新规
> 目标：把"能跑"提升到"命名一致 / 意图精准 / 删除合法 / 流程专业"

---

## 0. 现状诊断（动手前先对齐事实）

| 维度 | 当前事实 | 问题 |
|---|---|---|
| 项目名命名 | `projects` 表列 = `title`；`ProjectResp`/业务 API/前端全用 `name`；靠 `@property name` 适配层桥接 | 双命名并存，违反"变量名统一"铁律，repos 里还有 `name→title` 转换 hack |
| 删除体系 | `agent_delete` **已是**「单 agent + 内部合法校验 + 二次 `confirm` 弹窗」形态；删项目被显式 `block` | 方向对，但"删产物/删页面/删文件/删项目"四类边界、中风险二次弹窗仍不细 |
| 文档/需求路由 | 靠 `intent_catalog.json` 的 examples + 向量 + LLM；关键词权重弱 | 10 号（写 PRD）漏召回、3/6（诗/摘要）不应强行走 doc；13/14/19 多意图漏编排 |
| 角色拆分 | 8 个 skill（chat: chat/search/design/doc；build: requirement/build/review/generate_site），**实际已是角色雏形** | 没有显式的"产品/设计/开发/测试"四角色编排链路与交接物 |

**关键澄清（来自用户本轮）**：
- 3/6 不算 bug（用户没要求生成文档）→ 不要强行路由 doc。
- 10 号"写/文档"是明确关键词 → 必须优化召回，命中 `requirement`。
- 18 号"删除项目"不允许，但"删除项目内产物（html/css/js/文档）"允许 → 走单 agent 内部校验 + 二次确认。

---

## 1. 命名统一：Project 模型 `title` → `name`（用户第 1 点）

**原则**：DB 列、ORM 属性、schema、repos、proxy、前端、管理端全部用 `name`；`Conversation.title` 是**另一张表的独立列，不动**。

### 1.1 改动清单
| 文件 | 改动 |
|---|---|
| `scripts/reset_all.py` | 重置时 `DROP` 旧表重建，`projects` 列从 `title` 改为 `name`，无需迁移旧数据（按用户"重写数据库"） |
| `shared/models.py` | `Project.title` → `Project.name`（列名改）；**删除** `@property name` 适配层 |
| `app/schemas.py` | `ProjectResp.name` 保持；`SearchItemResp` 已返回 `title=p.name`，无需改字段名 |
| `app/repos/business_repos.py` | **删除** `ProjectRepo.create/update` 里的 `name→title` 转换 hack；直接落 `name` |
| `app/projects.py` | `conv_repo.create(title=...)` 保留（会话列就叫 title，属另一表）；搜索处 `p.name` 已是；核对无 `Project.title` 直接引用 |
| `app/proxy.py` / `app/admin.py` | 把残留 `p.title` / `Project.title` 引用改 `p.name`（grep 确认：当前多数为 `conv.title`，仅 search/artifact 少量） |
| 前端 `src/**` | 前端**已普遍用 `p.name`**（ProjectsView/Sidebar/ChatView），仅 `SearchItemResp.title` 是展示字段，保留即可 |
| 测试 `_e2e_harness.py` | 仅 conv 用 `title`，不受影响 |
| `docs/01~04` | 同步更新"项目名字段 = name"的口径 |

### 1.2 验证
- 重置数据 → `desc projects` 仅见 `name` 列，无 `title`。
- 全链路 re-run 5 条建项目 / 改项目名 / 搜索项目用例，确认无 `title` 残留 AttributeError。

---

## 2. 意图召回 & 文档关键词优化（用户第 2 点）

**目标**：10 号必走 `requirement`；诗歌/摘要不强行 doc；13/14/19 多意图稳定编排。

### 2.1 关键词白名单增强（落 `intent_catalog.json` + 级联）
- **需求文档强信号词**：`写一份/写个/出一份/写个.*文档/PRD/需求文档/方案书/规格/功能清单/用户故事` → 即便向量分低，规则直路由 `build_requirement → agent_requirement`，并触发 `requirement_doc` 计量。
- **纯创作不误伤**：诗歌(`写首诗/作诗/七言`)、摘要(`总结/概括/摘要/提炼`) 保留 `chat_casual`，**不**加入 doc 白名单（用户明确 3/6 不算 bug）。
- **多意图并列连词**：`并且/而且/同时/再帮我/还要/另外/顺带/以及/，然后` 作为多意图门控强触发词（当前 13/14/19 漏召的主因）；命中即 `recognize_intents` 拆 A+B。

### 2.2 多意图门控调参（`app/agent/intent/multi_intent.py`）
- 当前"命中≥2 意图大类才进"门槛偏高；改为：**规则层命中任一 `多意图强触发词` 即进混合分层**（不唯大模型是从），提升 13/14/19 召回率。
- 轻量路径（`_segment_text` + 复用 `_classify_segment`）优先，避免每个都升级 LLM 深拆（保性能）。

### 2.3 验证
- 回归 `scripts/multi_intent_regression.py` + 复用 `_e2e_harness` 改写 10/13/14/19/3/6 用例，断言：10→requirement、3/6→chat、13/14/19→orchestration(≥2 sub)。

---

## 3. 删除体系：单 agent + 合法校验 + 二次确认（用户第 3 点）

**现状已符合方向**（`agent_delete`）：单 agent、内部校验、二次 `confirm`。本项做**补全 + 标准化**。

### 3.1 四类删除的合法边界（在 `agent_delete` 内建一张判定表）
| 用户意图 | 判定 | 行为 |
|---|---|---|
| 删除**项目本身** | 含`项目/整个项目/这个站`且无`产物/文件`限定 | **block**（复用现有逻辑，18 号已验证）→ 提示"项目不可删，仅可删内部产物" |
| 删除**全部产物** | `清空/删除所有/全部产物/所有文件` | confirm（高风险）→ 删 `artifacts` 全量 |
| 删除**单个文件** | 含 `index.html / style.css / app.js / 文档.md` 等 | confirm（中风险）→ 删指定文件 |
| 删除**某页面/模块** | `删掉首页/关于页面` 等语义 | confirm → 标记为待办（建站产物按页拆分时精确删，否则降级提示） |

### 3.2 强化项
- **中风险也弹二次确认**：当前仅"全删"弹 confirm，单文件也补 confirm（用户要求"二次弹窗确认"统一）。
- **合法校验前置**：在 `_is_delete_request` 之后立即跑"边界判定"，先确认目标是"产物"还是"项目"，项目直接 block，不产生多余 confirm。
- **与前端 confirm 弹窗对齐**：返回的 `confirm` 事件带 `risk_level`（high/medium）字段，前端按级别渲染不同文案（红/黄）。

### 3.3 不改动
- `DELETE /api/projects/{id}` 仍保留（超管/用户主动删项目走 API + 软删除，非对话触发）。

---

## 4. Agent 角色重构：拆 产品/设计/开发/测试（用户第 4 点 · **已选定方案 B**）

> 用户 2026-07-27 决策：**选 B（拆 4 个独立 Agent）**，明确"整体流程可能需要大改动"。
> 落地粒度采用 **B-轻（同进程内 4 角色上下文隔离 + 交接物协议）**，保住单进程架构（不退回双进程/多服务 IPC）。**✅ 已落地实现（2026-07-27）**，代码见 `backend/app/agent/roles/` 包（`handoff.py`/`base.py`/`product.py`/`design.py`/`dev.py`/`qa.py`/`orchestrator.py`），开关 `ROLE_ORCHESTRATOR_ENABLED`（默认 `"1"`=开，置 `"0"` 走原生 `Orchestrator`/`run_skill` 零破坏回退）。下面为方案全文。

### 4.1 四个 RoleAgent 与现有能力的映射
| 角色 | 合并自 | 输入 | 输出（交接物） | 职责 |
|---|---|---|---|---|
| **ProductAgent（产品）** | `agent_requirement` | 用户需求语义 / 对话摘要 | `PRD`（目标·功能清单·用户故事·约束·验收点） | 理解"要做什么" |
| **DesignAgent（设计）** | `agent_design` + chat 中设计咨询 | `PRD` | `DesignSpec`（风格·布局·色彩·组件·交互规范） | 定义"长什么样" |
| **DevAgent（开发）** | `agent_build` + `agent_generate_site` + `agent_doc` | `DesignSpec`（或纯开发请求） | `CodeArtifact`（html/css/js + 预览 + 文档） | 把设计"做出来" |
| **QAAgent（测试）** | `agent_review` + `scoring.py`(7维) | `CodeArtifact` + `PRD` | `ReviewReport`（评分·缺陷清单·修复建议） | 质量把关、按 PRD 验收 |

> 说明：`agent_search` 归为跨角色支撑工具（产品调研/竞品），`agent_chat` 保留为"非四角色"的闲聊/咨询兜底（诗歌/摘要等不进四角色 SOP）。`agent_delete` 保持独立（删除体系，见第 3 节）。

### 4.2 Orchestrator（调度器，非第 5 角色）
- **单意图**：直派对应 RoleAgent；建站类若缺 `PRD` 则先 `ProductAgent` 再 `DevAgent`（保证有需求基线）。
- **多意图**：`planner.split()` 拆子任务 → 按 SOP 串/并行调度 RoleAgents → `merge` 交接物 → 终态（复用现有 `[5/6]/[6/6]` 多意图编排）。
- **默认 SOP（完整闭环）**：`PRD → DesignSpec → CodeArtifact → ReviewReport`。

### 4.3 "独立"的关键：上下文隔离 + 交接物协议
- 每个 RoleAgent 启动仅注入：① 自身 system prompt（角色边界，禁止越权）② **上游交接物**（而非整段聊天历史）③ 最小必要项目上下文（project_id / site_generated 等）。
- 好处：Dev 不被闲聊污染代码；QA 严格按 `PRD` 验收；四角色可**单独评测 / 单独换模型 / 单独迭代**。
- 交接物 = 强 Schema 的 TypedDict / Pydantic 模型（`PRD`/`DesignSpec`/`CodeArtifact`/`ReviewReport`），落 `app/agent/roles/handoff.py`。

### 4.4 目标骨架（伪代码）
```python
# app/agent/roles/orchestrator.py
async def run(intent, ctx):
    if intent.is_multi:
        subtasks = planner.split(intent)            # 产品拆任务
    pipe = [ProductAgent, DesignAgent, DevAgent, QAAgent]
    artifact = ctx.seed  # 用户原始需求
    for role in pipe:
        artifact = await role.run(prev=artifact)    # 仅拿到上游交接物
    return merge(artifacts) + qa_report
```
```
ProductAgent.run(prev) -> PRD
DesignAgent.run(prev=PRD) -> DesignSpec
DevAgent.run(prev=DesignSpec) -> CodeArtifact   # 内部仍调 build/generate_site 执行体
QAAgent.run(prev=CodeArtifact, prd=PRD) -> ReviewReport
```

### 4.5 与现有 8 skill 的关系（避免重写）
- RoleAgent **不重写**执行逻辑，而是**包装**现有 skill：ProductAgent 内部调用 `agent_requirement` handler，DesignAgent 调 `agent_design`，DevAgent 调 `agent_build`/`agent_generate_site`，QAAgent 调 `agent_review` + `scoring`。
- 即"执行层复用、编排层重写"——符合你"不用兼容但要稳健"的偏好，且风险可控。
- 新增目录：`app/agent/roles/{product,design,dev,qa,orchestrator}.py` + `handoff.py`；原 `skills/` 退化为"被 RoleAgent 调用的能力库"，逐步收敛。

### 4.6 为什么选 B-轻（同进程隔离）而非 B-重（真拆 4 服务）
- 你已定调"单进程合并架构（v2.0.0 起）"为铁律；B-重要拆 OS 进程/加 IPC，等于推翻该约定。
- B-轻用"上下文隔离 + 交接物协议"实现**逻辑独立**（角色看不到彼此闲聊、可单独评测），达到你"拆角色"的体验目标，却不破坏单进程。
- 若未来真要"每角色独立部署/独立计费/独立告警"，再升级为 B-重——交接物协议不变，只是 transport 从 in-process 改成 queue/RPC。

---

## 5. 遗留项汇总 + 优先级

| 项 | 来源 | 优先级 | 是否纳入本方案 | 状态 |
|---|---|---|---|---|
| Project 命名统一 | 用户① | P0 | ✅ 第 1 节 | 已出方案，待开工 |
| 10 号文档召回 + 多意图门控 | 遗留 + 用户② | P0 | ✅ 第 2 节 | 已出方案，待开工 |
| 删除四边界 + 二次确认 | 用户③ | P1 | ✅ 第 3 节 | 已出方案，待开工 |
| 角色重构（B-轻：4 独立 RoleAgent + 交接物） | 用户④ | P1 | ✅ 第 4 节（**已选定 B-轻**） | 已出方案，**本轮不动，单独排期** |
| 日志 `TypeError: not enough arguments for format string` | 遗留常驻 | P2 | 统一 `%s` 占位 | 顺手，非主路径 |
| `Conversation.messages` 序列化 / 统计虚指标 | 已修 | — | — | 已闭环 |

---

## 6. 交付与回归
- 1/2/3 改动：本地 commit + tag（**不 push**，按你约定）。
- 回归：`_e2e_harness.py`（改写 10/13/14/19/3/6 断言）+ `scripts/multi_intent_regression.py` + 重置数据重跑。
- 文档同步 `docs/01~04`。
- 第 4 点（角色重构）单独成轮，开工前再开 TaskList 拆解（ProductAgent→DesignAgent→DevAgent→QAAgent→Orchestrator 5 个实现任务 + 回归）。

---

## 7. 用户已确认决策（2026-07-27）
1. **第 4 点：选方案 B-轻（同进程内 4 个 RoleAgent + 上下文隔离 + 交接物协议）**，不退回双进程/多服务；执行层复用现有 8 skill，仅重写编排层。
2. **执行节奏**：本轮只交付方案文本（本文件），用户确认后再进入实现；第 4 点不在本轮实现，单独排期。

> 注：3/6/10 的判定已按用户澄清——3/6 不强行走 doc（保留 chat），10 号"写/文档"强制走 `requirement` 并接通 `requirement_doc` 计量。18 号"删项目"维持 block，删项目内产物走 `agent_delete` 单 agent + 二次确认（方向已存在，本方案做边界细化）。
