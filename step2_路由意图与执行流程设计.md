# 第二步 · 路由 / 意图分类 / Agent 执行流程 / Pipeline 骨架 重新设计（历史依据）

> **状态：历史依据。自 2026-08-01 起已被《SeedAI全链路重构最终实施规范.md》替代；阶段、路由、SIR、执行与模型策略以最终规范为准。**
>
> 前提：第一步 `step1_工具技能与数据库设计.md` 已冻结（Tool 15 个 / Skill 8 个 / 风险四级 LOW·MID·HIGH·CRITICAL / MySQL 库名 `seed_ai` / Chroma 物理隔离 / 回收站三态）。  
> 本文档所有命名、风险分级、工具/技能引用**必须**与第一步一致，禁止新增未声明的 tool/skill。  
> 阶段命名 1:1 对齐《Agent全链路执行总图》：S0=PHASE0 … S9=PHASE9。

---

## 0. 本步统一命名规范（增量，叠加在 step1 §0 之上）

| 新增术语           | 命名规则                                                                                | 示例                                                                                                |
| -------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| 意图标识 Intent ID | `{l1}` 或 `{l1}/{l2}`，snake_case，全局唯一                                                | `build_site`、`build_site/from_design`、`project/purge`                                             |
| l1（意图域）        | 与 Skill 1:1 对应（8 个），不允许 l1 无对应 Skill                                                | `build_site` `design_advice` `review_code` `doc_generate` `requirement` `web_qa` `chat` `project` |
| l2（子意图）        | 同一 l1 下的细分动作，声明在 IntentSchema                                                       | `from_scratch` `from_design` `from_doc` `trash` `restore` `purge` `deploy`                        |
| Task 状态        | 复用 step1 `tasks.status` 枚举                                                          | `pending` `running` `done` `failed` `blocked`                                                     |
| 阶段 ID          | `S0`..`S9`，同时写入 `session_audits.stage`（step1 §3.2）                                  | `S2` `S6`                                                                                         |
| 风险档            | 复用 step1 四级，全小写                                                                     | `low` `mid` `high` `critical`                                                                     |
| 门 Gate         | 两个门语义明确区分：**Recall Gate**(S1 召回门) / **Storage Gate**(S7 存储门=step1 §3.4 Memory Gate) | —                                                                                                 |

---

## 1. 路由架构（Intent Routing）—— 三级漏斗 + 置信门控

### 1.1 行业共识（近 5 个月）

- **分层级联（Layered Cascade Router）**：规则(≈0ms) → 语义召回(≈5ms) → LLM 分类(≈1 次廉价调用) → 置信门控。层间是**截断(Priority Chain)**&#x5173;系而非加权平均：L1 命中直接返回，L2/L3 不执行。
- **意图互斥**：Intent 之间边界必须清晰，重叠会致 LLM "脑补"。本系统 Intent 由 `intent_catalog` 集中声明）。
- **PII 脱敏在 Ingest 做**：进分类器前先 strip 卡号/邮箱/手机号（轻量正则 + 必要时 Presidio），trace 链绝不带原始 PII。
- **few-shot exemplars** 提升分类 10–30%：从 `kb_intent`（Chroma 全局只读集合）召回 3–5 条最近似标注样本进分类 prompt。

### 1.2 路由四层（对应 S0 入口 → S2/S4）

```
                       ┌─────────────────────────────────────────────┐
  用户原始输入 ───────► │ S0 Ingest: trace_id 生成 / PII 脱敏 / 归一化 │
                       └─────────────────────────────────────────────┘
                                          │
            ┌───────────── L1 规则匹配(<1ms, 0 token) ─────────────┐
            │  仅匹配【系统/强动作指令】(见§1.7) → 零成本直路由    │
            │  业务创作意图命中 → 仅【缩窄候选集】(不阻断下游)      │
            │  未命中/非强指令 ↓                                   │
            ├───────────── L2 语义召回(≈5ms) ─────────────────────┐ │
            │  embed(query) → kb_intent Top-K(只读) + SIR 上一轮意图│ │
            │  + L1 缩窄后的候选集 → K=5 few-shot 候选             │ │
            │  仍低置信 ↓                                           │ │
            ├───────────── L3 LLM 分类(1 次 intent_lite) ──────┐  │ │
            │  结构化输出 {intents:[{l1,l2,conf,is_primary}],  │  │ │
            │   slots_extracted[], reasoning}  ← 多意图裁决者    │  │ │
            └───────────── L4 置信门控 ──────────────────────────┘  │ │
                  conf ≥ θ_high → 直接路由到 Skill                  │ │
                  θ_low ≤ conf < θ_high → 路由但打 flag=low_conf    │ │
                  conf < θ_low → 澄清(clarify)，不路由              │ │
```

> **L1 截断铁律（精炼）**：`step1 §0 命名硬规则` 延伸——纯规则阶段（L1）在进程内**禁止 import LLM client**；L2/L3 才允许 embedding / LLM。这与总图"规则在前、LLM 在后"一致。
> **L1 不阻断业务多意图（关键修订）**：L1 **只认强信号**——系统/强动作指令（如"继续/重置/清空记忆"、"project/purge"）零成本直路由；业务创作意图（build_site 等）即便 L1 命中正则，也**只收窄 L3 候选集**，最终分类 + 多意图检测**永远由 L3 完成**（防旧设计"build_site 命中即截断导致顺带 web_qa 意图被漏判"的隐患）。详见 §1.7。
> **Ingest 只做接入不做理解**：S0 不允许做意图语义理解（总图 PHASE0 明确禁止），理解在 S2。

### 1.3 关键组件与文件落点（骨架，暂不实现）

| 组件                    | 职责                                | 落点（规划）                                                    |
| --------------------- | --------------------------------- | --------------------------------------------------------- |
| `IntentRouter`        | 编排 L1→L2→L3→L4                    | `app/pipeline/stages/s2_understand.py` + `s4_classify.py` |
| `rule_router`         | L1 正则/关键词匹配                       | `app/agent/intent/rule_router.py`                         |
| `semantic_recaller`   | L2 调 `rag_query(scope=kb_intent)` | `app/agent/intent/semantic.py`                            |
| `llm_classifier`      | L3 廉价模型 + few-shot                | `app/agent/intent/classifier.py`                          |
| `confidence_gate`     | L4 阈值决策                           | `app/agent/intent/gate.py`                                |
| `intent_catalog.json` | Intent 集中声明（见 §2）                 | `app/agent/intent/intent_catalog.json`                    |

### 1.4 模型档位与阶段绑定（租户模型策略 · 用户决策 5）

**三类模型：**

| 类别                     | 档位键                                         | 用途                                                              | 谁选             |
| ---------------------- | ------------------------------------------- | --------------------------------------------------------------- | -------------- |
| **固定轻量模型**（平台锁定）       | `intent_lite`                               | 意图判断(S4)、结果判断(S8)、S2/LLM 理解(默认)、嵌入(L2)                          | **平台固定，租户不可选** |
| **任务执行模型**（租户自选 3 选 1） | `exec_standard` / `exec_pro` / `exec_ultra` | 仅 **S6 任务执行**（Skill/工具实际生成、Plan-and-Execute 的 Planner/Executor） | **租户在设置里选其一**  |
| **歧义升档**（稀疏触发）         | `intent_strong`                             | 仅 L3 歧义一次升档（§1.5 ④）                                               | 平台固定           |

