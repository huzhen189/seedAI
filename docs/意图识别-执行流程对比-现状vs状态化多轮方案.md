# 意图识别：现状执行流程 vs 优化方案 vs 状态化多轮方案（SIR）

> 本文把**当前线上代码（v1.0.8 的 5-module 管线）**、**已写方案文档里的 Plan C（规则+LLM 加权融合）**、以及**本次新提出的 SIR（Stateful Intent Resolution，状态化跨轮意图解析）** 三者的执行步骤逐行对比，并回答一个核心问题：
>
> **「哪怕多轮对话，依然能明确识别用户意图」—— 现状为什么做不到，Plan C 为什么仍不够，SIR 为什么能。**
>
> 结论先行：**现状是"每轮独立分类"，Plan C 是"每轮独立融合（更细的信号）"，只有 SIR 引入了"跨轮信念状态（belief）"。用户的硬需求（多轮连续明确）在结构上只有 SIR 满足。** 内联 4 张流程图辅助理解（现状流 / Plan C 流 / SIR 三层流 / 跨轮状态机）。

---

## §1 现状执行流程（v1.0.8，stateless 5-module）

调用链：`前端 GET /api/chat` → `business/proxy.py`（拼 payload）→ `ai_service/core/queue.py` worker → `detect_intent_v2()` → `classify_v2()` → 路由 → `run_skill()`。

逐步骤（来自真实代码，非推测）：

```
[业务层 proxy.py]
  1. get_summary(conv) → 静态 conversation_summary 字符串
  2. 读 Project：project_status / requirement_doc / project_system_prompt / project_constraints
  3. 组装 payload：messages(全量历史) + context_hint(前端WebLLM hint或空) + summary + project_* + has_req_doc

[Worker queue.py [3/6]]
  4. 调 detect_intent_v2(messages, …, context_hint, project_status, has_req_doc)

[classify_v2 / pipeline.py]
  5. [0/5] resolve_selection 短路：用户如果是在回复"待选项" → 直接路由，不重分类
  6. [2/5] 发射 run_semantic (LLM 异步, ~2s)：只看"最后一条 user 消息[:500]" + context_hint
  7. [3/5] 同步跑 3 个规则模块：
        - run_rules：只扫"最后一条 user 消息"的关键词 → build/chat 二元命中(conf=0.7)
        - run_context：关键词聚合"最后一条 assistant 消息" → 产出 correction(可能把 build/site 翻成 build/page，即 RC2)
        - run_safety：红线
  8. [4/5] 等 LLM 语义结果(超时35s降级)
  9. [5/5] _aggregate 融合：
        final = semantic(level1/level2)            # 语义 70% 权重
        if context.correction: 覆盖 final          # 上下文 10%（F2 已加护栏防 site→page）
        if rule 与 semantic 冲突: confidence *= 0.7 # 规则 20%
  10. [6/6] run_tools：INTENT_SKILL_MAP 映射 + 死亡路由(无文档且无对话需求才改道回需求, F3) + 置信门控
  11. 多意图拆分 maybe_split（命中才调 LLM）

[Worker 决策分流]
  12. switch(decision): block / unsupported / confirm / options(非阻塞) / split / route
  13. run_skill(selected_skill) —— 技能内部自己读 requirement_doc / summary
  —— 结束。没有把"本轮回应的意图结论"写回任何跨轮存储。
```

### 现状的 4 个结构性短板

| # | 短板 | 后果（真实发生过） |
|---|------|--------------------|
| S1 | **无跨轮信念状态**：每轮从零分类，唯一的"多轮"是 `messages[]` 整体喂给 LLM prompt + 一个静态 `summary` 字符串 | turn N 不知道 turn N-2 已经倾向 build；一条闲聊 aside 就能让本轮分类翻转 |
| S2 | **规则只看最后一条消息**：`run_rules` / `run_context` 都 `reversed(messages)` 取第一条 user/assistant | 上下文修正靠"上一条 assistant 提及网页"→ 误把"网站"降级成"单页"(RC2)；"做个网站难吗"这种调研句被 build 词命中 |
| S3 | **澄清不是一等公民**：意图层没有澄清循环。只有 `requirement_agent` / `build` 技能各自发一次 `paused/await_confirm`（方案确认卡），不是"为消除意图歧义而追问" | 模糊输入（"我想做个网站"）直接按 build/requirement 路由，没有"最少必要追问"把意图坐实 |
| S4 | **无结构化可观测**：日志是 `logger.info` 文本，没有每轮统一 JSON 记录（意图/置信/规则命中/决策/延迟/消耗） | 权重/词表标校只能改码+重启，误判无法系统化复盘 |

