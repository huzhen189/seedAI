# 意图识别链路流程对比方案：SIR 现状 vs 规则优先级联（你的方案）vs 混合优化（我的方案）

> 文档目标：把你提出的「**规则优先 → 向量召回 → 大模型判断 → 置信度门控**」4 步链路，与当前已落地的 **SIR 状态化跨轮** 方案做工程级对比，并给出一版融合了两者优点的**混合优化方案**。
>
> 适用对象：后端（7102 AI 核心）/ 业务（7101）/ 前端（7100）研发。
> 关联版本：`v1.1.0`（SIR 已提交，tag `v1.1.0`，未推送）。

---

## 0. TL;DR（结论先行）

| 维度 | 现状 SIR（v1.1.0） | 你的方案（规则优先级联） | 我的优化（混合级联） |
|---|---|---|---|
| 每轮是否必调 LLM | **必调**（L2 异步） | **仅模糊时调**（规则/向量命中则跳过） | **仅模糊时调**（同你，多一条 super-fast 直通） |
| 多轮一致性保证 | 粘性信念 `update_belief`（复杂，已修 1 个 bug） | 靠「当前任务状态」注入 LLM（简单透明） | 任务态注入 + 槽位记忆（继承两者） |
| 意图空间是否显式 | 否（LLM 自由判） | **是**（向量候选目录显式定义） | **是**（向量目录 + 可校准） |
| 可解释性 | 信念轨迹 + 规则信号 + JSONL | 向量相似度 + 命中规则 + LLM 推理 | 三者并集，且多「新奇度兜底」 |
| 成本/延迟 | 高（每轮 LLM） | 低（热路径 0 LLM） | 最低（热路径 0 LLM + 直通） |
| 冷启动/无向量库 | 优雅降级每轮独立 | 依赖 Chroma（已有 :8000） | 降级到 规则+LLM-only |

**一句话建议**：采用「你的方案」作为主干（它把 LLM 从「每轮必调」降为「按需调用」，这是最大的成本/延迟收益），但补上我方案里的 **① 向量 super-fast 直通、② 任务态注入替代粘性算术、③ 新奇度兜底、④ 槽位跨轮记忆** 这 4 点，避免回到 SIR 那种「为保多轮而引入复杂粘性公式」的老路。

下面逐层展开。

---

## 1. 现状：SIR 状态化跨轮方案（as-built，代码精确还原）

### 1.1 真实执行顺序（`classify_v2`，pipeline.py:132）

```mermaid
flowchart TD
    A[用户消息 messages] --> B0{选项选择短路?<br/>resolve_selection}
    B0 -- 命中/指定skill --> Z0[直接路由 Route<br/>不重分类]
    B0 -- 否 --> B1[L1 加载跨轮信念<br/>Redis IntentState]
    B1 --> B2[L2 发射语义LLM<br/>asyncio.create_task]
    B2 --> B3[L3 同步规则五维<br/>run_rules + run_context + run_safety]
    B3 --> B4[await 语义结果<br/>超时35s降级]
    B4 --> B5[融合 cur=0.55*sem+0.45*rule<br/>update_belief 粘性更新]
    B5 --> B6{安全critical?}
    B6 -- 是 --> X1[block 拦截]
    B6 -- 否 --> B7[run_tools 映射skill]
    B7 --> B8[_decide<br/>COMMIT/CLARIFY/CHAT/RESET]
    B8 --> B9{route 且 build类?}
    B9 -- 否 --> Z1[route/clarify/chat]
    B9 -- 是 --> B10[多意图拆分<br/>maybe_split 两阶段]
    B10 --> B11{轻量门控<br/>≥2意图类?}
    B11 -- 否 --> Z1
    B11 -- 是 --> X2[LLM深拆 SubTask[]<br/>≤3 serial/parallel]
    X2 --> Z1
```

### 1.2 关键事实（来自代码，非推测）