**强绑定铁律（用户决策 5）：**

- **S4 意图判断（L3 分类）** → 永远 `intent_lite`，**不随租户选择变化**。
- **S8 结果判断（Output Guard 判定/校验）** → 永远 `intent_lite`（含规则校验 + **`intent_lite` 安全/合规/毒性判定**，见 §1.4.1），绝不用租户模型。
- **S6 任务执行** → 用租户选定的那 1 个执行模型（默认 `exec_standard`），切换立即生效、下一次 S6 应用。
- S2 理解与 DST delta 提取 → `intent_lite`（成本优先，非生成重活）。
- 租户模型仅 3 档（由平台在 `config/models.yaml` 预置），**禁止租户自定义 model 名 / 微调 / 自传权重**。

> **配置落点**：`config/models.yaml` 声明 `intent_lite` / `intent_strong` / `exec_standard` / `exec_pro` / `exec_ultra` 的 `model_id` 与 `base_url`；`users.preferred_exec_model`（step1 §3.1，ENUM `standard|pro|ultra`，默认 `standard`）存租户选择。路由/执行代码**只允许读这 5 个档位键**，禁止硬编码任何具体模型名。  
> **好处**：意图与结果判断与租户付费档彻底解耦——无论租户选哪档，路由准确率、输出安全判定都一致可复现，避免"低价租户判定劣化"；生成质量差异仅体现在 S6。

#### 1.4.1 默认 model_id 推荐映射（用户决策 · 套餐特性）

> 你的环境：**qwen / hy3 走 token-plan 套餐（额度内不额外计费）；deepseek 按量计费（用多少算多少）**。以此为成本最优做默认映射——**高频固定调用尽量吃套餐额度（边际≈0），唯一按量档留给最强推理且前端明示计费**。

| 档位键            | 推荐默认 model_id（可配置）             | 成本特性                               | 选用理由                                   |
| --------------- | -------------------------------- | ---------------------------------- | -------------------------------------- |
| `intent_lite`   | `qwen-turbo`（轻量档）              | token-plan 套餐内，**高频调用边际≈0**         | S4/S8/S2 每次请求必调，用最便宜的套餐内档，不额外花钱          |
| `intent_strong` | `qwen-plus`（强档）                 | token-plan 套餐内                         | 歧义稀疏升档，仍吃套餐额度                            |
| `exec_standard` | `hy3`（默认执行档）                  | token-plan 套餐内                         | 默认执行模型，性价比高，额度内不额外计费                      |
| `exec_pro`      | `qwen`（强执行档）                  | token-plan 套餐内                         | 强规划/复杂生成，仍吃套餐额度                           |
| `exec_ultra`    | `deepseek`（按量）                  | **按量计费，额外花钱**                       | 最强推理档；**前端设置页必须明示"ultra 按量计费"**，租户知情选择 |

- **embedding 模型（L2）** → **`text-embedding-v3`（用户决策 1）**，**云端调用**（服务器硬件不行，不做本地推理）；维度建议 **1024**（最高质量，可降 768/512 省存储）；具体值在 `config/models.yaml` 锁死，**集合创建即固定、禁止混维度**（见 step1 §4.5）。
- **`intent_lite` / `intent_strong` 与嵌入共用 token-plan 额度**，故"规则在前、意图用轻量、嵌入走云端轻模型"整体边际成本极低，符合硬件受限环境。
- 以上为**可配置默认值**，部署时按实际可用套餐在 `config/models.yaml` 覆写；代码只认档位键，不认具体名。

#### 1.4.2 S8 Output Guard 增加 `intent_lite` 安全/合规判定（用户决策 4）

S8 在原"规则校验 + 脱敏 + 格式"基础上，**追加一次 `intent_lite` 判定轮**：

1. **规则层（同步、零成本）**：PII/密钥正则脱敏、HTML/JSON 格式校验、长度/危险标签黑名单（确定性，必跑）。
2. **判定层（`intent_lite`，异步可选）**：把 `output_draft` 摘要（非全文，防泄露）送 `intent_lite`，输出结构化 `{toxic:bool, compliance_violation:bool, unsafe_content:bool, reason}`，三类中任一类命中 → 拦截或降级（按严重度：致命→拒发+记 `output_guard_log`；轻微→改写后发+记）。
3. **结果必落表（用户要求"可记录"）**：每次 S8 判定写 `output_guard_log`（step1 §3.5 新增专表，**统计域、无 FK、purge 不删**），记录 `input_excerpt / category / decision(allow|rewrite|reject) / reason / model_used=intent_lite / confidence`。同时该次安全分回写 `qc_scores.dimension='safety'`（已有维），使"6 维打分"含安全维度闭环。
4. **判定结果可最终查询**：管理系统可按 `category`、`decision`、`user_id`、`date` 拉取拦截统计与样例，支撑安全审计与合规复盘。

---

### 1.5 L4 置信门控细节（落定）

门控对象**始终是 L3 输出的 `intent_list`**；**多意图时每个意图独立过门控**（主/从分别阈值，见下）。

**三区间动作**

| 区间                                 | 动作                            | 后续             |
| ---------------------------------- | ----------------------------- | -------------- |
| `conf ≥ θ_high (0.85)`             | **直路由**：信任结果，进 S5             | 高精零打扰          |
| `θ_low ≤ conf < θ_high (0.5~0.85)` | **软确认**：按主意图执行 + 打 `low_conf` | 记事件供校准；高风险才问一次 |
| `conf < θ_low (0.5)`               | **clarify**：不路由，反问确认          | 回 S0 重进，直到明确   |

**① 软确认规则（避免反复硬问）**

- `low_conf` 仍执行主意图，但置 `ctx.flags.low_conf=True`；S9 落 `metrics_events(type=intent_classify, low_conf=true)` 供 `auto_calibrate` 回收。
- 仅当主意图 `risk_level ≥ high`（如 `build_site`/`project/deploy`）时升级为**一次轻量澄清**："你是要建站对吗？"——低风险直接跑，高风险才问。

**② 主/从意图分别阈值**

- 主意图（is_primary）`θ_high=0.85 / θ_low=0.5`。
- 从意图（is_secondary，如顺带"查竞品"）`θ_secondary=0.7`；低于从阈值的次要意图**静默丢弃**不入 DAG（防噪声 Task）。从意图错了代价小且有主意图上下文兜底。

**③ 歧义升档 `intent_strong` 精确触发**  
非"低置信就升档"（太贵），仅在**都低且难分**时：

- 触发：`θ_low ≤ conf < θ_high` **且** Top-2 候选差 `margin < 0.08`
- 行为：用 `intent_strong` **仅调一次**重分类，新结果替换旧 delta；仍低 → clarify

**④ clarify 受控反问（防注入绕过）**  
L4 把"待澄清意图候选 + 缺失必填槽"交 `intent_lite` 生成**一句自然语言反问**（如"你想建站还是先查资料？"），约束：

- 模板受限：强制单选/短答结构，**禁止开放式长问**
- 走 `intent_lite`，不用租户模型（符合决策 5）
- 绝不在反问里回显用户原始 PII（S0 已脱敏）