> **一句话**：现状的多轮，本质是"把历史塞进 LLM 上下文窗口"，不是"系统记住了对话在朝哪个意图收敛"。这正是用户痛点的根源。

---

## §2 已写方案 Plan C（规则+LLM 加权融合，per-turn）

见 `docs/意图识别改进方案-对比分析.md`（推荐方案 C）。其执行流与现状的**差异**只在 6/7/9/10 步内部增强：

- **L2 语义扩展**：LLM 返回结构化 `{primary_intent, confidence, is_actionable, missing_specs, clarification_needed, clarification_questions}`（你提案第④点）。
- **L3 规则增强**：规则引擎不再是二元命中，而是计算 **5 维硬信号**——`lexical`(关键词密度) / `completeness`(约束计数) / `verb`(动词强度) / `context`(已有) / `behavior`(project 信号)，加权融合出三档（≥0.7 建站 / 0.4–0.7 咨询 / <0.4 闲聊）。
- **澄清子流程**引入：0.4≤score<0.7 或 `clarification_needed` → 主动反问（≤2 轮）。

```
差异点（相对现状）：
  6'. run_semantic → 返回结构化 intent JSON（含 is_actionable / missing_specs）
  7'. run_rules   → 计算 verb_strength + keyword_density + constraint_count（不再是二元）
  9'. _aggregate  → 五维加权融合 → 三档分数（而非 semantic70+rule20+ctx10 的单点）
  10'. run_tools  → 按三档路由；INQUIRY 带 → 澄清分支
  +   澄清漏斗：clarify(≤2轮) → 需求Agent出PRD → 方案确认卡
```

### Plan C 仍然不够的地方

**Plan C 依然是"每轮独立融合"**。它把单轮的信号算得更细、更准，但 **belief 不跨轮累积**：

- turn 1「我想做个网站」→ 单轮信号弱（verb 中、completeness 0）→ 判 INQUIRY，反问；
- turn 2「用 React 做企业官网，3 个页面」→ **重新从零算**，单轮信号强 → 判 BUILD。
- 这两步之间没有"turn1 已经偏向 build"的连续量。系统没有"对话正在收敛到 build"的记忆。
- 更糟：build 进行到一半，用户插一句「哎你说做个网站大概多少钱」→ 本轮单轮被 `lexical` 命中 + `verb` 弱 → 又掉回 INQUIRY/CHAT，**已经收敛的 build 信念被一条 aside 打断**（因为根本没有"粘性"）。

> **结论**：Plan C 解决了"单轮误判"（S2/S3 部分），但没解决"跨轮连续性"（S1）。用户的"哪怕多轮依然明确"——**Plan C 不满足**。

---

## §3 更优方案：SIR — Stateful Intent Resolution（状态化跨轮意图解析）

SIR 在 Plan C 的"三层管道 + 五维信号 + 澄清"基础上，**新增一层跨轮持久化信念（IntentState）**，把"本轮回应的噪声信号"与"对话正在收敛到的意图"解耦。这是相对 Plan C 的**增量价值**，也是满足用户硬需求的必要升级。

### 3.1 三层管道（目标架构，承载你提案第④点）

