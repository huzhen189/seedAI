# AI 核心流程对比：现状（v0.8.6） vs 你提出的新方案（已确认终版 · 2026-07-22）

> 本文档基于 `backend/ai_service/app/` 实际代码（v0.8.6）梳理**当前 AI 核心执行链路**，再与你提出的 4 步新流程逐项对比，标注**差异点、可复用资产、落地风险与建议**。
> 当前链路权威细节见 `docs/04-AI核心端细节.md`；意图管道规则见 `intent/pipeline.py` / `core/queue.py`(Worker)。

---

## 一、现状 AI 核心流程（代码实况）

### 1.1 端到端链路（一次用户输入）

```
浏览器 EventSource /api/chat (business :7101)
   └─> 业务代理把对话历史 + 项目上下文打包成 job
       └─> POST /generate (ai_service :7102)  ──> Redis队列 queue:generate
            └─> Worker 消费 (core/queue.py worker_loop)
                 ├─[1/6] 取出 job
                 ├─[2/6] Chroma 向量索引本轮消息 (asyncio.to_thread)
                 ├─[3/6] detect_intent_v2(messages, model)  ← 意图判定
                 ├─[4/6] 汇总器算出 decision + selected_skill (单一来源)
                 ├─[5/6] 按 decision 分流执行
                 └─[6/6] 后置 QC 三裁判 (run_qc) + §8 git 提交
                       └─> 事件流写入 Redis Stream gen:stream:<trace_id>  ── 回传前端
```

### 1.2 意图判定：5 模块并行 + 汇总器（`intent/pipeline.py`）

`classify_v2` 实际是「**1 个 LLM 语义模块飞行时，4 个零延迟规则模块重叠执行**」（并非真并行）：

| 模块 | 文件 | 作用 |
|------|------|------|
| selection 短路 | `intent/selection.py` | 用户回复待选项 / 显式指定 skill → 直接路由，不重跑 LLM |
| **semantic（LLM）** | `intent/semantic.py` | 6 类意图 + 行业 + 置信度 + 断点关系（只吃 `last[:500]`） |
| rules | `intent/rules.py` | 关键词/正则命中 |
| context | `intent/context.py` | 历史关联：WebLLM hint → Chroma 向量 → 关键词兜底（取最近 1 条 assistant/相关消息） |
| safety | `intent/safety.py` | HARD 词硬拦截 / SOFT 词语境中和 / 项目约束拦截 |

汇总器 `_aggregate` 决策顺序：**安全 critical→block**（死红线）→ 意图融合（语义 70% + 规则 20% + 上下文修正 10%）→ 工具选择 → high→confirm → 低置信度→系统自决 top-1 + alternatives（非阻塞）→ route。

### 1.3 决策分流（`core/queue.py` worker_loop `[5/6]`）

| decision | 行为 |
|----------|------|
| `block` | 安全拦截，发 `block` 事件，不可绕过 |
| `unsupported` | 降级 explain |
| `confirm` | 高风险待用户二次确认（发 `confirm` 事件，前端回传后重发） |
| `options` | **已改为非阻塞**：系统自己选 top-1，发 `alternatives` 提示，不卡用户 |
| `split` | 多意图 → `Orchestrator` DAG 调度 |
| `route`/`fallback` | 直接 `run_skill(selected_skill)` |

### 1.4 两大执行方向（现状）

- **闲聊/问答方向**：`explain` / `search_agent` / `write_code` / `fix_agent` 等 skill，内部直接 LLM 调用 + 流式 token，**无站内产物**（QC 仍跑，但 `generate_site`/`write_code` 才 git 提交）。
- **建站方向**：`generate_site` skill 内部**线性三阶段**（见 `skills/generate_site.py`）：
  `Planner`(结构化规格) → `paused`(发 plan 等用户确认) → `Coder`(流式单文件 HTML) → `Reviewer`(静态+LLM 自审) → **Reflexion**(≤3 轮修正直到 passed) → `cos_upload` 投递预览。
  另有 `requirement_agent`(需求分析) 在 `build/requirement` 时先行。

### 1.5 多意图（现状）

`splitter.py`：轻量规则门控（命中 ≥2 意图类）→ LLM 拆成 `SubTask[]`（上限 3，带依赖/风险）。
`orchestrator.py`：拓扑排序成层（`build_layers`），**层内并行、层间串行**，每子任务风险门控（HIGH 拒、MEDIUM 待确认）→ `run_skill` → `ResultMerger` 合并。