- **LLM 在每一轮都跑**（`B2` 无条件发射），只是用 `asyncio.create_task` 让规则模块与之并行，省的是规则计算时间，不是 LLM 调用本身。
- **融合是固定加权**：`cur_score = 0.55*semantic.conf + 0.45*rule.score`（pipeline.py:215）。
- **粘性信念**（`state.py:update_belief`）：已收敛 build(0.8) 遇闲聊 aside(0.3) → `max(0.3, 0.8*0.95)=0.76 ≥ 0.7`，强抗打断。这是 SIR 满足「多轮仍明确」的核心，但逻辑复杂、有边界坑（已修 1 个）。
- **决策**：`running ≥ 0.70 → COMMIT`；`0.40 ≤ running < 0.70` 或 LLM 要澄清/缺规格 → `CLARIFY`（≤2 轮）；否则 `CHAT`；显式退出词 → `RESET`。
- **可观测**：每轮写 `logs/intent_observations.jsonl`，`outcome` 由 Worker 异步回填。
- **多意图拆分**（`splitter.py`，见 §7 专项）：仅当 `decision==route 且 bel_l1==build` 触发。两阶段——Stage1 规则门控（零 LLM，`_GATE_KEYWORDS` 命中 ≥2 类才进）＋ Stage2 LLM 深拆成 `SubTask[]`（上限 3、依赖声明 serial/parallel、失败降级单意图不阻断）。

### 1.3 优点 / 痛点

✅ 多轮一致性结构性强，闲聊打断不掉 build。
✅ 全链路 JSONL 可观测，信念轨迹可追溯。
❌ **每轮必调 LLM**：成本/延迟高，且对「生成网站/继续/退出」这类确定性意图是浪费。
❌ 粘性公式隐式、难调参，跨方向切换的边界行为需要专门测试守着（我们已经踩过 1 次）。
❌ 意图空间不显式——到底是 BUILD/REQUIREMENT/STYLE_EDIT… 全靠 LLM 自由发挥，难做版本化与回归测试。

---

## 2. 你的方案：规则优先 → 向量召回 → 大模型判断 → 置信度门控

### 2.1 链路（4 步级联，热路径零 LLM）

```mermaid
flowchart TD
    A[用户消息] --> S1{Step1 规则强信号?<br/>rules_catalog 硬匹配}
    S1 -- 命中强信号 --> R1[直接路由 Route<br/>conf=1.0 跳过后续]
    S1 -- 否 --> S2[Step2 向量召回<br/>embed→Chroma top5候选意图]
    S2 --> S3{Step3 大模型判断?<br/>有歧义才调}
    S3 -- 清晰/高相似 --> R2[按向量top1路由]
    S3 -- 模糊 --> L1[LLM 结合 5候选+上下文<br/>+业务规则+任务态+工具列表]
    L1 --> S4{Step4 置信度门控}
    S4 -- conf高 & 槽位齐 --> SP{多意图检测?}
    SP -- 否 --> R3[执行 Route]
    SP -- 是 --> SPL[LLM深拆 SubTask[]<br/>复用 intent_catalog ≤3]
    SPL --> R3
    S4 -- conf低 / 缺关键参数 --> C1[追问 Clarify ≤2轮]
    C1 --> S2
    S4 -- 全低相似 --> CH1[CHAT 闲聊]
```

### 2.2 Step 1：规则强信号直接路由（我帮你生成的规则目录）

> 落地为 `intent/rules_catalog.json`，与现有 `ruleset.json`（五维加权）并存：后者做「软评分」，前者做「硬路由」。**命中即路由，不进向量/LLM**。

