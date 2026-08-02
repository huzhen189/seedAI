# DST / SIR 重构方案（基于现有代码 + IntentSlots 表）

> 目标：把当前的「LLM 顺带做理解与随意 merge」改成「LLM 只产出 SIR_delta（本轮变化量），DST 按 SOM-DST 4 标准操作 + 4 冲突规则无歧义合并」。
> 核心理念（用户定义）：**LLM 是写作者，代码是编辑 + 裁判。**

---

## 0. 现有代码与新方案的落差（必须先看清）

| 现有行为 | 文件/行 | 问题 |
| --- | --- | --- |
| `IntentSlots.slots` 列存扁平 dict：`{"intent_id","slots":{k:v},"clarify_rounds","confidence","updated_at"}` | `shared/models.py:294` / `store.py:174 _EMPTY` | 每槽**无** confidence/status，无法做「低置信不覆盖高置信」 |
| `_llm_rule` 一个 prompt 同时产出 intent_id / confidence / industry / collected_slots / missing_slots / questions / options / reason | `cascade.py:90-110,137-224` | LLM 既「理解意图」又「抽取槽位」又「规划澄清」，职责混在一起 |
| merge 逻辑就是 `merged = dict(prior); merged.update(ruling.collected_slots)` | `cascade.py:651-652` | **ad-hoc `update()`**，无置信优先、无来源优先、无意图切换清槽、低置信直接覆盖 |
| `missing_slots` 由 LLM 给 | `cascade.py:188,654` | LLM 可能漏给/乱给；且目录已能算（`required_slots_of`） |
| 捷径分支直接 `save_slots({...旧形状...})` | `cascade.py:419,485,526,617,703` | 写的是旧扁平结构，重构后全部要换签名 |

**结论**：DST 引擎 + SIR_delta schema + slot 结构化是新增，分类器（向量+规则+LLM 终判选 intent）**保留**作为「来源优先级」的强信号，不丢。

---

## 1. 一、职责切分（落地到函数级）

```
用户输入
  └─ classify_v3 预分类（保留）：向量召回 + 规则强信号 → 候选 active_intent + 来源/置信
       └─ _extract_sir_delta(LLM, 新调用)：只产出 SIR_delta（本轮对「状态」的理解）
            └─ DST.apply_delta(old_SIR, SIR_delta) → new_SIR   ← 纯规则，无 LLM
                 └─ 持久化/召回：store.load_sir / save_sir（Redis 热 + MySQL 冷）
                      └─ 路由/worker/skill/Memory/Tool 读 new_SIR
```

| 角色 | 负责 | 不做 |
| --- | --- | --- |
| **LLM** | 抽/更新 SIR 字段、判 `intent_stability`、填 `context_refs`、`memory_hints`、给 `SIR_delta` | 不直接决定「要不要追问」「调哪个 skill 执行」（那是代码） |
| **代码（DST + cascade）** | 校验 SIR_delta 合法性（类型/枚举）、执行 4 标准操作 + 4 冲突规则、驱动 Workflow（pending 空才执行）、算 `missing`（用 catalog）、持久化 | 不替 LLM「猜」用户没说的槽 |

---

## 2. 二、SOM-DST 4 种标准操作（DST 唯一允许的状态变更原语）

> 每个 **slot** 本轮必属其一，无 ad-hoc 分支。

| 操作 | 触发（来自 SIR_delta） | DST 行为 |
| --- | --- | --- |
| **CARRYOVER** | LLM **不输出**该槽（用户没提） | 原值保留（默认，零代码） |
| **UPDATE** | 输出 `{value, confidence, status:confirmed|set}` | 写入/覆盖该槽（受规则①/②约束） |
| **DELETE** | `value=null` 或 `status:deleted`（用户取消） | 从 SIR 删除该槽 |
| **DONTCARE** | `status:dontcare`（用户说「随便」） | 槽位保留 key，value=null，status=dontcare |