**⑤ auto_calibrate 算法（回决策 1）**  
`config/router.yaml` 的 `θ_*` 上线初值固定，离线任务每周回写：

```
输入: metrics_events(type=intent_classify) 最近 N 轮 {conf, ground_truth(人工标注/用户澄清反馈)}
算: 最优切点使 直路由误判率<2% 且 clarify率<15%
约束: 只允许收窄不允许放宽 (保守), 带 [min,max] 钳制防漂移
输出: 回写 θ_low/θ_high
```

---

### 1.6 中途用户干预处理（Human-in-the-Loop / 执行中断）

**问题**：S6 执行中（Agent 正在跑 Skill / 调工具 / 生成产物）用户突然发消息——可能是否定/反驳（"停，不对"）、纠正方向（"不是建站是改红色"）、补充信息（"再加一页团队介绍"）、或元指令（"保存进度"）。这不是新 turn，是**对进行中流程的干预事件**。

**① 输入分类（S0 入口即标记）**

| 类型    | 判定信号                                | 动作                               |
| ----- | ----------------------------------- | -------------------------------- |
| 驳回/中止 | "停/取消/不对/别做了/等等" + 当前有 running Task | 置 cancel，终止全部在跑 Task             |
| 纠正方向  | 含否定词("不是…是…" / "改成") + 指向当前 Task 产物 | SIR 快照回滚 + 注入新 delta 重跑该/后续 Task |
| 补充信息  | 新增槽/需求（"再加一页…" / "用真实数据"）           | 增量写 SIR（S3 合并），续跑后续 Task         |
| 元指令   | "保存/切换模型/清空记忆"                      | 系统控制面处理，不进意图流                    |

> 分类本身**走 `intent_lite`**（与路由同模型，符合决策 5），不消耗租户模型。

**② 中断优先级最高**

- 执行态（任意 Stage 在 running）收到用户新消息 → 标记为 `InterventionEvent`，**不新建 TurnContext**，而是注入当前 ctx。
- 后端置 `ai:cancel:{turn_id}`（复用旧系统 C1 断连取消的 cancel 信号，并增强为"主动干预"也置位）。
- Skill / Tool 循环在\*\* checkpoint 点轮询 cancel 信&#x53F7;**（每步、每次工具调用前），命中即**安全退出\*\*（不破坏已落盘产物，已完成的 Task 保留 `done`），当前 Task 标 `cancelled`。

**③ 干预后接续（复用 S3 快照链 + A1 决策）**

- **驳回**：cancel → 当前产物保留为草稿，等用户下条指令；不自动重跑。
- **纠正**：`ai:sir:snap:{cid}`（Redis 热路径，LTRIM 10，A1 决策保留热路径）回滚到最近快照 → 应用新 delta（S3 纯函数合并）→ 从被纠正的 Task 起重跑；已完成 Task 不重跑。
- **补充**：delta 增量合并进 SIR（只新增槽，不回滚）→ 续跑后续 Task。
- 全部重新走 **S4（仅 affected Task）→ S5 → S6 局部**，不重跑已完成的 L1/L2/L3 全链路（除非用户明确"重新理解"）。

**④ 与 Approval Gate 区分（职责边界）**

- **Approval Gate（S6 执行前）**：高风险工具（HIGH，`site_delete`）执行**前**挂起等用户确认——是计划内的确认点。
- **用户干预（本 §）**：执行**中**用户主动打断——是计划外的 HITL 事件，优先级高于一切，含否定/纠正/补充/元指令四型。
- 两者通过不同信号区分：Approval 用 `ai:gate:approval:{req_id}`（挂起等确认），干预用 `ai:cancel:{turn_id}` + `InterventionEvent`（立即中断）。

> **前端配合**：执行中输入框状态=「可干预」，发送即触发 §1.6②；SSE 下发 `interrupted` / `cancelled` / `corrected` 事件告知前端进度态。

### 1.7 L1 强指令清单（唯一可直路由的规则信号）

L1 **只认强信号**，分两类：

**A. 会话控制指令（高精正则，零歧义）**——绕过意图流，直接系统处理：
| 指令 | 正则/信号 | 动作 |
|---|---|---|
| 继续/恢复 | `^(继续|继续上一步|resume|go on)$` | 唤醒挂起 Task（Approval 后重跑） |
| 重置/清空记忆 | `^(清空记忆|reset memory|重新开始对话)$` | 清 `ai:sir:snap:{cid}` + 当前 SIR（不删 DB） |
| 停止当前 | `^(停|停下|cancel|中止)$` + 有 running Task | 触发 §1.6 驳回/中止 |
| 切换模型 | `^(切换模型|改用.*模型)$` | 改 `users.preferred_exec_model` |

**B. 极强直动词指令（动词极强、几乎不可能歧义）**——可零成本直路由到对应 Skill（仍过 L4，但免 L2/L3 调用）：
| 触发 | 直路由意图 |
|---|---|
| `删除项目`/`彻底删除`/`永久删除` | `project/purge`（→ CRITICAL 双确认） |
| `放进回收站`/`删除到回收站` | `project/trash` |
| `恢复项目` | `project/restore` |
| `部署上线`/`发布到生产` | `project/deploy`（→ CRITICAL 双确认） |

**B 类之外的所有业务创作意图一律不直路由**——即使 L1 命中正则，也只把该意图作为 L3 候选集放进 few-shot，**最终分类 + 多意图检测由 L3 完成**（防漏判顺带意图）。
> L1 命中项与 `intent_catalog.json` 同走 CI 重叠校验（§9.4），A/B 类指令与 8 个 l1 的触发词若冲突启动即失败。

---

### 2.1 l1 意图域（与 8 个 Skill 严格 1:1）

| l1              | 对应 Skill（step1 §2.2） | 典型 l2                                   |
| --------------- | -------------------- | --------------------------------------- |
| `build_site`    | `site_build`         | `from_scratch` `from_design` `from_doc` |
| `design_advice` | `site_design`        | `layout` `style` `component`            |
| `review_code`   | `site_review`        | `audit` `fix`                           |
| `doc_generate`  | `doc_write`          | `readme` `spec` `copy`                  |
| `requirement`   | `req_clarify`        | `clarify` `pref_extract`                |
| `web_qa`        | `web_research`       | `research` `lookup`                     |
| `chat`          | `general_chat`       | `explain` `casual`                      |
| `project`       | `project_manage`     | `trash` `restore` `purge` `deploy`      |

> **互斥保证**：每个 l1 有独立 `trigger_keywords` + `examples`，`intent_catalog.json` 由校验脚本保证无重叠触发词（CI 门禁）。

### 2.2 IntentSchema（集中声明，step1 曾指出"旧系统缺集中 schema"）

每个意图在 `intent_catalog.json` 声明：