### 1.6 质检（现状，`qc.py`）

`run_qc` 三裁判（deepseek/qwen/hy3）并行，6 维打分（correctness/completeness/compliance/efficiency/readability/safety），聚合均值+方差。
**关键局限**：QC 在 `done` 之后跑，结果只 `publish` 给前端 + 落库 + 后台雷达图；**不驱动重执行**（失败仅 warn，不重试）。是「监督」不是「闭环自修复」。

---

## 二、你提出的新方案（4 步）

> 原文复述 + 我的解读（解读部分用 *斜体* 标注，便于你纠正）

1. **输入上下文融合**：用户输入 + 最近 5 条上下文（先 redis，没有则 db）+ 项目关键词 + 项目记忆 + 用户卡片 → 做一次**语义判断**。
   *解读：显式把「对话历史深度(5条) + 结构化项目知识(关键词/记忆) + 用户画像」作为意图判定的第一输入，而非现状那样只喂最后 1 条 500 字。*
2. **语义判断 → 执行计划**：判断能否拆多任务、怎么拆、生成执行计划。每个任务走不同执行方案（**大方向 2 个：闲聊问答、建网站相关**）。**若判断不确定，提问用户具体哪方面**。
   *解读：保留「拆分多任务 + 执行计划」，但明确「不确定就反问用户」——比现状「系统自决 top-1 不阻塞」更偏向交互澄清。*
3. **闲聊问答方向**：一般纯文字对话，无真正产物。先走**安全规则校验 + 项目规则校验** → LLM 查询 → 返回前端 → **对结果多模型并行打分**。
4. **建网站方向**：细分 **产品 / 开发 / 样式 / 测试 / 修改** 等子 agent。同样先**安全 + 项目规则校验** → LLM + 调用 skills/tools 执行 → 收集合并 → **多模型并行打分** → **评分过低则定位哪一环节不合格 → 重启那一步重执行**，直到产出完整可靠结果再返回前端。

---

## 三、逐项对比（现状 vs 新方案）

| 维度 | 现状（v0.8.6） | 新方案 | 差距/评价 |
|------|---------------|--------|-----------|
| **上下文输入** | 业务侧已传 `messages`(历史) + `project_system_prompt`(关键词) + `requirement_doc`(记忆) + 用户 id；但 **ai_service 语义分类只吃 `last[:500]`，context 模块只取最近 1 条关联**；用户卡片未进意图层 | 显式「最近 5 条(redis→db) + 项目关键词 + 项目记忆 + 用户卡片」一次融合 | **新方案更优**：现状上下文深度不足、用户画像缺席。但业务侧已有数据，主要是 ai_service 没充分利用 → 改动点在 `detect_intent_v2` 入参 |
| **意图判定架构** | 5 模块并行 + 安全优先汇总器（已含安全/项目约束/语义） | 一次性「语义判断」 | **✅已确认复用现状 5 模块**（含安全优先短路）：安全死红线不能丢，新方案 step1 = 现状 pipeline，仅扩展入参 |
| **多任务拆分 + 计划** | 有（`splitter` 规则门控+LLM，`SubTask` 带依赖/风险；`orchestrator` DAG） | 有（明确「执行计划」） | **已基本具备**，新方案的「计划」≈ 现状 `SubTask[]`。可平滑对齐 |
| **两大方向** | 现状是 6 类 level1（learn/code/build/doc/translate/unsupported），建站=build，闲聊≈learn | 闲聊问答 / 建网站相关 两分 | **新方案更直观**，本质是把 level1 收敛为 2 个业务桶。可在汇总器后加一层「归一化到 2 大方向」 |
| **建站内部分工** | `generate_site` 内部线性：Planner→Coder→Reviewer→Reflexion | product / 开发 / 样式 / 测试 / 修改 多 agent | **新方案更精细**，是现状最大增量。现状没有「产品/样式」独立角色，「测试」=Reviewer，「修改」=Reflexion。需把 generate_site 升级为**多 agent 团队** |
| **安全/项目校验时机** | 意图层 `run_safety`（HARD/SOFT/项目约束）全局拦截；skill 内部不再单独校验 | 每个方向/每个子 agent 执行**前**都走安全+项目校验 | **新方案更纵深**（纵深防御）。现状 skill 内无二次校验，若拆分后子任务拼接出危险组合可能漏。建议保留意图层拦截 + 子 agent 前复核 |
| **「不确定就提问」** | 现状：`confirm`(仅高风险) + `options`(已改非阻塞自决) | 不确定 → 反问用户具体方面 | **✅已确认：仅高风险操作反问**（低置信非高风险仍自决），与现状 `confirm` 对齐 |
| **结果打分** | 有 `run_qc` 三裁判全量并行（6 维） | 多模型并行打分 | **几乎一致**，新方案可直接复用 `qc.py` |
| **打分后的自修复闭环** | **无**：QC 只监督，不重执行 | **有**：评分过低 → 定位失败环节 → 重启该步重执行，直到可靠 | **这是新方案相对现状最核心的能力缺口**。现状 QC 与执行是脱钩的；新方案把 QC 变成执行回路的一环（Reflexion 升级为跨 agent 的失败定位+局部重启） |
| **产物/版本** | §8 每轮 git 提交 + COS bundle（仅 generate_site/write_code/orchestrator） | **✅已确认：单 trace、单产物目录、单 git 版本**（建站多 agent 共享同一 trace/目录/版本） | 与 §8 天然适配：git 在整轮/最终提交，不每子 agent 各提交一份 |