```json
{
  "version": 1,
  "tiers": {
    "P0_hard_route": [
      {
        "id": "RESET",
        "match": "(算了|不用了|不做了|当我没说|随便聊聊|就聊聊天|退出|终止任务|取消任务|先不弄了)",
        "route_to": "agent_chat",
        "confidence": 1.0,
        "side_effect": "clear_intent_state"
      },
      {
        "id": "RESUME_BUILD",
        "match": "(继续|开始生成|那就生成|按刚才的|按我说的|生成吧|动手做)",
        "require": "has_requirement_doc == true",
        "route_to": "generate_site",
        "confidence": 1.0
      },
      {
        "id": "EXPLICIT_BUILD",
        "match": "(帮我)?(做|生成|开发|搭建|创建|建).*(网站|官网|站点|主页|首页|门户|整站|landing|官网)$",
        "route_to": "generate_site",
        "confidence": 1.0,
        "required_slots": ["site_type"]
      }
    ],
    "P1_strong_route": [
      {
        "id": "BUILD_PAGE",
        "match": "(加|增加|再来|做个).*(页面|页|落地页|landing)",
        "route_to": "generate_site",
        "confidence": 0.95,
        "required_slots": ["page_name"]
      },
      {
        "id": "REQUIREMENT",
        "match": "(写|生成|整理|出|帮我列).*(需求|prd|规格|方案)",
        "route_to": "agent_requirement",
        "confidence": 0.95
      },
      {
        "id": "STYLE_EDIT",
        "match": "(改|换|调整|优化|重做).*(样式|主题|配色|风格|颜色|视觉)",
        "route_to": "agent_style_design",
        "confidence": 0.9,
        "required_slots": ["target"]
      },
      {
        "id": "DEPLOY",
        "match": "(部署|发布|上线|部署到|挂到线上)",
        "route_to": "deploy",
        "confidence": 0.95
      },
      {
        "id": "CANCEL",
        "match": "(取消|停止|中止|别做了).*(生成|任务|构建|这次)",
        "route_to": "cancel",
        "confidence": 0.95
      }
    ]
  }
}
```

> 说明：`P0` 命中**直接路由并清空/复用状态**；`P1` 命中**路由但检查 `required_slots`**，缺槽位则转 Step4 追问。`match` 用正则，`require` 是上下文条件（如已有需求文档才能「继续」）。

### 2.3 Step 2：向量召回 top-5 候选意图

- **意图目录（显式定义，也是向量库的 seed）**：每个意图有 `id / description / sample_utterances[] / handler_skill / required_slots[]`。
- **嵌入**：复用现有 `Qwen text-embedding-v3`（与 Chroma 记忆层同一套），存入 Chroma 集合 `intent_candidates`（持久化、可版本化）。
- **召回**：当前消息 embed → 余弦 top-5 候选 + 各自相似度。
- **目录示例（节选）**：

| id | description | handler | required_slots |
|---|---|---|---|
| BUILD_SITE | 从零生成一个完整网站 | generate_site | site_type, style |
| BUILD_PAGE | 在已有站点上加页面 | generate_site | page_name |
| REQUIREMENT | 撰写/整理需求文档(PRD) | agent_requirement | — |
| STYLE_EDIT | 调整已有产物视觉风格 | agent_style_design | target |
| DEPLOY | 部署发布到线上 | deploy | env |
| INQUIRE_PRICE | 询问报价/工期 | agent_chat | — |
| EXPLAIN | 解释功能/概念 | agent_chat | — |
| CHAT_GENERAL | 闲聊/无明确意图 | agent_chat | — |
| RESET | 退出当前任务 | agent_chat | — |
| … | （共约 16 个，覆盖现有 INTENT_SKILL_MAP） | | |

### 2.4 Step 3：大模型最终判断（仅在模糊时调用）

> 把「5 候选 + 上下文 + 业务规则 + 当前任务状态 + 可用工具列表」拼成结构化 prompt，让 LLM 做**有界选择**而非自由发挥。

**System Prompt 要点**：
```
你是意图裁决器。只能从下方「候选意图」中选 1 个（或判为 CHAT_GENERAL）。
输入：
 ① 候选意图（top5，含描述/相似度）：{candidates}
 ② 对话上下文摘要：{context_summary}
 ③ 已命中业务规则：{matched_rules}（P0/P1 若命中会标注）
 ④ 当前任务状态：{task_state}（如 进行中build/等待确认/已有需求文档）
 ⑤ 可用工具列表：{tool_list}（skill 名 + 一句话说明）
输出 JSON：
 {
   "chosen": "意图id",
   "confidence": 0.0~1.0,
   "missing_slots": ["site_type"],
   "clarification_questions": ["需要什么类型的网站？"],
   "reasoning": "一句话"
 }
约束：若 top5 最高相似度<0.45 且无规则命中 → chosen=CHAT_GENERAL。
```

> 关键点：**LLM 不再「从零分类」，而是在 5 个已召回候选里做有界裁决**——这比 SIR 让 LLM 自由产出 `level1/level2` 稳定得多，也天然可回归测试（给定候选+上下文，断言 chosen）。