```
Layer 1  上下文重建与归一化 (Context Reconstruction)
         ├─ 从 Redis 加载 IntentState(conv_id)  ← 跨轮信念（现状完全没有）
         ├─ 归一化：当前消息 + 滚动窗口(last N turn) + 先验信念 + 会话/项目信号
         └─ 产出 NormalizedContext

Layer 2  意图理解与候选生成 (LLM-based NLU)
         └─ LLM 结构化候选：{primary, confidence, sub, is_actionable,
                              missing_specs, clarification_needed, questions, specs}

Layer 3  规则校验与决策 (Rule Engine + Policy)
         ├─ 规则引擎（热更新）：verb_strength / keyword_density / constraint_count / behavior
         ├─ 融合 → 候选分
         ├─ ★ 信念更新：Belief = f(先验IntentState, 本轮证据)   ← 跨轮累积核心
         ├─ 决策策略：COMMIT / CLARIFY(≤2轮) / CHAT
         └─ 写回 IntentState → 路由到 Specialist / 发 clarify 事件 / 闲聊
                  ↓
          路由到对应 Specialist（agent_generate_site / agent_chat / …）
```

### 3.2 跨轮信念状态（IntentState，Redis 持久化）

```python
# intent/state.py
@dataclass
class IntentState:
    conv_id: int
    belief_l1: str = "chat"          # 当前收敛方向
    belief_l2: str = "casual"
    running_conf: float = 0.0        # 跨轮累积置信（0~1）
    specs: dict = field(default_factory=dict)      # 已抽取的需求规格（跨轮累积）
    missing_specs: list = field(default_factory=list)
    clarify_rounds: int = 0          # 已澄清轮次（≤2）
    trajectory: list = field(default_factory=list) # 每轮信号快照（供可观测）
    updated_at: float = 0.0

KEY = "intent_state:{conv_id}"

def load_state(conv_id) -> IntentState | None: ...   # Redis GET + json
def save_state(s: IntentState): ...                  # Redis SET + TTL(会话级)
```

### 3.3 信念更新公式（粘性，抵抗闲聊 aside）

```python
STICK = 0.55  # 先验基础权重

def update_belief(prior: IntentState | None, cur_score: float,
                  cur_l1: str, cur_l2: str) -> tuple[float, str, str]:
    if prior is None or prior.running_conf == 0:
        return cur_score, cur_l1, cur_l2
    consistent = (prior.belief_l1 == cur_l1) or \
                 (prior.running_conf >= 0.7 and cur_l1 in ("chat", "casual"))
                 # 已收敛的 build 遇到闲聊 aside → 视为"不一致/噪声"
    if consistent:
        running = STICK * prior.running_conf + (1 - STICK) * cur_score   # 同向加速收敛
    else:
        stick2 = min(STICK + 0.25, 0.85)
        running = stick2 * prior.running_conf + (1 - stick2) * cur_score  # 强粘性抗打断
    running = max(0.0, min(1.0, running))
    # 方向取"更强证据"的一方
    l1 = cur_l1 if cur_score >= prior.running_conf else prior.belief_l1
    l2 = cur_l2 if cur_score >= prior.running_conf else prior.belief_l2
    return running, l1, l2
```

**为什么这能"多轮依然明确"**：
- 同向证据让 `running_conf` 逐轮**单调上升**收敛（turn1 0.5 → turn2 0.85 → COMMIT）。
- 已收敛(build, 0.8) 后插入闲聊 aside（本轮 0.3 chat）→ 公式走 `stick2=0.80`，`running = 0.8*0.8 + 0.2*0.3 = 0.70` → **仍 ≥0.7，保持 BUILD，不被打断**。
- 真要退出：用户显式"我就是随便问问" → 发 `reset` 信号 → `running_conf=0` 重置。

### 3.4 决策策略（含澄清状态机）

```
running_conf ≥ 0.70 且 is_actionable 且 missing_specs 空  → COMMIT  → 路由 specialist
0.40 ≤ running_conf < 0.70 或 clarification_needed：
        if clarify_rounds < 2  → CLARIFY（发 clarify 事件 + 动态最少追问 missing_specs）
        else                   → 若用户已确认"就做" → COMMIT(requirement)；否则 CHAT
running_conf < 0.40                                    → CHAT
显式退出("随便聊聊")                                    → RESET → CHAT
```

### 3.5 澄清子流程（≤2 轮，动态最少追问）