---

## 四、关键差异与落地风险

### 4.1 最值得借用的两点（现状缺失）
1. **QC 驱动的局部自修复闭环**（新方案 step4）：现状 `Reflexion` 只在 `generate_site` 的 Coder→Reviewer 内部跑 ≤3 轮；跨 agent（product/样式/测试）的失败定位和「只重启那一步」是空白。这是把系统从「能生成」提升到「生成得可靠」的关键。
2. **建站多角色 agent 团队**（新方案 step4）：把「产品/开发/样式/测试/修改」显式化，比现状单 skill 线性 Planner/Coder/Reviewer 更可控、更可观测、更易定位问题。

### 4.2 需要注意的代价
- **成本/延迟**：多模型并行打分 + 可能的重执行，单次请求 LLM 调用数显著上升。现状 QC 已 60s 超时；重执行预算**已确认**：单子任务超时 30s、单个子任务最多 3 轮自修复（超出转降级），否则可能比用户直接重试还慢。
- **「最近 5 条 redis→db」**：现状业务侧其实已经把历史带进 `messages`，ai_service 无需自己查 redis/db——**建议在业务侧统一取最近 N 条 + 项目记忆 + 用户卡片**，作为参数传给 `detect_intent_v2`，避免 ai_service 直连 DB（保持内网 service 只持模型 Key 的边界）。
- **「不确定就提问」的交互频率**：若对所有低置信都反问，会破坏「对话感」。建议分级：高风险/高代价操作才反问，纯文本闲聊低置信仍走自决。

### 4.3 与现状资产的映射（尽量复用，不重写）
| 新方案概念 | 复用现状 |
|-----------|----------|
| step1 语义判断 | `detect_intent_v2`（5 模块+汇总器），扩展入参（最近5条+项目记忆+用户卡片） |
| step2 执行计划 | `splitter.SubTask[]` + `orchestrator` |
| step3 闲聊方向 | `explain`/`search_agent`/`write_code` + `qc.run_qc` |
| step4 建站多 agent | 升级 `generate_site` 内部为 agent 团队（product/code/style/test/modify），保留 Planner 思想 + Reflexion |
| step4 安全/项目校验 | `run_safety`（已含 HARD/SOFT/项目约束），子 agent 前再调一次 |
| step4 打分 + 自修复 | `qc.py` 复用 + 新增「失败定位→局部重启」编排层 |

---

## 五、落地建议（若采用，渐进式）

1. **Phase A（低风险，先吃上下文红利）**：扩展 `detect_intent_v2` 入参，把业务侧已有的「最近 N 条 + 项目记忆 + 用户卡片」真正喂给语义判断；不动执行链路。
2. **Phase B（中风险，建站多 agent）**：把 `generate_site` 内部从线性 Planner/Coder/Reviewer 重构为 `product/code/style/test/modify` agent 团队（仍走 `run_skill` + 事件流），保留 paused 确认 + cos_upload。
3. **Phase C（核心增量，自修复闭环）**：在 `qc.py` 之上加「失败定位 + 局部重启」编排——QC 评分低 → 回看各环节产出自评 → 只重跑最差环节（带上一轮上下文）。预算已确认：单步 30s 超时、每子任务 ≤3 轮、best-of-N 保留最高分。
4. **Phase D（交互哲学）**：明确「不确定就提问」的触发条件（建议：仅高风险/高代价 + 低置信才反问），其余保持自决。