### 2.5 Step 4：置信度门控

```
conf ≥ 0.85 且 required_slots 全齐        → Route 执行
conf ≥ 0.85 但缺 required_slots           → Clarify（追问缺的槽，≤2 轮）
0.50 ≤ conf < 0.85                        → 若缺关键参数 Clarify，否则 Route（低确定性）
conf < 0.50 且 top1 相似度 < 新奇度兜底    → CHAT_GENERAL
conf < 0.50 但 top1 相似度 ≥ 兜底          → Clarify（让 LLM 补问）
```

### 2.6 多意图拆分（级联方案里的落点）

在级联方案里，多意图拆分**不再局限于 `build` 类**（这是 SIR 的一个局限）。落点设计：

- **检测前置到向量召回本身**：Step2 的 top-5 召回天然是多意图探测器——若 top-5 中有 ≥2 个**不同意图**且相似度均高于阈值（如 ≥0.55），直接判为多意图候选，无需再走一遍关键词门控（关键词门控保留作零延迟预筛）。
- **拆分裁决并入 Step3/Step4**：LLM 在 Step3 做有界裁决时，输出可同时含 `sub_tasks`（每个子任务复用 `intent_catalog` 的 `id`+`handler_skill`）；Step4 门控若 `is_multi` → 走拆分分支而非单路由。
- **约束复用**：`MAX_SUBTASKS=3`、依赖 `serial/parallel`、风险分级、失败降级单意图——全部沿用 `splitter.py` 现有实现，只是输入从「自由文本」变为「候选意图限定集」，更稳定。

---

## 3. 我的优化：混合级联（SIR × 向量，取其精华）

你的方案已经是正确方向。我补 4 点，让它既省钱又保住 SIR 的多轮能力、还更好维护：

```mermaid
flowchart TD
    A[用户消息] --> S1{Step1 规则硬路由<br/>rules_catalog}
    S1 -- P0/P1命中 --> QF{向量直通?<br/>top1.sim≥0.9 且规则一致}
    QF -- 是 --> R0[Super-fast 路由<br/>0 LLM 调用]
    QF -- 否 --> R1[路由+槽位检查]
    S1 -- 未命中 --> S2[Step2 向量 top5召回]
    S2 --> SF{top1.sim ≥ 0.9?}
    SF -- 是 --> R2[直接按top1路由<br/>0 LLM]
    SF -- 否 --> L1[Step3 LLM有界裁决<br/>5候选+任务态+工具]
    L1 --> S4{Step4 置信度门控}
    S4 -- 高且槽齐 --> SP{多意图检测?}
    SP -- 否 --> R3[Route]
    SP -- 是 --> SPL[LLM深拆 SubTask[]<br/>单源 intent_catalog]
    SPL --> R3
    S4 -- 低/缺槽 --> C1[Clarify ≤2轮<br/>槽位跨轮记忆]
    C1 --> S2
    S2 --> NF{top5 全 < 新奇度兜底0.45?}
    NF -- 是 --> CH1[CHAT_GENERAL<br/>不强行归类]
```

### 3.1 四点补强

1. **向量 Super-fast 直通**：当规则命中 **且** 向量 top1 相似度 ≥ 0.90（规则与向量互相印证），**直接路由，不调 LLM**。这是你方案里「规则命中就跳过」的强化版——多一道向量印证，避免规则误命中（如「做个网站多少钱」被 P0 错判为 BUILD 而非 INQUIRE_PRICE）。

2. **任务态注入替代粘性算术**：**不再用 `update_belief` 的粘性公式**。改为把「当前任务状态」（进行中 build / 等待确认 / 已有需求文档 / 上一轮 clarify 缺的槽）作为结构化字段注入 Step3 的 LLM prompt（④）。LLM 看到「用户正在 build，说『多少钱』」会自然判为 INQUIRE_PRICE 而非新意图——**多轮一致性由「上下文显式注入」实现，而非隐式信念累加**，可解释、可调、无边界坑。