- 触发：INQUIRY 带 / `clarification_needed=true` / 低置信不猜。
- 追问内容**不是固定模板**，而是 LLM 返回的 `missing_specs`（如 `["page_count","tech_stack"]`）→ 动态生成 1–2 个最少必要问题。
- 轮次存 `IntentState.clarify_rounds`，Redis 持久；用户回复后**重新进管道**，信念累积（不是重来）。
- 显式退出优先：`"不用了/聊天而已"` → 立即 RESET，不再追问。
- 安全：模糊输入（"做个网站"）**不立即进重型 Planner**，先轻量澄清，避免浪费。

### 3.6 可观测 + 热更新落点（你提案第②③点）

- **可观测**：`intent/observation.py` 每轮 append 一条 JSONL（见下），字段对齐你给的 schema（`request_id` 新增、`tokens_used` 从 LLM `response_metadata` 取、`outcome` 异步回填）。
- **热更新**：`intent/ruleset.json`（词表 / 修正映射 / 动词强度 / 五维权重 / 阈值）外置；`rules.py` 三种重载策略（mtime 轮询 / 管理端点 / 配置中心），**reload 失败回滚** → 改权重/词表免重启 7102。

```json
{
  "timestamp": "2026-07-24T20:08:00Z",
  "request_id": "req_abc123",
  "conversation_id": 42,
  "user_id": "u_456",
  "raw_input": "帮我把登录改成支持 OAuth",
  "llm_intent": "MODIFY_FUNCTION",
  "llm_confidence": 0.87,
  "rules_triggered": ["verb:强", "lexical:2", "completeness:1"],
  "belief_before": 0.52, "belief_after": 0.78,
  "decision": "CLARIFY",
  "latency_ms": 1240,
  "tokens_used": 3200,
  "specialist_routed": null,
  "outcome": "pending"
}
```

---

## §4 三方案执行步骤逐行对比

| 步骤 | 现状 v1.0.8 | Plan C（文档推荐） | SIR（本次新提） |
|------|-------------|--------------------|-----------------|
| 入口 | `detect_intent_v2` | 同 | 同（重构内部） |
| 跨轮状态 | 无（每轮从零） | 无（每轮从零） | **Redis 加载 IntentState** |
| L1 输入 | 全量 messages + 静态 summary | 同 | 当前+窗口+**先验信念**+会话信号 |
| L2 NLU | LLM 返回 level1/2/conf | **结构化 JSON**（含 is_actionable/missing） | 同 + **specs 抽取** |
| L3 规则 | 二元关键词命中 | **五维加权分数** | 五维 + **融合 prior 信念** |
| 融合 | semantic70+rule20+ctx10 | 五维加权三档 | 五维 + **跨轮 belief 更新** |
| 澄清 | 无（仅方案确认卡） | ≤2 轮反问 | ≤2 轮反问 + **轮次持久 + 累积** |
| 决策 | route/confirm/block/options | route/clarify/chat | **COMMIT/CLARIFY/CHAT/RESET** |
| 状态回写 | 无 | 无 | **save IntentState** |
| 可观测 | 文本日志 | 文本日志 | **每轮 JSONL** |
| 规则更新 | 改码+重启 | 改码+重启 | **ruleset.json 热更** |
| 多轮连续性 | ✗（仅靠 LLM 上下文窗） | ✗（仍每轮独立） | **✓（信念累积+粘性）** |

---

## §5 三方案能力矩阵

| 维度 | 现状 | Plan C | SIR |
|------|------|--------|-----|
| 多轮连续性（用户硬需求） | ✗ | ✗ | ✓ |
| 单轮防误判（动词/完整度） | △（二元） | ✓ | ✓ |
| 闲聊 aside 抗打断 | ✗ | ✗ | ✓（粘性） |
| 澄清循环 | ✗ | ✓ | ✓（持久） |
| 延迟 | ~2s（LLM） | ~2s | ~2s（+Redis <5ms） |
| 成本 | 1 LLM/轮 | 1 LLM/轮 | 1 LLM/轮（同） |
| 可解释 | 低（文本） | 中 | 高（JSONL+trajectory） |
| 可观测复盘 | ✗ | ✗ | ✓ |
| 热更新调参 | ✗ | ✗ | ✓ |
| 实现风险 | — | 低 | 中（新增 Redis 状态 + 迁移） |