---

## 六、上一轮澄清点 — 已确认结论（2026-07-22 夜间）

| # | 问题 | 你的确认 | 落地含义 |
|---|------|----------|----------|
| 1 | step1 语义判断架构 | **复用现有 5 模块（含安全优先短路）** | `detect_intent_v2` 不动架构，仅扩展入参；安全死红线保留 |
| 2 | 建站多 agent 是独立 skill 还是内部阶段 | **`generate_site` 内部 agent 阶段**；保持**单 trace、单产物目录、单 git 版本** | 不拆 trace，所有子 agent 共享同一 `gen:stream:<trace_id>` 与 §8 artifact 目录；git 只在整轮/最终提交 |
| 3 | 「评分过低重启哪步」判定依据 | **每个 agent 产出结构化自评 + QC 交叉验证** | 失败定位 = agent 自评(逐维) × QC 三裁判(逐维) 双信号；任一/双低即定位该环节 |
| 4 | 超时上限 / 自修复预算 | **单子任务超时 30s；单个子任务最多 3 轮自修复** | 每 agent 步带 30s 硬超时；每子任务 repair 循环 ≤3 轮封顶，超出转降级 |
| 5 | 「不确定就提问」适用范围 | **仅高风险操作**（低置信但非高风险仍自决） | 与现状 `confirm` 对齐：HIGH 风险/高代价才发 `confirm` 反问，闲聊低置信不烦用户 |

> 以上 5 点已全部闭环，作为实施方案的硬约束。

## 七、补充优化方向 — 已确认（2026-07-22 夜间）

4 个优化方向你全部选了推荐项（A），与硬约束一起作为实施方案的固定决策：

| # | 方向 | 你的确认 | 落地含义 |
|---|------|----------|----------|
| 1 | 局部重启粒度（7.1） | **A 保留上游、只重启失败子 agent** | 失败环节重跑时复用上游已通过产物，成本/延迟最优 |
| 2 | 闲聊自修复（7.2） | **A 闲聊也加 1 轮轻量重答** | QC 标跑题/事实错/安全软警示时自动重答一次（不追问） |
| 3 | best-of-N（7.3） | **A 跨轮保留历史最高分产物** | 最终返回 3 轮里 QC 总分最高那版，避免末轮回退 |
| 4 | QC 节奏（7.4） | **A 每轮轻量自评 + 仅最终全量 QC** | 每轮 agent 结构化自评快速判停；整轮结束才三裁判权威 QC + 失败定位 |

---

## 八、实施方案纲要（before/after + 落地落点）

> 把「现状 → 新方案」拆成可执行的改造项，复用优先、不全重写。每项标注：改什么文件、依赖、风险。落地顺序按 Phase A→D。

### 8.1 总览：现状 vs 目标

| 环节 | 现状 | 目标（新方案，已确认） |
|------|------|------------------------|
| 上下文输入 | 语义只吃 `last[:500]`，context 取最近 1 条，用户卡片缺席 | 业务侧统一取「最近 N 条 + 项目记忆 + 用户卡片」作 `detect_intent_v2` 入参 |
| 意图架构 | 5 模块 + 安全优先汇总 | **不变**（复用），仅扩展入参 |
| 闲聊方向 | explain 等 + QC（仅监督，无重答） | explain 等 + 安全/项目校验 + QC + **低分 1 轮轻量重答** |
| 建站方向 | `generate_site` 线性 Planner→Coder→Reviewer→Reflexion | `generate_site` 内多 agent 团队（product/code/style/test/modify），**单 trace/单目录/单 git** |
| 建站安全 | 仅意图层 `run_safety` | 意图层 + **每个子 agent 执行前再 `run_safety`** |
| 打分 | `run_qc` 三裁判（done 后，不重执行） | `run_qc` 三裁判复用；**每轮 agent 结构化自评快判停，整轮末才三裁判** |
| 自修复 | 无（QC 仅 warn） | **失败定位（自评×QC）→ 只重启失败子 agent（保留上游）→ 最多 3 轮 / 单步 30s 超时 → best-of-N 保留最高分** |
| 「不确定就问」 | `confirm` 仅高风险 | **不变**，仅高风险反问（对齐） |