代码里是一个 `for name, spec in delta.slots.items():` 的 4 分支 `if/elif`，**不存在第五种处理方式**。

---

## 3. 三、4 条核心冲突解决规则（写进 `DST.apply_delta`）

- **规则① 置信度优先**：UPDATE 仅当 `new.conf >= old.conf`（同来源内）；高置信不被低置信覆盖。
- **规则② 来源优先级**：每个槽带 `source`（代码打戳，非 LLM 给）。优先级 `rule_strong > user_explicit > vector > llm_delta`。来源等级高者可越过「等置信不覆盖」（即规则①只在**同来源**内生效）。
- **规则③ 意图切换清空无关槽**：若 `meta.active_intent` 由 A 变 B，删除所有「非 B 拥有」且「非跨意图常驻槽」的槽。`CROSS_INTENT_SLOTS = {"industry","language","theme","tone"}`（用户级偏好跨意图保留）。
- **规则④ 低置信只进 pending，不覆盖**：`conf < 0.6` → 即便输出也写入 `status:"pending"` 并加入 `SIR.pending[]`，**绝不**覆盖已 confirmed 的槽；`pending` 非空 → cascade 决策为 `clarify`。

---

## 4. 四、LLM 输出 SIR_delta 的 Schema + System Prompt

### 4.1 JSON Schema（代码侧用 Pydantic 强校验）

```json
{
  "meta": { "active_intent": "build_site", "intent_stability": "high|medium|low" },
  "slots": {
    "<slot_name>": { "value": "<any|null>", "confidence": 0.0, "status": "confirmed|dontcare|deleted" }
  },
  "constraints": [ { "type": "exclude|include|limit", "key": "<str>", "value": "<any|null>" } ],
  "pending": [ "<slot_name>" ]
}
```

- `value=null` 等价于 `status:deleted`。
- LLM **不必**输出 `source` / `updated_at`（代码打戳）。
- 校验失败（枚举非法/类型错）→ 代码丢弃该 delta 并降级（保留 old_SIR，记 warning），**不产生脏状态**。

### 4.2 System Prompt（可基本照用户原文）

```
你是一个对话状态解析器。请根据用户输入，输出 SIR_delta（本轮的变化量），不要输出完整 SIR。
输出字段：
- meta.active_intent
- slots: {slot_name: {value, confidence, status}}
- constraints: [{type, key, value}]
- pending: [slot_name]
规则：
1. 用户没提的 slot 不要输出（CARRYOVER）
2. 用户取消 → slot=null（DELETE）
3. 用户说"随便" → status=dontcare
4. 低置信(<0.6) → 仍输出，但会被放进 pending
5. 只输出 JSON，不要解释。
```
> 注入时由代码把「当前最可能 active_intent = X」「已有 SIR 槽位快照」「本意图 required_slots」作为强先验塞进 user prompt，使 LLM 专注「状态理解」而非重新分类。

### 4.3 与现有 `_llm_rule` 的关系

- **保留** `_llm_rule` 的 intent 选择部分（它从候选选 intent_id + confidence + industry），用以**生成 `meta.active_intent` 先验**与「来源=vector/llm 的强信号」。
- **删除** `_llm_rule` 输出 `collected_slots` / `missing_slots` / `questions` / `options` 的槽位职责（这些改由 DST + catalog 算）。保留 `options`/`questions` 仅用于「意图级澄清」，槽位级追问由 `missing + pending` 推导。
- 新增 **`_extract_sir_delta`**：传入 (user_msg, prior_sir, active_intent_candidate, catalog_slots)，返回校验后的 `SIRDelta`。仅在「需要理解」的路径调用（LLM 终判路径 + PM 粘性路径）；规则/向量 super-fast/选项选择 等捷径路径直接构造确定性 SIR_delta，省一次 LLM。

---

## 5. IntentSlots 数据表落地（关键！）