```json
{
  "intent_id": "build_site/from_design",
  "l1": "build_site", "l2": "from_design",
  "title": "基于设计稿建站",
  "risk_level": "high",                 // 允许调度的最高 tool 风险 = skill.risk_ceiling
  "trigger_keywords": ["设计稿","建站","做出网站","Figma"],
  "examples": ["帮我按这个设计稿做个落地页","用这个 Figma 出网页"],
  "required_slots": ["design_source"],   // 必填槽(进 S5 校验)
  "optional_slots": ["style","pages","color"],
  "shared_slots": ["project_id","user_id"],  // 跨意图共享(从 SIR 继承)
  "slot_formats": {"design_source": "url|file_ref", "pages": "int(1-20)"},
  "skill": "site_build",                // 路由目标(见 §5)
  "max_steps": 12
}
```

> **单一真相源**：S5 校验、L1 触发词、S4 路由、Skill 选择**全部读 `intent_catalog.json`**，不再散落正则/硬编码（修复旧 cascade.py/common.py 的槽位散落问题）。  
> 旧 `intent_catalog.json` 仅含 `id/skill/required_slots/level1/level2/risk` 且缺 `slot_formats/shared_slots/examples` —— 本步补全为完整 IntentSchema。

### 2.3 IntentSchema 在线编辑与热加载（单一真相源如何免重部署更新）

> 既是"单一真相源"，就不应每次改意图都走代码发版。本系统支持**运行时热更新 + 后台可视化编辑**，但**不牺牲一致性校验**。

1. **加载入口单例**：进程启动时 `IntentCatalog.load()` 一次性读 `intent_catalog.json` 入内存 `dict`；全局只读 `get_catalog()` 提供查询，路由/校验/Skill 选择全部经此，避免散落副本。
2. **热重载（`reload()`）**：
   - 本地开发/运维：`file-watch`（`watchdog`）监听 `intent_catalog.json` 变更 → 自动 `reload()`；校验失败（见③）则**保持旧版本不替换**并告警，不阻塞服务。
   - 后台管理端：管理员在可视化编辑器改意图（增删 l2、改 `trigger_keywords`/`risk_level`/`skill` 映射）→ 写盘 + 调 `POST /admin/intent-catalog/reload`，进程内原子替换（旧 dict 不可变共享，新 dict 就绪后整体换引用，避免读到半更新状态）。
3. **强制一致性门禁（与 `lint_intents.py` 同款）**：任何 reload/写盘前必须过校验，任一失败则拒绝：
   - `intent_id` 全局唯一；`l1/l2` 组合唯一；`skill` 必须存在于 step1 §2.2 Skill 清单；`risk_level` ∈ `{low,mid,high,critical}` 且不高于目标 Skill 的 `risk_ceiling`。
   - `required_slots` 中每个 slot 必须在 `slot_formats` 有定义；`shared_slots` 仅允许 `project_id/user_id/conversation_id` 等白名单跨意图槽。
   - `trigger_keywords` 不得跨意图强重叠（重叠率 > 阈值 → CI 告警，呼应 step2 §1.7 "L1 重叠硬校验"）。
   - JSON Schema 合法（无尾逗号/类型错）。
4. **版本与回滚**：每次成功 reload 写 `intent_catalog.json` 的同时落 `intent_catalog.audit.{ts}.json` 备份 + 记 `app/logs/intent_catalog.log`（谁、何时、改了啥）；回滚 = 选备份文件 `reload()`，无需发版。
5. **CI 即发版门禁**：`scripts/lint_intents.py` 在 PR 阶段强制跑（也作为运行时校验复用），不一致直接 red，杜绝"改了文件但结构错"上线即路由崩。
6. **灰度**：多实例部署时，reload 信号经 Redis pub/sub（`ai:intent:reload`）广播，所有 worker 同步换引用；单 worker 校验失败不影响其他 worker 的旧真相源。

> 简言之：**文件是真相源，进程热加载，后台可改，CI 兜底，备份可滚，多实例广播一致**——避免"单一真相源"变成"发版才能改"的枷锁。

### 2.4 旧 RoleAgent（6 角色）与 8 Skill 的职责映射详细对比

> 用户决策「roles 并入 planner 不单列」。但原 `app/agent/roles/` 下 **6 个 RoleAgent + 强 Schema 交接物（handoff）** 是已验证的能力集合，不能"并入"就当能力丢失。下表逐角色给出**原职责 → 新架构承接点**，证明 6 角色能力 100% 被 8 Skill + Pipeline + 全局统计线承接，无能力缺口。

| 旧 RoleAgent | 原职责（封装的 Skill / 产出） | 强 Schema 交接物 | 新架构承接（Skill / Stage / 模块） | 关键差异（角色边界 → 意图/阶段边界） |
| --- | --- | --- | --- | --- |
| **`product`（产品分析师）** | 需求澄清、范围界定、验收标准 → 封装 `agent_requirement` | `PRD` | `req_clarify` Skill（`l1=requirement`）+ **S3（SIR 合并）+ S5（缺槽澄清）** | 不再有独立 PM Agent；需求直接沉淀为 **SIR 共享槽 + `requirement_doc`（DB JSON，可版本化）**，后续任意 Skill 按需读取 |
| **`design`（设计顾问）** | UI/UX 设计、布局/风格/组件建议 → 封装 `agent_design` | `DesignSpec` | `site_design` Skill（`l1=design_advice`）+ `site_build` 内 `design_token`（required_slot） | 设计意图经 L1/L4 直路由；`design_token` 作为 `build_site` 的**硬依赖 required_slot**（见 §4 依赖推断），由 DAG 自然排序，无需 handoff 显式传递 |
| **`dev`（开发工程师）** | 代码规划 + 实现 → 封装 `agent_build` / `agent_generate_site` / `agent_doc` | `CodeArtifact` | `site_build`（`build_site`）/ `doc_write`（`doc_generate`）Skill + **Planner（S5/S6）负责架构与任务拆分** | 架构/拆分不再由 dev Agent 单点承担，改由 **Planner 的 Task DAG（§6）+ splitter（§4）** 统一编排，支持多意图并行 |
| **`qa`（质量评审）** | 质量评审 + 测试 → 封装 `agent_review` + `scoring` | `ReviewReport` | `site_review` Skill（`l1=review_code`）+ 全局 **`qc_scores` / `flow_checks`**（S8 + 后台 judge） | 打分与流程复查从"角色产出"升级为**全局统计线（step1 §3.5）**，与具体角色解耦；S8 出口统一做安全/质量校验，所有 Skill 共用 |
| **`orchestrator`（编排层）** | 角色级身份注入 + 上游交接物捕获 + SOP 顺序编排 | 协调 SOP（product→design→dev→qa） | **Pipeline（S0–S9）+ TurnContext（唯一真相源）+ splitter（S4 DAG）**（§7） | 编排由 **Pipeline + TurnContext 单一真相源** 承担；多意图依赖关系由 §4 依赖推断表达（build 依赖 design），不再靠 orchestrator 维护 SOP 顺序 |
| **`handoff`（强 Schema 交接物）** | 定义与传递 `RoleHandoff`：`PRD`/`DesignSpec`/`CodeArtifact`/`ReviewReport` | `RoleHandoff` | **SIR（共享槽）+ `requirement_doc` + `content_path` 产物指针 + 各 Skill `run.py` 输入契约**（step1 §3.1） | 交接物从"角色间强 Schema"泛化为"**阶段间 TurnContext/SIR 状态**"；`content_path` 提供产物精确指针（path/version/status），下游 Skill 按契约读取，比 handoff 更松耦合、更易观测 |