> **总评**：Plan C 是 SIR 的真子集。要满足"多轮依然明确"，SIR 的跨轮信念是**不可替代**的一步；Plan C 的信号精细化则是 SIR 的 L2/L3 内容，二者不冲突，应**在 SIR 框架内吸收 Plan C**。

---

## §6 SIR 落地步骤（分阶段，可独立提交）

**P0 — 信号精细化（即 Plan C 的 P0，零架构风险）**
- `intent/rules.py`：加 `verb_strength()` + `constraint_count()`，二元命中升级为带强度。
- 不改管道结构，纯增强；立刻缓解"做个网站难吗"误判。
- 文件：`rules.py` / `pipeline.py:_aggregate`。

**P1 — 结构化 NLU**
- `intent/semantic.py`：prompt 扩展输出 `is_actionable / missing_specs / clarification_needed / questions / specs`；解析进 `SemanticResult`。

**P2 — 跨轮信念状态（SIR 核心，本次新增）**
- 新增 `intent/state.py`：`IntentState` + `load/save_state`（Redis，会话级 TTL）。
- `classify_v2` 重构：步骤 5 前 `load_state`；步骤 9 融合后 `update_belief`；步骤 11 后 `save_state`。
- 决策枚举扩展 `COMMIT/CLARIFY/CHAT/RESET`。
- 文件：`state.py`（新）/ `pipeline.py` / `queue.py`（透传 conv_id 已具备）。

**P3 — 澄清漏斗接入 Worker**
- `queue.py` 决策分流新增 `clarify` 分支：发 `clarify` 事件（动态问题）→ 存 `clarify_rounds`；前端 `ChatView.vue` 复用 `paused/await_confirm` 渲染或新增澄清卡。
- 前端：`ChatView.vue`。

**P4 — 可观测 JSONL**
- 新增 `intent/observation.py`：每轮 append JSONL 到 `logs/intent_observations.jsonl`；`outcome` 由 Worker 执行后异步回填。

**P5 — 规则热更新**
- `intent/ruleset.json` 外置 + `rules.py` 加载/重载（mtime 轮询优先，简单可靠）；reload 失败回滚。

**建议顺序**：P0 → P1 → P2 → P3 → P4 → P5。P0/P1 可立刻做且不影响现状验证；P2 是分水岭，建议单独提交 + 写回归测试覆盖"多轮收敛 / aside 抗打断"两条路径。

---

## §7 风险与未决

- **R1 状态一致性**：多 Worker 并发同一 conv 时 `save_state` 竞态 → 用 Redis `SET NX`/乐观锁或单 conv 串行化（按 conv_id 分桶）。
- **R2 状态过期**：会话长时间挂起后 `IntentState` 失真 → 会话级 TTL + 超过 N 轮无 build 活动则衰减 `running_conf`。
- **R3 迁移成本**：现状 `decision` 枚举（route/confirm/block/options/split）需兼容映射，避免 Worker 分流大面积改动 → P2 先做兼容层。
- **R4 粘性过强**：`stick2` 太高会导致"用户真想改主意"被压制 → 显式 `override/correct` 关键词（已有语义 `checkpoint_relation`）强制降先验权重。
- **未决**：`request_id` 当前无全局生成点，需在 `proxy.py` 或 Worker 入口补；`tokens_used` 依赖 LLM provider 返回 `response_metadata`，需确认 deepseek/qwen 适配层是否透传。

---

## 附录：流程图对应

- 图 A（内联）：现状 stateless 执行流。
- 图 B（内联）：Plan C per-turn 融合流。
- 图 C（内联）：SIR 三层管道 + 信念回写流。
- 图 D（内联）：跨轮信念状态机（多轮如何收敛 / aside 如何被抗打断）。

> 本文未改任何代码，纯设计对比。下一步建议从 **P0（rules.py 信号增强）** 或 **P2（IntentState 跨轮信念）** 切入；P2 是满足用户硬需求的关键，建议优先排期。