列结构**不变**（仍是 `slots: JSON`），只改 JSON **内容 schema**：

```python
# store.py 新 _EMPTY（SIR 根结构）
_EMPTY = {
    "meta": {
        "active_intent": "",            # 原 intent_id
        "intent_stability": "unstable", # high|medium|low
        "context_refs": [],             # 引用的历史消息/产物版本
        "memory_hints": [],             # 交给 Memory 模块的持久化提示
    },
    "slots": {},                        # name -> {value, confidence, status}  (status: confirmed|dontcare|deleted|pending)
    "constraints": [],                  # [{type,key,value}]
    "pending": [],                      # [slot_name]  低置信/待确认
    "updated_at": 0.0,
}
```

- **迁移**：因现有 `slots` 是扁平 `{k:v}`，`load_sir()` 做一次性归一化（老行 → 包成 `{slots:{k:{value:v,confidence:1.0,status:"confirmed"}}, meta:{active_intent:旧intent_id}}`）。配合 `reset_all.py` 清空即可彻底换型；线上存量行由归一化兜底，**不阻塞主流程**。
- `store.py` 接口改名（保留兼容薄包装）：`load_slots/save_slots/reset_slots` → `load_sir/save_sir/reset_sir`，内部仍走 `intent:slots:{conv_id}`(Redis) + `intent_slots`(MySQL) 双写，逻辑不变。

---

## 6. 新增模块 `intent/dst.py`（DST 引擎，纯函数 + 测试友好）

```python
# 伪代码骨架
SLOT_SOURCE_RANK = {"rule_strong":4, "user_explicit":3, "vector":2, "llm_delta":1}
CROSS_INTENT_SLOTS = {"industry","language","theme","tone"}
LOW_CONF = 0.6

def apply_delta(old: dict, delta: SIRDelta, source: str = "llm_delta") -> dict:
    new = deepcopy(old)
    # —— 规则③ 意图切换清槽 ——
    a_new = delta.meta.active_intent or new["meta"]["active_intent"]
    if a_new and a_new != new["meta"]["active_intent"]:
        owned = slot_ownership(a_new) | CROSS_INTENT_SLOTS
        for k in list(new["slots"]):
            if k not in owned and k not in delta.slots:
                new["slots"].pop(k, None)
        new["meta"]["active_intent"] = a_new
    if delta.meta.intent_stability:
        new["meta"]["intent_stability"] = delta.meta.intent_stability
    # —— 4 标准操作 ——
    for name, spec in delta.slots.items():
        val, conf, status = spec.value, spec.confidence, spec.status
        if status == "deleted" or val is None:               # DELETE
            new["slots"].pop(name, None); continue
        if status == "dontcare":                             # DONTCARE
            new["slots"][name] = {"value": None, "confidence": conf, "status": "dontcare"}; continue
        # UPDATE
        cur = new["slots"].get(name, {"confidence": 0.0, "status": "pending"})
        rank_new = SLOT_SOURCE_RANK[source]; rank_old = SLOT_SOURCE_RANK[cur.get("source","llm_delta")]
        if conf < LOW_CONF:                                  # 规则④ 低置信
            new["slots"][name] = {"value": val, "confidence": conf, "status": "pending", "source": source}
            if name not in new["pending"]: new["pending"].append(name)
            continue
        if rank_new < rank_old: continue                     # 规则② 来源优先
        if conf < cur["confidence"]: continue                # 规则① 置信优先
        new["slots"][name] = {"value": val, "confidence": conf, "status": "confirmed", "source": source}
        new["pending"] = [p for p in new["pending"] if p != name]
    # —— constraints ——
    for c in delta.constraints:
        if c.value is None:
            new["constraints"] = [x for x in new["constraints"] if not (x.type==c.type and x.key==c.key)]
        else:
            new["constraints"] = [x for x in new["constraints"] if not (x.type==c.type and x.key==c.key)] + [c]
    new["updated_at"] = time.time()
    return new

def compute_missing(sir: dict, intent_id: str) -> list[str]:
    """用 catalog.required_slots_of 算缺失，替代 LLM 给 missing_slots"""
    req = required_slots_of(intent_id)
    return [s for s in req if sir["slots"].get(s, {}).get("status") not in ("confirmed","dontcare")]

def derive_decision(sir: dict, intent_id: str) -> str:
    """pending 空 + 必填齐 → route；否则 clarify"""
    if sir["pending"]: return "clarify"
    if compute_missing(sir, intent_id): return "clarify"
    return "route"
```