**承接结论**：
1. **能力零丢失**：6 角色的 4 类交接物（PRD/DesignSpec/CodeArtifact/ReviewReport）全部有对等载体——PRD→`requirement_doc`、DesignSpec→`design_token`/SIR 槽、CodeArtifact→`content_path` 产物指针、ReviewReport→`qc_scores`/`flow_checks` 评分。
2. **边界模型升级**：旧系统是"角色边界"（谁该看什么、依次交接），新系统是"**意图边界 + 阶段边界**"——路由（S4）定意图、Pipeline（§7）定阶段、SIR/TurnContext 定共享状态，更利于并行（§4 DAG）、可观测（§1.4.1 阶段进度条）、与统计解耦。
3. **SOP 自然表达**：旧的 `product→design→dev→qa` 顺序，现由**意图依赖推断**自动生成（build 的 required_slot 含 design_token 且 SIR 未满足 → design 必须先于 build，§4.2 依赖语义），无需 handoff 模块显式排程。
4. **QA 全局化是净增益**：原 `qa` 的打分仅服务被评审的那次产出；新设计把 `qc_scores`/`flow_checks` 提到全局统计线，覆盖**所有意图所有轮**，质量闭环更完整（呼应 step1 §3.5 "统计不被破坏"铁律）。

> 因此「roles 并入 planner 不单列」= **角色 Agent 类不新建，但其职责被 8 Skill + Planner(DAG) + 全局统计线 完整吸收**；第四步落地时 `app/agent/roles/` 目录不再保留，对应逻辑分别落到 `app/skills/<name>/run.py`（执行）、`app/router/`（路由）、`app/core/pipeline.py`（编排）、`app/stats/`（质量）。

---

## 3. DST / SIR 状态合并（S3，纯代码，不调 LLM）

复用 step1 结论，明确落点：

- **S2 产出 `sir_delta`**：LLM 只负责"理解变化"（提取本轮 slots/intent 变化），输出结构化 delta，**不合并**。
- **S3 `apply_delta(sir, delta) -> sir'`**：纯函数（旧 `dst.py` 已具备 CARRYOVER/UPDATE/DELETE/DONTCARE 四操作 + 来源优先级 + 置信度）。本步保留其纯函数性，补**快照链**（A1 决策：快照归内容表，随 purge 删）。
- **快照落点**：`sir_snapshots`（step1 §3.2，FK→conversation_id，`prev_snapshot_id` 自链）；热路径 `ai:sir:snap:{cid}`(Redis LIST, LTRIM 10)。
- **回滚**：S3 合并前先在 Redis/MySQL 写入 pre-merge 快照，校验失败可回滚到上一轮 SIR。

**S3 冲突解决与回滚触发（运行级细节）**
- **来源优先级**（同槽多源冲突时）：用户本轮显式输入 > 上一轮 SIR 锁定值 > 检索到的记忆建议 > 模型默认推断。
- **置信度门控**：S2 提取的 delta 带 `confidence`；低于 0.6 的槽变更标记为 `tentative`，不直接覆盖现有 SIR，留待 S5 校验时澄清或 S8 输出时提示。
- **回滚触发**：① S5 校验发现 S3 合并后必填槽矛盾（CARRYOVER 与 UPDATE 打架）→ 回滚到 pre-merge 快照并重跑 S2→S3；② 用户 §1.6 纠正 → 从 `ai:sir:snap` 回滚到被纠正 Task 对应轮次再注入新 delta（不重跑已完成轮）。
- **快照热/冷双写**：合并后 `ai:sir:snap:{cid}`（Redis LIST，LTRIM 10 保留最近 10 轮，A1 决策保留热路径）+ `sir_snapshots`（MySQL，持久真相源，prev_snapshot_id 自链）；Redis 丢失以 MySQL 为准重建。

---

---

## 4. 多意图切分 + Task DAG（S4 后半）—— 一句话含 N 个意图的完整处理

> 典型场景：「**帮我按这个 Figma 做个官网，首页配色用科技蓝，顺便给这个项目写个 README，再用网络搜一下竞品**」→ 一句里同时命中 `build_site` + `design_advice` + `doc_generate` + `web_qa` 四个意图。以下是从这句话进来到全部意图跑完的**端到端闭环**。

### 4.1 完整流程（S0 → S9）

```
S0 网关
  └─ 收到一句话 user_msg + 当前 SIR(含 project_id 等共享槽)
        │
S2 理解(S2: 提取 sir_delta)         ← 不调路由分类, 只理解"变化"
        │
S3 合并(S3: apply_delta)            ← 纯函数合并共享槽, 写快照
        │
S4 路由分类(L1→L2→L3→L4)            ← intent_lite 三级漏斗
  ├─ 分类器返回多意图候选:
  │     [ {build_site/from_design, 0.92},
  │       {design_advice/style,     0.88},
  │       {doc_generate/readme,     0.79},
  │       {web_qa/research,         0.74} ]
  │
  ├─ ① 逐个过 L4 门控(§1.5②):
  │     - 主意图阈值 θ_high/θ_low; 从意图阈值 θ_secondary=0.7
  │     - design 0.88 / doc 0.79 / web 0.74 ≥ 0.7 → 保留
  │     - 若有某候选 <0.7(如 0.5 的"可能想部署")→ 静默丢弃, 不入 DAG
  │
  ├─ ② 主意图裁决(primary adjudication):
  │     - 取置信最高者 build_site(0.92) 为 ROOT/主意图
  │     - 其余为从意图; 若多个 ≥θ_high 同分 → 按 intent_catalog.risk_level
  │       与"是否建站类"优先级裁决(建站 > 内容产出 > 问答), 杜绝双主根
  │
  ├─ ③ 依赖推断(dependency inference):
  │     - 查 intent_catalog 的 deps 声明 + SIR 槽:
  │        design_advice 产出 design_token → build_site 消费  ⇒ design 先于 build
  │        doc_generate / web_qa 与 build 无数据依赖          ⇒ 并列叶子
  │
  └─ ④ splitter.py 生成 Task DAG(写入 tasks 表, parent_task_id + deps JSON):
        ROOT: T1 build_site(from_design)
         ├─ T2 design_advice(style)   [deps: T1 等待其 design_token? 见 4.3 ②]
         ├─ T3 doc_generate(readme)   [deps: none, 并行叶子]
         └─ T4 web_qa(research)       [deps: none, 并行叶子]
        │
S5 校验(每个 Task 独立校验 required_slots)  ← build_site 缺 design_source? 触发澄清
        │  (用户补全后回到 S4 重切, 已生成 Task 不废)
S6 调度执行(§6, 当前串行拓扑序):  T2 → T1 → T3 → T4
        │  (每个 Task 落一个 Skill, 经 ToolRegistry 门控)
        │
S8 输出装配(§9.3 降级 + output_guard)
        │
S9 持久化 + 聚合回填(§4.4)
```

### 4.2 各环节细节（补齐原骨架遗漏）