### 8.2 改造项清单

**Phase A — 上下文深度融合（低风险，先吃红利）**
- **A1 业务侧组装上下文**：`backend/business/app/proxy.py` `_build_messages_from_db` 扩展——除 `messages` 外，注入 `project_memory`（已有 `requirement_doc`/`project_system_prompt`）+ `user_card`（昵称/角色/套餐/偏好；需业务 `users` 表字段 or 新增 `user_profile`）。保持 ai_service 不直连 DB。
- **A2 `detect_intent_v2` 入参扩展**：`intent/pipeline.py` `classify_v2` 增加 `project_memory`/`user_card` 参数，喂给 `semantic` prompt（取代只吃 `last[:500]`）。context 模块改为取最近 N 条关联。
- 验证：复用 `scripts/smoke_agent_judgment.py` 跑 10 输入回归；新增「带用户卡片后分类更稳定」用例。

**Phase B — 建站多 agent 团队（中风险）**
- **B1 `generate_site` 重构为 agent 团队**：内部编排 product→code→style→test→modify 阶段，每阶段是一个 `async def` 子流程（非独立 skill，保持单 trace）。保留 `paused` 确认 + `cos_upload`。
- **B2 子 agent 前安全复核**：每个阶段入口调 `run_safety`（复用 `intent/safety.py`），高风险组合拦截。
- **B3 结构化自评**：每个子 agent 产出 `{passed, scores:{correctness..safety}, issues:[...], artifact}`（自评估，零/低模型成本），供 Phase C 快判停。
- **B4 事件流**：`gen:stream:<trace_id>` 增 `agent_step` 事件（阶段名 + 状态 + 自评），前端 `ThoughtTrail` 渲染「产品规划中/开发中/样式中/测试中」时间线（复用现有 plan 节点）。

**Phase C — QC 驱动的自修复闭环（核心增量）**
- **C1 编排层 `core/repair.py`（新增）**：消费各子 agent 自评 + `run_qc` 末轮三裁判；判定失败环节（`max(低维)`）→ 仅重跑该子 agent（带上游产物 + 上轮自评/QC 反馈作 prompt 上下文）。
- **C2 预算/超时**：单子 agent 步 `asyncio.wait_for(..., timeout=30)`；每子任务 repair ≤3 轮（`for round in range(3)`）；超时/超轮 → 标记 `needs_review` 降级返回 best-of-N。
- **C3 best-of-N**：每轮结束缓存该子任务 QC 总分；最终取最高分产物（非末轮）。
- **C4 §8 git 提交时机**：整轮（含所有 repair 轮）结束、`done` 前提交一次（单 git 版本），不每子 agent 提交。

**Phase D — 闲聊轻量自修复 + 交互收敛（低风险）**
- **D1 闲聊重答**：`explain`/`search_agent` 等 skill 在 QC 标跑题/事实错/安全软警示时，用自评+QC 反馈重答 1 次（仍单轮、不追问）；不通过则展示并 `needs_review`。
- **D2 「不确定就问」收敛**：确认为仅高风险反问（现 `confirm` 已满足）；`options` 保持非阻塞自决。

### 8.3 风险与回滚
- **回归风险**：Phase B 改 `generate_site` 内部结构，可能影响现有建站产物。回滚=git revert 该文件；用 `scripts/smoke_*` + 现有 10 输入回归守护。
- **成本风险**：Phase C 多轮 + 多 agent 增加 LLM 调用。靠 30s 超时 + 3 轮封顶 + 每轮仅自评（不每轮三裁判）兜底。
- **崩溃安全**：任一子 agent 抛错 → 捕获 → 标记该步 failed → 走 C1 失败定位或降级；不整轮崩。

### 8.4 统计接入（符合统计系统强制规则）
- 每个 Phase 改造项接入 `analytics.py`：使用次数 + 成功率/失败率 + 耗时 p50/p90/p99（Redis zset）；管理后台「系统分析」展示。

---

> **状态**：本文档为**终版设计**（9 项决策已全部确认：5 硬约束 + 4 优化方向）。下一步可落成 `engineering-remediation-plan.md` 风格实施方案并逐项改码（Phase A→D），改动即同步 `docs/04` 并本地 commit（不 push）。待你确认是否开工。