- `slot_ownership(intent_id)`：以 `catalog.required_slots_of(intent_id)` 为基 + 允许自由槽（用户说啥都记，不丢）。
- **单测**：4 操作 × 4 冲突规则 = 16~20 条断言，`scripts/` 下加 `dst_regression.py`。

---

## 7. cascade.py 改造清单（最小侵入）

| 位置 | 改动 |
| --- | --- |
| `_llm_rule` | 删除 `collected_slots`/`missing_slots` 产出；保留 intent 选择 |
| `_classify_segment` 步⑨后 | 旧 `merged = dict(prior); merged.update(...)` → 改为 `delta = await _extract_sir_delta(...)`;`new_sir = apply_delta(prior_sir, delta)` |
| 步⑩ 门控 | 旧 `still_missing` → `compute_missing(new_sir, intent_id)`；决策加入 `derive_decision(new_sir, intent_id)`（pending 驱动） |
| 捷径分支（selection/delete/reset/ctx_gate/PM粘性/建站启发/super-fast/novelty） | `save_slots({旧形状})` 全部换成 `save_sir(build_sir_for_shortcut(...))`；构造确定性 SIR_delta（如 reset → 清空，PM → `meta.active_intent=build_requirement`） |
| `[11] 持久化` | `save_sir(new_sir)` |
| `memory_hints`/`context_refs` | 取出后异步喂给 Memory 模块（向量库写入 user/project 偏好）|

---

## 8. Workflow 驱动（规则「pending 空才执行」）

- **Worker / runner** 在真正执行 skill 前：`assert new_sir["pending"] == []`，否则降级为 `clarify` 回问 pending 槽。
- **前端**：从 SIR 读 `pending` 渲染「待确认」卡片；`confirmed` 槽渲染为已收集摘要，实现「进度可见」。

---

## 9. 风险与权衡

| 风险 | 缓解 |
| --- | --- |
| 每轮多一次 LLM（SIR_delta）→ 延迟/成本 | 仅 LLM 终判 + PM 路径调用；规则/super-fast/selection 捷径走确定性 delta，零 LLM |
| `chat_casual` 无槽位也要跑 delta | active_intent=chat 时跳过 delta 调用，直接空 SIR |
| 老数据扁平结构 | `load_sir` 归一化兜底 + `reset_all.py` 可选清表 |
| intent_stability 影响澄清频次 | 初版仅 `low` 触发「意图级澄清」，不影响槽位级 |
| 回归测试 | 新增 `dst_regression.py`（16+ 断言）+ 旧 `run_tests.py` 不破 |

---

## 10. 落地顺序（建议）

1. `intent/dst.py` + Pydantic `SIRDelta` + `dst_regression.py`（先过单测，无依赖）
2. `store.py` 改 `_EMPTY` + `load_sir/save_sir` + 归一化；`reset_all.py` 支持清表
3. `intent/sir_prompt.py` + `_extract_sir_delta`
4. `cascade.py` 接 DAG：删旧 merge、接 apply_delta、捷径分支换签名
5. Worker/runner 加 `pending` 门控
6. `run_tests.py` 全量回归 + 5 条 SSE 探针复跑
7. 写 `docs`（用户明确要求时）

> 注：本次仅输出方案，未改动源码。代码改动按上面顺序落地前需再确认「是否先 reset 库」。