1. **主意图裁决（primary adjudication）**：
   - 取 L4 通过后置信最高者为 `primary`，它是 DAG 的唯一 ROOT，对应对话线程里**主回复/主产物**。
   - 平局规则：多条 ≥`θ_high` 时，按 `(risk_level 越高越主? 否)`——本系统定为 **业务建站类优先于内容类优先于问答类**（`build_site` > `design_advice`/`doc_generate` > `web_qa`/`chat`），再 tie-break 用置信度。杜绝"双主根导致两条回复打架"。
   - 若**所有候选都 < `θ_high` 且只有 1 个 ≥ `θ_low`** → 该单意图直接为主，其余全丢弃（常规单意图路径）。

2. **依赖推断（dependency inference） rule**：
   - **硬依赖**（数据流向）：A 的产物是 B 的 `required_slot` → A 必须先于 B（`design_advice.design_token` → `build_site`）。
   - **软并列**（无数据依赖）：可并行，但本系统因 §6.2 决策"暂只串行"而**排队顺序执行**（T1 同层叶子按 `risk_level` 高的先跑，无关紧要）。
   - **跨会话复用**：若 SIR 已含 `design_token`（上一轮已做过设计），本轮 `build_site` 的该槽已满足 → 即便分类出 `design_advice` 也可降级为**不新建 Task**（仅在主回复里复用旧 token），避免过度拆分。这是"上下文感知切分"，不是机械每句全拆。

3. **部分失败隔离（partial failure, 关键缺口补全）**：
   - DAG 执行中某从意图 Task `failed`（如 `web_qa` 网络超时）→ **不连坐主意图**。`build_site` 仍正常交付，`web_qa` 标 `failed` 并把错误汇总进 S8 输出（3-part error），用户看到"官网已生成，但竞品搜索失败：xxx，可重试"。
   - 仅当**主意图/ROOT Task 失败**或**主意图的硬依赖 Task 失败**（如 `design_advice.design_token` 挂了，build 没素材）→ 主产物无法交付，整轮降级为"部分成功 + 明确告知缺哪块"。
   - 失败 Task 触发 §6.2.1 Replanner 修订一次仍失败则标 `failed`（不无限重试），用户二次输入可单独重跑该 Task（走 §3 否定/补充机制，仅重跑失败节点）。

4. **输出聚合与回填（S9 aggregation）**：
   - 多意图最终在对话线程只有**一条主回复**（`primary` 意图的产物 + 摘要），`design_advice`/`doc_generate`/`web_qa` 的产出作为**子块/附件**挂在主回复下（前端 Activity Panel 展开看各 Task 详情，呼应 step3 §1.4）。
   - 各 Task 写产物经 `messages.content_path[]` 聚合（step1 §3.1），主 `message` 一行记录本轮全部文件引用；统计 `qc_scores`/`flow_checks` 按 Task 维度分别打分再汇总 overall（step1 §3.5）。
   - 计费：`usage_ledger` 按 Task 分别记 token（step1 §3.5），多意图 = 多笔明细，用户可在用量页看"本轮哪块花了多少"。

### 4.3 两个易错决策点（必须钉死）

- **② 依赖方向陷阱**：`design_advice` 何时先于 `build_site`？只有当 build 的 `required_slots` 含 design_token 且 SIR 未满足时才建硬依赖；若用户明确"先用上次的设计"，则不建依赖、design Task 省掉。避免"为拆而拆"导致无谓等待。
- **③ 澄清不废 DAG**：S5 校验发现主意图 `build_site` 缺 `design_source` 需澄清时，**已生成的 T2/T3/T4 不废**，仅挂起 T1 等用户补；用户补完从 T1 续跑，不从零重切——呼应 step3 §1.3"断点续跑、LLM 不重跑"。

> 一句话总结多意图哲学：**一句话 = 一个 DAG；置信裁决主从、门控筛噪声、依赖定次序、失败不连坐、输出聚一条、计费拆多笔。** 既不丢用户真实诉求（多意图都跑），又不让用户被 N 条回复轰炸（聚合成一条主回复 + 可展开子块）。

---

---

## 5. 意图 → Skill 调度器（Dispatcher）

S4 路由结果（intent）→ 查 `intent_catalog.json.skill` → 得到 Skill。`project/*` 4 个 l2 全归 `project_manage`（step1 §6 已定）。

| Intent (l1/l2)                    | Skill            | 关键 Tools（与 step1 §6 一致）                                                                     |
| --------------------------------- | ---------------- | ------------------------------------------------------------------------------------------- |
| `build_site/*`                    | `site_build`     | rag_query, mem_recall, img_generate, fs_write, html_validate, site_publish, browser_capture |
| `design_advice/*`                 | `site_design`    | rag_query, mem_recall, img_generate                                                         |
| `review_code/*`                   | `site_review`    | fs_read, html_validate, browser_capture, fs_write                                           |
| `doc_generate/*`                  | `doc_write`      | rag_query, mem_recall, fs_write                                                             |
| `requirement/*`                   | `req_clarify`    | mem_store, mem_recall, rag_query                                                            |
| `web_qa/*`                        | `web_research`   | web_search, web_fetch, rag_query, mem_store                                                 |
| `chat/*`                          | `general_chat`   | rag_query, mem_recall                                                                       |
| `project/trash` `project/restore` | `project_manage` | project_recycle                                                                             |
| `project/purge`                   | `project_manage` | project_purge                                                                               |
| `project/deploy`                  | `project_manage` | site_deploy                                                                                 |



> Dispatcher **只做映射，不决定执行细节**（step1 §6 原则）。执行细节由 Skill 内部策略决定。

---

## 6. Agent 执行流程（S6 · Plan-and-Execute / ReAct 混合）

### 6.1 行业共识

- **Plan-and-Execute**：Planner(强模型, 一次) 生成 DAG Plan → Executor(小/廉价模型或确定性 runner) 逐步执行 → Replanner(稀疏触发) 仅失败时修订。
- **ReAct 适合短任务**（≤4 步、环境不确定）；**Plan-and-Execute 适合长任务**（≥5 步、强依赖）。生产系统常**按请求在两者间路由**。
- **成本/可控性**：Planner 用强模型，Executor 用廉价模型；每步有 checkpoint，失败时从断点续跑。

### 6.2 本系统执行模型

```
S6 进入: ctx.task_dag (S4 产出) + ctx.skills_to_run
  │
  ├─ 简单任务 (Task 数 ≤ 4 且无强依赖) → Skill 内 ReAct 轻量循环 (max_steps=5)
  │     loop: Thought → Tool(经 ToolRegistry 风险门控) → Observation → 直到 done
  │
  └─ 复杂任务 (Task 数 ≥ 5 或有依赖) → Plan-and-Execute
        Planner(强模型): Task DAG → 有序 Plan (SKILL 内 policy.py 产出)
        Executor: 按依赖调度每个 Task → 调对应 Skill → Skill 调 Tools
        Replanner: 某 Task failed/观测偏差 → 修订剩余 Plan (稀疏, 不每步)
```

> **执行并发策略（用户决策 2：暂只串行）**：Task DAG **仅按依赖拓扑串行执行**，无并行分支。即：取 DAG 中入度为 0 的 Task 顺序执行，一个完成再取下一个；依赖全部满足才解锁后续。不做同层级并发（如 build_site 与 web_qa 也排队跑）。理由：①硬件受限环境省并发/锁开销；②避免共享 SIR 并发写入冲突（并行需加写锁+合并策略，复杂度高，当前不需要）。**未来若需并行**：在 Executor 增加「无依赖 Task 批」并发池 + SIR 按 Task 加写锁 + 提交时三方合并，预留扩展位但本期不实现。

