"""DST 引擎(纯函数, 测试友好) —— SOM-DST 范式落地(v2.2.x DST/SIR 重构)。

设计理念(来自 DST_SIR_REDESIGN_PLAN.md, 用户定义):
  **LLM 是写作者** —— 只产出 `SIRDelta`(本轮状态变化量);
  **代码是编辑 + 裁判** —— `apply_delta` 按 4 标准操作 + 4 冲突规则无歧义合并, 无 LLM。

本模块零 IO、零 LLM、无副作用, 仅依赖 `catalog.required_slots_of`(静态目录)。
可在无 Redis / 无网络的纯进程内直接跑单测。

核心成员:
  - `EMPTY_SIR` / `new_sir_root()`: SIR 根结构(替代旧 store._EMPTY 扁平结构)
  - `SIRDelta`(Pydantic v2, 强校验 LLM 输出合法性)
  - `apply_delta(old, delta, source)`: 唯一的状态变更入口(4 标准操作 + 4 冲突规则)
  - `compute_missing(sir, intent_id)`: 用目录算缺失槽(替代 LLM 给 missing_slots)
  - `derive_decision(sir, intent_id)`: pending 空 + 必填齐 → route, 否则 clarify
  - `build_sir_for_shortcut(...)`: 捷径分支构造确定性 SIR 根(零 LLM)

常量(单一来源, 与方案 §3/§6 完全一致):
  - SLOT_SOURCE_RANK: rule_strong > user_explicit > vector > llm_delta
  - CROSS_INTENT_SLOTS: 跨意图常驻的用户级偏好(意图切换不清空)
  - LOW_CONF: 低置信阈值(0.6) → 仅进 pending, 绝不覆盖
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field

from .catalog import required_slots_of


# ───────────────────────── 常量(单一来源) ─────────────────────────

SLOT_SOURCE_RANK: dict[str, int] = {
    "rule_strong": 4,     # 人工高精规则(scolding / 强信号)
    "user_explicit": 3,   # 用户在选项中显式选择 / 字段级直填
    "vector": 2,          # 向量召回 + 规则中等置信
    "llm_delta": 1,       # LLM 状态解析器产出的本轮变化量
}
"""来源优先级: 数字越大等级越高(可越过『等置信不覆盖』)。"""

CROSS_INTENT_SLOTS: set[str] = {"industry", "language", "theme", "tone"}
"""跨意图常驻槽: 用户级偏好, 意图切换(A→B)时不被清空。"""

LOW_CONF = 0.6
"""低置信阈值: conf < 0.6 的 UPDATE 只进 pending, 绝不覆盖已 confirmed 的槽。"""

# SIR 槽位 status 枚举(内部持久态)
SIR_STATUS_CONFIRMED = "confirmed"
SIR_STATUS_DONTCARE = "dontcare"
SIR_STATUS_DELETED = "deleted"
SIR_STATUS_PENDING = "pending"

# LLM 输出的 SIRDelta.slot.status 枚举(仅三种; 内部另增 pending)
DELTA_STATUS_CONFIRMED = "confirmed"
DELTA_STATUS_DONTCARE = "dontcare"
DELTA_STATUS_DELETED = "deleted"

# intent_stability 枚举
STABILITY_HIGH = "high"
STABILITY_MEDIUM = "medium"
STABILITY_LOW = "low"
STABILITY_UNSTABLE = "unstable"


# ───────────────────────── SIR 根结构 ─────────────────────────

def new_sir_root() -> dict:
    """返回全新的空 SIR 根(深拷贝, 调用方可安全修改)。

    替代旧 store _EMPTY 的扁平结构。内容 schema:
      meta.{active_intent, intent_stability, context_refs, memory_hints}
      slots: name -> {value, confidence, status[, source]}
      constraints: [{type, key, value}]
      pending: [slot_name]
    """
    return {
        "meta": {
            "active_intent": "",
            "intent_stability": STABILITY_UNSTABLE,
            "context_refs": [],
            "memory_hints": [],
        },
        "slots": {},
        "constraints": [],
        "pending": [],
        "updated_at": 0.0,
    }


# 兼容别名(给 store.py 直接引用)
EMPTY_SIR = new_sir_root()


def normalize_sir(sir: dict | None) -> dict:
    """把任意(可能老化/残缺/扁平)的持久化结构兜底归一化为完整 SIR 根。

    - 完全缺失 / None → 全新空 SIR
    - 老扁平结构 {intent_id, slots:{k:v}, clarify_rounds, confidence} → 包成新 schema
    - 缺字段的半新结构 → 补齐 meta / constraints / pending
    不产生脏状态, 仅保证下游 apply_delta / compute_missing 不 KeyError。
    """
    if not sir or not isinstance(sir, dict):
        return new_sir_root()

    # 老扁平结构判定: 有 intent_id 且无 meta → 归一化
    if "intent_id" in sir and "meta" not in sir:
        flat_slots = sir.get("slots") or {}
        new_slots: dict = {}
        if isinstance(flat_slots, dict):
            for k, v in flat_slots.items():
                new_slots[k] = {
                    "value": v,
                    "confidence": float(sir.get("confidence", 1.0) or 1.0),
                    "status": SIR_STATUS_CONFIRMED,
                    "source": "llm_delta",
                }
        return {
            "meta": {
                "active_intent": str(sir.get("intent_id", "") or ""),
                "intent_stability": STABILITY_UNSTABLE,
                "context_refs": [],
                "memory_hints": [],
            },
            "slots": new_slots,
            "constraints": [],
            "pending": [],
            "updated_at": float(sir.get("updated_at", 0.0) or 0.0),
        }

    # 半新结构: 补齐缺省
    meta = sir.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    slots = sir.get("slots")
    if not isinstance(slots, dict):
        slots = {}
    # 顺便规整每个槽的字段, 缺 source 补 llm_delta
    clean_slots: dict = {}
    for k, v in slots.items():
        if isinstance(v, dict):
            clean_slots[k] = {
                "value": v.get("value"),
                "confidence": float(v.get("confidence", 0.0) or 0.0),
                "status": v.get("status", SIR_STATUS_CONFIRMED),
                "source": v.get("source", "llm_delta"),
            }
        else:
            # 极老形态: 槽位是裸值
            clean_slots[k] = {
                "value": v, "confidence": 1.0,
                "status": SIR_STATUS_CONFIRMED, "source": "llm_delta",
            }
    return {
        "meta": {
            "active_intent": str(meta.get("active_intent", "") or ""),
            "intent_stability": meta.get("intent_stability", STABILITY_UNSTABLE),
            "context_refs": list(meta.get("context_refs", []) or []),
            "memory_hints": list(meta.get("memory_hints", []) or []),
        },
        "slots": clean_slots,
        "constraints": list(sir.get("constraints", []) or []),
        "pending": list(sir.get("pending", []) or []),
        "updated_at": float(sir.get("updated_at", 0.0) or 0.0),
    }


# ───────────────────────── SIRDelta(LLM 输出强校验) ─────────────────────────

class SlotSpec(BaseModel):
    """SIRDelta 中单个槽的 spec(LLM 产出)。"""
    model_config = ConfigDict(extra="allow")

    value: object = None
    confidence: float = 0.0
    status: str = DELTA_STATUS_CONFIRMED  # confirmed | dontcare | deleted

    def __init__(self, **data):
        # 规整 confidence 为 float 并夹在 [0,1]
        if "confidence" in data and data["confidence"] is not None:
            try:
                data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))
            except (TypeError, ValueError):
                data["confidence"] = 0.0
        super().__init__(**data)


class ConstraintSpec(BaseModel):
    """SIRDelta 中的约束项。value=None 表示删除该 (type,key) 约束。"""
    model_config = ConfigDict(extra="allow")

    type: str
    key: str
    value: object = None


class SIRDelta(BaseModel):
    """LLM 产出的『本轮状态变化量』, 代码侧用 Pydantic 强校验。

    LLM 不必输出 source / updated_at(代码打戳)。
    校验失败(枚举非法/类型错) → 调用方丢弃该 delta 并降级(保留 old_SIR, 记 warning), 不产生脏状态。
    """
    model_config = ConfigDict(extra="allow")

    meta: dict = Field(default_factory=dict)  # {active_intent, intent_stability}
    slots: dict[str, SlotSpec] = Field(default_factory=dict)
    constraints: list[ConstraintSpec] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)

    # ── 便利读取(容错) ──
    @property
    def active_intent(self) -> str:
        m = self.meta or {}
        return str(m.get("active_intent", "") or "")

    @property
    def intent_stability(self) -> str:
        m = self.meta or {}
        return str(m.get("intent_stability", "") or "")


_VALID_DELTA_STATUS = {DELTA_STATUS_CONFIRMED, DELTA_STATUS_DONTCARE, DELTA_STATUS_DELETED}


def parse_sir_delta(raw: dict | None):
    """把 LLM 原始 dict 解析为 SIRDelta; 失败返回 (None, error_msg)。

    校验内容:
      - raw 必须 dict
      - 每个 slot 的 status 必须是合法枚举(若为 deleted 由应用层转 DELETE)
      - constraints 每项须有 type/key
    """
    if raw is None or not isinstance(raw, dict):
        return None, "delta 不是 dict"
    try:
        slots_raw = raw.get("slots") or {}
        if not isinstance(slots_raw, dict):
            return None, "slots 不是 dict"
        constraints_raw = raw.get("constraints") or []
        if not isinstance(constraints_raw, list):
            return None, "constraints 不是 list"
        # 预校验 slot.status 枚举
        for name, spec in slots_raw.items():
            if not isinstance(spec, dict):
                return None, f"slot[{name}] 不是 dict"
            st = spec.get("status", DELTA_STATUS_CONFIRMED)
            if st not in _VALID_DELTA_STATUS:
                return None, f"slot[{name}].status 非法: {st!r}"
        delta = SIRDelta(
            meta=raw.get("meta") or {},
            slots=slots_raw,
            constraints=constraints_raw,
            pending=raw.get("pending") or [],
        )
        return delta, None
    except Exception as e:  # noqa: BLE001
        return None, f"delta 解析异常: {e}"


# ───────────────────────── 意图归属 ─────────────────────────

def slot_ownership(intent_id: str) -> set[str]:
    """意图拥有的槽集合(意图切换清槽的依据)。

    基线 = 目录声明的 required_slots(未来若目录补 optional_slots 一并并入)。
    配合 CROSS_INTENT_SLOTS 构成『意图切换时不被清空的槽』全集。
    """
    return set(required_slots_of(intent_id))


# ───────────────────────── 核心: apply_delta ─────────────────────────

def apply_delta(old: dict | None, delta: SIRDelta, source: str = "llm_delta") -> dict:
    """DST 唯一的状态变更入口: 把 SIRDelta 无歧义合并进旧 SIR。

    实现 SOM-DST 4 标准操作 + 4 冲突规则:
      操作: CARRYOVER(未输出即保留, 零代码) / UPDATE / DELETE / DONTCARE
      规则:
        ① 置信优先: 同来源内 new.conf >= old.conf 才覆盖
        ② 来源优先: rank_new < rank_old(低位)→ 跳过(高位可越级覆盖)
        ③ 意图切换清槽: active_intent 变化 → 删非新意图拥有且非跨意图的槽
        ④ 低置信只进 pending: conf < LOW_CONF → status=pending + 入 pending[], 绝不覆盖

    source: 本次 delta 的来源戳(rule_strong / user_explicit / vector / llm_delta)。
    """
    new = normalize_sir(old)           # 深拷贝 + 兜底归一化
    if source not in SLOT_SOURCE_RANK:
        source = "llm_delta"
    rank_new = SLOT_SOURCE_RANK[source]

    # ── 规则③ 意图切换清槽 ──
    a_new = delta.active_intent or new["meta"]["active_intent"]
    if a_new and a_new != new["meta"]["active_intent"]:
        owned = slot_ownership(a_new) | CROSS_INTENT_SLOTS
        for k in list(new["slots"].keys()):
            # delta 中本轮正在设置的槽不在此轮被清(避免『刚说又要删』)
            if k not in owned and k not in delta.slots:
                new["slots"].pop(k, None)
        new["meta"]["active_intent"] = a_new

    # intent_stability 透传
    stab = delta.intent_stability
    if stab in (STABILITY_HIGH, STABILITY_MEDIUM, STABILITY_LOW):
        new["meta"]["intent_stability"] = stab

    # ── 4 标准操作 ──
    for name, spec in delta.slots.items():
        val = spec.value
        conf = float(spec.confidence or 0.0)
        status = spec.status

        # DELETE: value=null 或 status=deleted(用户取消)
        if status == DELTA_STATUS_DELETED or val is None:
            new["slots"].pop(name, None)
            # DELETE 同时把该槽从 pending 里移除(已彻底作废)
            if name in new["pending"]:
                new["pending"] = [p for p in new["pending"] if p != name]
            continue

        # DONTCARE: 用户说「随便」 → 留 key, value=null, status=dontcare
        if status == DELTA_STATUS_DONTCARE:
            new["slots"][name] = {
                "value": None, "confidence": conf,
                "status": SIR_STATUS_DONTCARE, "source": source,
            }
            if name in new["pending"]:
                new["pending"] = [p for p in new["pending"] if p != name]
            continue

        # UPDATE(confirmed)
        cur = new["slots"].get(name) or {"confidence": 0.0, "status": SIR_STATUS_PENDING, "source": "llm_delta"}
        rank_old = SLOT_SOURCE_RANK.get(cur.get("source", "llm_delta"), 1)

        # 规则④ 低置信: 不覆盖, 只进 pending
        if conf < LOW_CONF:
            cur_slot = new["slots"].get(name)
            # 已 confirmed 的槽: 低置信提及『绝不覆盖』(硬规则), 也不强行置 pending
            # 避免对已确认信息反复追问。直接跳过。
            if cur_slot is not None and cur_slot.get("status") == SIR_STATUS_CONFIRMED:
                continue
            # 未确认 / 新槽 → 记录为 pending 候选(供 clarify 卡片回显『我猜你指 X, 确认?』)
            new["slots"][name] = {
                "value": val, "confidence": conf,
                "status": SIR_STATUS_PENDING, "source": source,
            }
            if name not in new["pending"]:
                new["pending"].append(name)
            continue

        # 规则② 来源优先: 新来源等级低于旧来源 → 跳过(高位可越级覆盖低位)
        if rank_new < rank_old:
            continue
        # 规则① 置信优先: 同/可越级来源内, 低置信不覆盖高置信
        if conf < float(cur.get("confidence", 0.0) or 0.0):
            continue

        new["slots"][name] = {
            "value": val, "confidence": conf,
            "status": SIR_STATUS_CONFIRMED, "source": source,
        }
        # 升级为 confirmed → 从 pending 移除
        if name in new["pending"]:
            new["pending"] = [p for p in new["pending"] if p != name]

    # ── constraints: upsert / delete(value=None) ──
    for c in delta.constraints:
        if c.value is None:
            new["constraints"] = [
                x for x in new["constraints"]
                if not (x.get("type") == c.type and x.get("key") == c.key)
            ]
        else:
            new["constraints"] = [
                x for x in new["constraints"]
                if not (x.get("type") == c.type and x.get("key") == c.key)
            ] + [{"type": c.type, "key": c.key, "value": c.value}]

    new["updated_at"] = time.time()
    return new


# ───────────────────────── 决策推导(替代 LLM 的 missing_slots / 追问判断) ─────────────────────────

def compute_missing(sir: dict, intent_id: str) -> list[str]:
    """用 catalog.required_slots_of 算缺失槽(替代 LLM 给 missing_slots)。

    某槽『不缺失』当且仅当其 status ∈ {confirmed, dontcare}。
    pending 状态视为仍缺失(需确认)。
    """
    sir = normalize_sir(sir)
    req = required_slots_of(intent_id)
    missing = []
    for s in req:
        st = sir["slots"].get(s, {}).get("status")
        if st not in (SIR_STATUS_CONFIRMED, SIR_STATUS_DONTCARE):
            missing.append(s)
    return missing


def derive_decision(sir: dict, intent_id: str) -> str:
    """pending 空 + 必填齐 → route; 否则 clarify。

    Rule-of-thumb: 只要存在待确认(pending)或缺失必填(confirmed/dontcare 之外),
    就不能直接执行, 必须 clarify 回问。
    """
    sir = normalize_sir(sir)
    if sir["pending"]:
        return "clarify"
    if compute_missing(sir, intent_id):
        return "clarify"
    return "route"


# ───────────────────────── 捷径分支构造(零 LLM) ─────────────────────────

def build_sir_for_shortcut(
    intent_id: str,
    *,
    confidence: float = 0.0,
    selected_skill: str | None = None,
    slots: dict | None = None,
    stability: str = STABILITY_UNSTABLE,
    context_refs: list | None = None,
    memory_hints: list | None = None,
) -> dict:
    """为捷径分支(选项选择 / delete / reset / 规则强信号 / super-fast / 新奇度 / PM 粘性等)
    构造确定性 SIR 根(不经过 LLM delta)。

    捷径分支要么清空状态(reset/selection/delete), 要么只置 active_intent(+ 已确知槽),
    用确定性根表达, 语义与 apply_delta 一致但不依赖 LLM。
    """
    sir = new_sir_root()
    sir["meta"]["active_intent"] = intent_id
    sir["meta"]["intent_stability"] = stability
    if context_refs is not None:
        sir["meta"]["context_refs"] = list(context_refs)
    if memory_hints is not None:
        sir["meta"]["memory_hints"] = list(memory_hints)
    if slots:
        for k, v in slots.items():
            if isinstance(v, dict):
                sir["slots"][k] = {
                    "value": v.get("value"),
                    "confidence": float(v.get("confidence", 1.0) or 1.0),
                    "status": v.get("status", SIR_STATUS_CONFIRMED),
                    "source": v.get("source", "rule_strong"),
                }
            else:
                sir["slots"][k] = {
                    "value": v, "confidence": 1.0,
                    "status": SIR_STATUS_CONFIRMED, "source": "rule_strong",
                }
    sir["updated_at"] = time.time()
    # selected_skill 仅作透传提示(下游 _emit_route 仍用 catalog.skill_for / run_tools 决定)
    if selected_skill:
        sir.setdefault("meta", {})["selected_skill"] = selected_skill
    return sir