3. **新奇度兜底（novelty floor）**：Step2 若 top5 最高相似度 < 0.45（且规则未命中）→ 直接 CHAT_GENERAL，**绝不强行塞进某个意图**。这解决了 SIR 里「低置信只能靠闲聊兜底但仍有被错误收敛风险」的问题。

4. **槽位跨轮记忆**：把 SIR `IntentState.specs/missing_specs` 的「槽位累积」保留，但**语义简化**——只存 `slots` 字典，clarify 轮次累积，收敛后随任务一起清。这比粘性信念轻得多，且天然服务「追问缺的槽」。

### 3.2 拆分落点（混合级联的单一事实源）

混合级联里「意图分类」和「意图拆分」**共用同一份 `intent_catalog`**——这是相对 SIR 最大的整洁度提升：

- 分类时向量召回的 top-5 候选，本身就能直接喂给拆分器当「候选子任务意图集」；
- 拆分 LLM 的 prompt 直接引用 `intent_catalog`（每个子任务的 `skill` 从 catalog 的 `handler_skill` 取，不另起白名单）；
- 因此「单意图分类」和「多意图拆分」不会再出现 skill 映射两处维护的问题（根治 R3）。

### 3.3 为什么这版比纯 SIR 好

- **成本**：热路径（规则+向量直通）LLM 调用 = 0；仅真正模糊才调。**相比 SIR 每轮必调，预计 LLM 调用量降 60–80%**（建站/继续/退出/改样式这类确定性意图占比高）。
- **可维护性**：删掉 `update_belief` 粘性公式（最易出 bug 的部分），多轮一致性改为「任务态注入 + 槽位记忆」两条显式逻辑。
- **可测试性**：意图目录显式 → 可写「给定候选+上下文→断言 chosen」的单元测试；SIR 的 `level1/level2` 自由产出很难断言。
- **多轮不退化**：任务态注入保留了 SIR 的抗打断能力（build 中途插「多少钱」仍判 INQUIRE_PRICE），只是实现更透明。

---

## 4. 维度逐项对比

| 维度 | SIR（现状） | 规则优先级联（你） | 混合级联（我） |
|---|---|---|---|
| 每轮 LLM 调用 | 1（必调） | 0~1（模糊才调） | 0~1（多一条直通） |
| 平均延迟（热路径） | ~LLM 延迟 | 规则+向量 ~20ms | 规则+向量 ~20ms（直通 0 LLM） |
| 多轮一致性 | 粘性信念（隐式） | 任务态注入（显式） | 任务态注入+槽位（显式） |
| 意图空间 | 隐式（LLM 自由） | 显式（向量目录） | 显式（向量目录+可校准） |
| 可解释 | 信念轨迹+JSONL | 向量分+规则+推理 | 三者并集+新奇度兜底 |
| 误判恢复 | RESET 显式退出 | RESET + 低相似兜底 | RESET + 新奇度兜底 |
| 冷启动/无 Chroma | 降级每轮独立 | 需 Chroma（已有） | 降级 规则+LLM-only |
| 维护复杂度 | 高（粘性公式） | 中（目录+向量） | 中（目录+向量+槽位） |
| 回归测试友好度 | 低 | 高 | 高 |
| 多意图拆分 | 仅限 build 类，两阶段（规则门控+LLM深拆） | 向量 top-5 天然探测，不限 build | 单源 catalog，分类/拆分共用 |

---

## 5. 迁移路径与风险（若决定落地）

**分阶段（不动 SIR 契约，新增并行实现）：**
1. **P1**：建 `intent_catalog.json`（16 意图 + sample + handler + slots）+ `rules_catalog.json`（§2.2）；脚本 seed 到 Chroma `intent_candidates`。
2. **P2**：实现 `classify_v3`（混合级联），与 `classify_v2` 并存，路由器加开关 `INTENT_MODE=v3`。
3. **P3**：离线回归：用 `scripts/sir_validation.py` 范式，新增 `scripts/cascade_validation.py`，断言「规则命中→0 LLM」「build 中途插闲聊→INQUIRE_PRICE 不翻 build」「低相似→CHAT」。
4. **P4**：灰度（按 user_id 切 v3），比对 `intent_observations.jsonl` 的 outcome 分布，确认准确率↑、LLM 调用量↓后切默认。