- **Skill 是执行单元**（step1 §2）：每个 Task 落到一个 Skill；Skill 内 `policy.py` 决定 plan/react 模式与工具编排；`risk_ceiling` 强制工具访问上限。
- **工具调用一律经 `ToolRegistry`**（step1 §1）：HIGH → Approval Gate 挂起（`ai:gate:approval:{req_id}`）；CRITICAL → 默认拒绝，仅 `project_manage` 显式提权 + 白名单双确认。
- **可观测**：每个 Task 起步写 `tasks`(pending→running→done/failed)，每 Tool 调用写 `tool_calls`（step1 §3.2），Skill 运行写 `agent_runs`。

**S6 执行器契约细节**
- **模型档位绑定**（决策 5）：执行 Skill/工具的实际生成 **只许读** `config/models.yaml` 的 `exec_standard|pro|ultra` 其一（取 `users.preferred_exec_model`）；Plan-and-Execute 的 Planner 用 `exec_pro`（强规划），Executor 用用户档；ReAct 全程用用户档。绝不在 S6 调 `intent_lite`。
- **Skill 工具面暴露**：LLM 在某一 Skill 上下文**仅看到该 Skill 声明的子集**（step1 §1.2，"按 Skill 子集暴露"），由 `SkillLoader` 在装载 `SKILL.md` 时从 `tools` 字段切片注入 system prompt，规避 MCP「>15 工具选择退化」。
- **中断检查点（与 §1.6 对齐）**：ReAct 循环的 Thought→Tool→Observation 每一步、Plan-and-Execute 的每个 Task 起步前，**轮询 `ai:cancel:{turn_id}`**；命中立即 `TurnCancelled` 安全退出（已完成 Task 保留 `done`）。
- **失败重试边界**：单 Tool `retryable=true` 最多 3 次指数退避（step1 §1.5①）；Task 级 `failed` 触发 Replanner 修订一次，仍失败则 Task 标 `failed` 并把错误汇总进 S8 输出，不无限重试（fail-safe）。
- **产物落盘与 content_path**：每个写产物 Tool 成功后回写 `messages.content_path[]`（step1 §3.1 结构），S9 统一持久化；中途 cancel 的 pending 产物标记 `status='deleted'` 或保留为临时供恢复。

#### 6.2.1 Planner 失控防护护栏（fail-safe，必做）

> Plan-and-Execute / ReAct 的最大风险是 LLM **生成无限 Plan / 永不收敛 / 自我循环**。以下护栏是强约束，不依赖模型自觉：

1. **Plan 修订上限（`max_plan_revisions=2`）**：Replanner 最多修订 2 次（初版 + 2 次修订）。第 3 次仍失败 → Task 标 `failed`，错误汇总进 S8，转 `error` 事件（3-part），**绝不无限重排**。
2. **Plan / DAG 规模硬上限**：单 turn 的 Task 数 ≤ `max_tasks=20`；单 Task 的 plan step ≤ `max_steps_per_task`（来自 `IntentSchema.max_steps`，默认 12）；ReAct `max_steps=5`（§6.2 已定）。超限即截断并标 `failed`（原因 `plan_too_large`），避免 OOM/烧 token。
3. **停滞检测（stuck detection）**：Task 进入 `running` 后超过 `task_timeout=300s` 未产出工具调用或状态变更 → 强制 `cancelled` + 记 `metrics_events(type=task_stuck)`；ReAct 连续 `max_no_op_steps=3` 步无新工具调用/无 delta → 判停滞，转 Replanner 或中止。
4. **执行预算（token/time budget）**：每个 turn 在进 S6 时预算 `ctx.exec_budget`（来自 `config/quota.yaml` 的 tier 算力预算）；逼近预算 80% → 停派新 Task，已完成的交付，未完成的标 `cancelled` 并告知用户"可继续追加"。避免单会话烧穿当日额度（呼应 step3 §5）。
5. **循环回路检测**：维护 `visited_state_hashes`（按 `current_task_id + last_action` 哈希）；同一哈希重复出现 ≥3 次 → 判死循环，强制中止（原因 `loop_detected`）。这是 ReAct「反复 same action」的根治。
6. **工具调用频率熔断**：单 Task 内同一工具 10s 内调用 > `max_same_tool_rps=5` → 限流并返回 `ok=False,error_code='tool_busy',retryable=true`，防「疯狂重试某工具」拖垮外部依赖。
7. **可解释失败**：上述任一护栏触发，必须产出结构化 `fail_reason`（`what/why/next` 三件套），前端以 3-part Error Surface 展示（呼应 step3 §1.4），不能只甩"执行失败"。

> 护栏核心思想：**LLM 可以聪明，但系统必须设边界。** 任何"模型说再等等/再试一次"都不能突破上述硬限。

---

### 6.3 中断/审批桥接（S6 ↔ 前端）

- HIGH 工具触发 → 写 `ai:gate:approval:{req_id}`，S6 挂起返回 `needs_approval` 事件；前端确认/拒绝后由 S6 续跑（断点续跑，不重跑已完成 Task）。
- CRITICAL 仅在 `project_manage` 提权路径，走双确认后再执行。

---

## 7. Pipeline 骨架（S0–S9 编排）

### 7.1 TurnContext（全链路唯一真相源）

```python
@dataclass
class TurnContext:
    # 身份/追踪
    request_id: str; trace_id: str; user_id: int; project_id: int|None
    conversation_id: int; turn_no: int
    # 输入(S0)
    raw_input: str; normalized_input: str; pii_redacted: bool
    # 路由/意图(S2/S4)
    sir: dict; sir_delta: dict|None            # S3 合并后 sir 更新
    intents: list[IntentMatch]                  # 含主从、confidence
    task_dag: list[Task]                        # S4 产出
    skills_to_run: list[str]
    # 校验(S5)
    validation: list[ValidationResult]          # pass|clarify|block
    # 执行(S6)
    tool_calls: list[dict]; tasks_state: list[dict]
    # 记忆(S1/S7)
    memory_hints: dict; memory_decisions: list[dict]  # Storage Gate 结果
    # 输出(S8)
    output_draft: str; guard_results: list[dict]
    # 审计(全程)
    audit: list[StageAudit]                     # 每阶段一条 → session_audits
```

### 7.2 Stage 契约与编排器

```python
class Stage(ABC):
    id: str                                   # "S0".."S9"
    @abstractmethod
    def run(self, ctx: TurnContext) -> TurnContext: ...

STAGES: list[Stage] = [S0Gateway(), S1Recall(), S2Understand(), S3Dst(),
                       S4Classify(), S5Validate(), S6Execute(),
                       S7Memory(), S8OutputGuard(), S9Archive()]

class Pipeline:
    def run(self, ctx: TurnContext) -> TurnContext:
        for stage in STAGES:
            t0 = now()
            try:
                ctx = stage.run(ctx)
                self._audit(ctx, stage.id, ok=True, ms=now()-t0)
                self._metric(stage.id, ms=now()-t0)        # ai:stage:{id}:* 埋点
            except AbortTurn as e:                         # S5 block / S8 reject
                self._audit(ctx, stage.id, ok=False, err=str(e))
                ctx.output_draft = e.user_message
                break
        return ctx
```