**风险：**
- 🔴 R1 Chroma 不可用 → 降级到「规则 + LLM-only」（不调向量，LLM 在自由候选里裁决）。
- 🟠 R2 向量相似度阈值（0.45/0.90）需按真实语料校准，先宽松后收紧。
- 🟠 R3 意图目录需随 skill 变更同步更新（做成 `INTENT_SKILL_MAP` 的派生，避免两处维护）。
- 🟡 R4 prompt 体积：5 候选+上下文+工具列表可能较大，用摘要裁剪上下文。

---

## 6. 建议

**采用混合级联（§3）作为下一版 `v1.2.0` 目标**，它直接复用你提的 4 步结构，只补 4 个工程化增强点。这样既拿到「LLM 按需调用」的最大收益，又不必背 SIR 粘性公式的复杂度债务。SIR（v1.1.0）保留为可回退实现（路由开关切回 `v2` 即可）。

> 下一步若你确认，我可以：① 直接落地 `intent_catalog.json` + `rules_catalog.json` + `classify_v3` 骨架（先不接 LLM，用规则和向量直通跑通热路径）；② 或先做一份 `cascade_validation.py` 把 §4 的对比用断言固化下来。你说走哪条。

---

## 7. 意图拆分（多意图）流程专项对比

多意图拆分是「一句话里藏多个独立可交付目标」的处理机制（如「做个博客，再写份部署文档」）。三套方案都保留**两阶段**骨架，但触发条件与数据源不同。

### 7.1 通用两阶段拆分流程

```mermaid
flowchart TD
    A[已判定为 Route 的单轮请求] --> G{Stage1 轻量门控<br/>零 LLM}
    G -- 命中≥2意图类 --> L[Stage2 LLM深拆<br/>SubTask[] ≤3]
    G -- 单意图 --> S[单路由执行]
    L --> D{有依赖?}
    D -- 是 --> SE[serial 串行]
    D -- 否 --> PA[parallel 并行]
    SE --> E[执行子任务]
    PA --> E
    L -- 全部模型失败 --> S
```

- **Stage1 规则门控**（`_lightweight_multi_check`）：用 `_GATE_KEYWORDS`（build/doc/code/learn/translate/design/search）扫描，命中 ≥2 类 → 疑似多意图；否则零额外延迟。
- **Stage2 LLM 深拆**（`split_intent`）：输出结构化 `SubTask{goal, original_text, level1/level2, skill, context_hint, risk_level, dependencies}`，约束：不同目标才拆 / 原子可独立 / 上限 3 / 依赖声明 / 风险分级。
- **降级**：LLM 失败 → 回退单意图，不阻断主流程。

### 7.2 三方案拆分对比

| 项 | SIR 现状 | 规则优先级联（你） | 混合级联（我） |
|---|---|---|---|
| 触发条件 | `route 且 bel_l1==build` | 任意 Route + 向量 top-5 含 ≥2 高相似不同意图 | 任意 Route + 向量 top-5（同你） |
| 多意图探测 | 仅靠 `_GATE_KEYWORDS` 关键词 | **向量召回天然探测**（top-5 多峰） | 向量召回天然探测（单源 catalog） |
| 子任务 skill 来源 | `SKILL_WHITELIST` 白名单 | `intent_catalog.handler_skill` | **同一 `intent_catalog`**（分类/拆分共用） |
| LLM 输入 | 自由文本 | 候选意图限定集 | 候选意图限定集（更稳） |
| 粒度上限 | 3 | 3 | 3 |
| 依赖/调度 | serial/parallel | serial/parallel | serial/parallel |
| 失败降级 | 单意图不阻断 | 单意图不阻断 | 单意图不阻断 |

### 7.3 关键改进点

1. **解除「仅 build 类」限制**：SIR 里「做个博客+写文档」若在非 build 语境不会拆；级联/混合方案基于向量多峰，任意意图组合都能拆。
2. **向量召回即探测器**：省掉一次独立关键词门控（仍保留作零延迟预筛），top-5 多峰直接判多意图。
3. **单源 catalog**：混合方案让分类与拆分的 skill 映射同源，根治「白名单 vs INTENT_SKILL_MAP 两处维护」的 R3 风险。