> **~200 行自研编排器，不用 LangGraph**（线性流水线不需要图引擎，零新依赖，符合 step1 决策）。  
> 每个 Stage **自动打点 + 写 `session_audits`**（step1 §3.2），即总图 PHASE9 审计格式先行落地。  
> 纯规则 Stage（S0/S3/S5）import-linter 强制禁止 import LLM client。

### 7.3 阶段职责速查（S0–S9 严格对齐总图）

| Stage | 名称                    | 主要动作                                                                | 调 LLM?       | 关键产物                           |
| ----- | --------------------- | ------------------------------------------------------------------- | ------------ | ------------------------------ |
| S0    | 请求接入(网关)              | trace_id / PII 脱敏 / 限流 / 注入初筛                                       | 否            | normalized_input               |
| S1    | 记忆召回(Recall Gate)     | 判定是否召回、召回哪些集合；从 `ai:sir:snap`/Chroma 取                              | 否            | memory_hints                   |
| S2    | 意图理解 + SIR_delta      | LLM 提取变化(槽/意图) → delta                                              | 是(理解)        | sir_delta                      |
| S3    | DST 状态更新              | 纯函数合并(快照可回滚)                                                        | 否            | sir'                           |
| S4    | 意图分类 + 切分             | L1→L4 路由 + 多意图 DAG + Dispatcher→Skill                               | 视 L1 命中      | intents/task_dag/skills_to_run |
| S5    | 规则校验                  | 四层(必填槽/格式/权限/工具风险)                                                  | 否            | validation[]                   |
| S6    | Plan-and-Execute      | 调 Skill→Tool(风险门控) / Plan or ReAct                                  | 视任务          | tool_calls/tasks               |
| S7    | SIR 回写 + Storage Gate | 写 `sir_snapshots`；Memory Gate 判定 store/skip                         | 否            | memory_decisions               |
| S8    | Output Guard + 回复     | 规则校验+脱敏+格式 + `intent_lite` 安全/合规判定；生成回复                              | 否(规则)+intent_lite判定 | output_draft + output_guard_log |
| S9    | 会话归档                  | 落 `messages`(含 content_path) + `session_audits` 汇总 + flow_checks 触发 | 否            | 持久化                            |

> **S5 四层校验**（总图 PHASE5）：① 必填槽齐备 ② 槽格式合法 ③ 权限/归属 ④ 工具风险分级——任一失败 → clarify(缺槽) 或 block(非法)，流程暂停/终止，**不进 S6 省成本**。

---

## 8. 与第一步的交叉一致性确认

- 路由表(§5) = step1 §6 路由速查 → **完全一致**。
- 风险分级(§1/§6) = step1 §1.2 → **完全一致**（HIGH Approval Gate / CRITICAL 默认拒）。
- 统计/审计表(§7.2) = step1 §3.2 `session_audits`/`tool_calls`/`tasks`/`agent_runs`/`sir_snapshots` → **字段已对齐**。
- 回收站三态(§6.3 CRITICAL purge) = step1 §3.3 → **一致**。
- A1 决策落实：S7 写 `sir_snapshots` 归内容表，purge 时随项目删（step1 §3.3 清理顺序①已含）。

---

## 9. 已拍板的最小决策（用户 2026-07-31）

1. **θ_high / θ_low 阈值**：`θ_high=0.85` / `θ_low=0.5` 作**上线初值**，后续用 `metrics_events` 的 `event_type='intent_classify'` 实测分布**自动校准**（不在代码里硬编码死，集中放 `config/router.yaml`，配 `auto_calibrate=true` 由离线任务回写）。
2. **L3 分类模型档位（按推荐）**：默认用**轻量档小模型**（强意图分类足够，仅歧义时升档）。档位名 `intent_lite`（映射 1 个廉价模型）；升档键 `intent_strong` 仅在 `θ_low ≤ conf < θ_high` 且候选 ≤2 个等歧义场景触发**一次**，不进入常规路径。
3. **S1 Recall Gate 默认策略（按推荐）**：新会话首轮**默认召回**——先召回用户级 `u_{uid}_mem`，再用 S0→S4 初判的 l1 命中对应项目级集合（`p_{pid}_*`，若尚未建项目则跳过）；`memory_hints` 控制后续轮的增量召回。
4. **Intent 重叠 CI 硬校验（加）**：在 `reset_all.py` 初始化 + CI `lint_intents.py` 中加**触发词/正则重叠即失败**硬校验——载入 `intent_catalog.json` 后两两比对 `triggers` 正则 + 关键词集合，有交集直接 `sys.exit(1)`（含重叠 l1 名、l2 在某 l1 内重名、IntentSchema 缺字段）。防"l1 脑补"。
5. **租户模型策略（用户决策 5）**：系统提供 **3 档任务执行模型**（`exec_standard`/`exec_pro`/`exec_ultra`），**仅用于 S6 任务执行**、租户在 `users.preferred_exec_model` 自选其一；**意图判断(S4)与结果判断(S8)一律走固定轻量模型 `intent_lite`**，与租户选择解耦、不可变。详见 **§1.4 模型档位与阶段绑定**。
6. **embedding 模型（用户决策 1）**：全平台锁 **`text-embedding-v3`**（云端调用，服务器硬件不行不做本地推理），维度默认 **1024**，集合创建即固定、禁止混维度（step1 §4.5）。
7. **多意图 Task 并发（用户决策 2）**：Task DAG **本期仅串行**（拓扑依赖顺序，无并行分支），避免共享 SIR 并发写冲突与硬件开销；并行预留扩展位（见 §6.2）但不实现。
8. **三档执行模型具体映射（套餐特性，用户决策 3）**：`exec_standard=hy3` / `exec_pro=qwen` / `exec_ultra=deepseek(按量)`；`intent_lite=qwen-turbo`、`intent_strong=qwen-plus`（均 token-plan 套餐内，边际≈0）。`exec_ultra` 前端设置页**必须明示"按量计费"**供租户知情选择。可配置默认值，代码只认档位键（step1 §1.4.1）。
9. **S8 加 `intent_lite` 安全/合规判定（用户决策 4）**：在规则校验+脱敏+格式之上追加 `intent_lite` 判定轮（toxic/compliance/unsafe），结果**必落 `output_guard_log` 专表**（统计域、purge 不删、可查询），安全分回写 `qc_scores.dimension='safety'`。详见 **§1.4.2**。

> 这 9 点已冻结，第二步骨架设计**完成**。下一步进**第三步：代码结构落地**（写 `intent_catalog.json` 完整 schema + `IntentRouter`/`Pipeline`/`TurnContext` 骨架代码 + `reset_all.py` 适配 step1 的 13 内容表 + 统计表）。

## 10. 开放架构：租户模型扩展位（第三步落地用）

第二步"路由用 `intent_lite` + 歧义升 `intent_strong`"已在 **§1.4** 完整约束租户模型策略；第三步写路由/执行代码时，所有模型调用**只允许读 §1.4 的 5 个档位键**，禁止硬编码具体模型名，保障租户切换零侵入。
