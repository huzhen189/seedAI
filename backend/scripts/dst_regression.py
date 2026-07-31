"""DST 引擎回归测试(SOM-DST 4 标准操作 × 4 冲突规则)。

纯函数、零 IO、零 LLM, 直接 python 运行:
  cd backend
  python scripts/dst_regression.py

全部断言需通过才算 Step 1 完成。覆盖:
  - 4 标准操作: CARRYOVER / UPDATE / DELETE / DONTCARE
  - 4 冲突规则: ① 置信优先 ② 来源优先 ③ 意图切换清槽 ④ 低置信只进 pending
  - 辅助: normalize_sir 老数据兜底 / compute_missing / derive_decision
"""

from __future__ import annotations

import os
import sys


# 允许以脚本方式直接运行(把 backend 加入 path)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.intent.dst import (  # noqa: E402
    CROSS_INTENT_SLOTS,
    DELTA_STATUS_CONFIRMED,
    DELTA_STATUS_DELETED,
    DELTA_STATUS_DONTCARE,
    SIR_STATUS_CONFIRMED,
    SIR_STATUS_DONTCARE,
    SIR_STATUS_PENDING,
    SIRDelta,
    apply_delta,
    build_sir_for_shortcut,
    compute_missing,
    derive_decision,
    new_sir_root,
    normalize_sir,
    parse_sir_delta,
    slot_ownership,
)


PASS = 0
FAIL = 0


def check(name: str, cond: bool):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  <-- FAILED")


def mk(old_slots: dict | None = None, active_intent: str = "", stability: str = "unstable") -> dict:
    """构造旧 SIR 用于测试。old_slots: name -> {value,confidence,status,source}。"""
    sir = new_sir_root()
    sir["meta"]["active_intent"] = active_intent
    sir["meta"]["intent_stability"] = stability
    if old_slots:
        sir["slots"] = {k: dict(v) for k, v in old_slots.items()}
    return sir


print("=" * 64)
print("DST 引擎回归测试")
print("=" * 64)

# ───────────── 操作 1: CARRYOVER(未输出 → 原值保留) ─────────────
print("\n[1] CARRYOVER —— 未输出的槽原值保留")
old = mk({"style": {"value": "简约", "confidence": 0.9, "status": SIR_STATUS_CONFIRMED, "source": "user_explicit"}})
d = SIRDelta(meta={}, slots={"goal": {"value": "博客", "confidence": 0.8, "status": DELTA_STATUS_CONFIRMED}})
n = apply_delta(old, d)
check("未输出的 style 仍保留confirmed", n["slots"].get("style", {}).get("value") == "简约")
check("新输出的 goal 已写入", n["slots"].get("goal", {}).get("value") == "博客")

# ───────────── 操作 2: UPDATE(覆盖) ─────────────
print("\n[2] UPDATE —— 标准更新(同来源 + 新置信更高)")
old2 = mk({"style": {"value": "简约", "confidence": 0.4, "status": SIR_STATUS_CONFIRMED, "source": "user_explicit"}})
d = SIRDelta(meta={}, slots={"style": {"value": "科技", "confidence": 0.8, "status": DELTA_STATUS_CONFIRMED}})
n = apply_delta(old2, d, source="user_explicit")
check("style 覆盖为新值", n["slots"]["style"]["value"] == "科技")
check("style status=confirmed", n["slots"]["style"]["status"] == SIR_STATUS_CONFIRMED)

# ───────────── 操作 3: DELETE(value=null) ─────────────
print("\n[3] DELETE —— value=null 删除槽")
d = SIRDelta(meta={}, slots={"style": {"value": None, "confidence": 0.9, "status": DELTA_STATUS_CONFIRMED}})
n = apply_delta(old, d)
check("style 被删除", "style" not in n["slots"])

# ───────────── 操作 3': DELETE(status=deleted) ─────────────
print("\n[3'] DELETE —— status=deleted 删除槽")
d = SIRDelta(meta={}, slots={"style": {"value": "x", "confidence": 0.9, "status": DELTA_STATUS_DELETED}})
n = apply_delta(old, d)
check("style 被删除(status=deleted)", "style" not in n["slots"])

# ───────────── 操作 4: DONTCARE(随便) ─────────────
print("\n[4] DONTCARE —— 用户说随便")
d = SIRDelta(meta={}, slots={"style": {"value": "随便", "confidence": 0.7, "status": DELTA_STATUS_DONTCARE}})
n = apply_delta(old, d)
check("style 留 key", "style" in n["slots"])
check("style value=None", n["slots"]["style"]["value"] is None)
check("style status=dontcare", n["slots"]["style"]["status"] == SIR_STATUS_DONTCARE)

# ───────────── 规则① 置信优先(同来源, 且达到阈值避免低置信分支干扰) ─────────────
print("\n[规则①] 置信优先 —— 同来源内低置信不覆盖高置信")
old = mk({"style": {"value": "简约", "confidence": 0.9, "status": SIR_STATUS_CONFIRMED, "source": "user_explicit"}})
d = SIRDelta(meta={}, slots={"style": {"value": "科技", "confidence": 0.7, "status": DELTA_STATUS_CONFIRMED}})
n = apply_delta(old, d, source="user_explicit")  # 同来源, 0.7≥LOW_CONF
check("同来源 0.7 不覆盖 0.9", n["slots"]["style"]["value"] == "简约")

# 同来源高置信覆盖
d = SIRDelta(meta={}, slots={"style": {"value": "科技", "confidence": 0.95, "status": DELTA_STATUS_CONFIRMED}})
n = apply_delta(old, d, source="user_explicit")
check("同来源 0.95 覆盖 0.9", n["slots"]["style"]["value"] == "科技")

# ───────────── 规则② 来源优先(异来源, 等置信) ─────────────
print("\n[规则②] 来源优先 —— 高位可越级覆盖低位(等置信)")
old = mk({"style": {"value": "简约", "confidence": 0.7, "status": SIR_STATUS_CONFIRMED, "source": "llm_delta"}})
# rule_strong(4) 覆盖 llm_delta(1), 即使等置信 0.7
d = SIRDelta(meta={}, slots={"style": {"value": "科技", "confidence": 0.7, "status": DELTA_STATUS_CONFIRMED}})
n = apply_delta(old, d, source="rule_strong")
check("rule_strong 越级覆盖 llm_delta(等置信)", n["slots"]["style"]["value"] == "科技")

# 低位不可覆盖高位(等置信)
old = mk({"style": {"value": "简约", "confidence": 0.7, "status": SIR_STATUS_CONFIRMED, "source": "rule_strong"}})
d = SIRDelta(meta={}, slots={"style": {"value": "科技", "confidence": 0.7, "status": DELTA_STATUS_CONFIRMED}})
n = apply_delta(old, d, source="llm_delta")
check("llm_delta 不可覆盖 rule_strong(等置信)", n["slots"]["style"]["value"] == "简约")

# ───────────── 规则③ 意图切换清槽(保留 CROSS_INTENT_SLOTS) ─────────────
print("\n[规则③] 意图切换清槽 —— A→B 删非拥有 + 保留跨意图常驻槽")
old = mk(
    {
        "style": {"value": "简约", "confidence": 0.9, "status": SIR_STATUS_CONFIRMED, "source": "user_explicit"},
        "industry": {"value": "edu", "confidence": 0.9, "status": SIR_STATUS_CONFIRMED, "source": "user_explicit"},
        "goal": {"value": "旧目标", "confidence": 0.9, "status": SIR_STATUS_CONFIRMED, "source": "user_explicit"},
    },
    active_intent="build_site",
)
# build_site 拥有 goal/pages/style 等但不拥有 industry(跨意图) 也不拥有 theme/tone/language
d = SIRDelta(meta={"active_intent": "build_modify"}, slots={})
n = apply_delta(old, d)
check("意图切换 active_intent 更新", n["meta"]["active_intent"] == "build_modify")
check("industry(跨意图)保留", n["slots"].get("industry", {}).get("value") == "edu")
# 确定性断言: style 的归属取决于 build_modify 是否拥有, 故只验证『未被低低位覆盖』式保留逻辑——
# 这里 build_site 拥有 style, 若 build_modify 不拥有则被清; 用归属集合推导期望而非硬编码。
owned_modify = slot_ownership("build_modify")
expected_style_kept = ("style" in owned_modify) or ("style" in CROSS_INTENT_SLOTS)
check("style 按归属决定保留/清除", ("style" in n["slots"]) == expected_style_kept)
# goal 是 build_site 拥有但 build_modify 未必拥有 → 按归属推导
expected_goal_kept = ("goal" in owned_modify) or ("goal" in CROSS_INTENT_SLOTS)
check("goal 按归属决定保留/清除", ("goal" in n["slots"]) == expected_goal_kept)
check("industry 始终保留(不被清)", "industry" in n["slots"])

# ───────────── 规则④ 低置信只进 pending ─────────────
print("\n[规则④] 低置信只进 pending —— 不覆盖已 confirmed 槽")
old = mk({"style": {"value": "简约", "confidence": 0.9, "status": SIR_STATUS_CONFIRMED, "source": "user_explicit"}})
d = SIRDelta(meta={}, slots={"style": {"value": "科技", "confidence": 0.3, "status": DELTA_STATUS_CONFIRMED}})
n = apply_delta(old, d, source="llm_delta")
check("低置信 0.3 不覆盖已confirmed槽", n["slots"]["style"]["value"] == "简约")
check("低置信不改动原 confirmed 槽", n["slots"]["style"]["status"] == SIR_STATUS_CONFIRMED)
check("低置信不把已confirmed槽塞进pending", "style" not in n["pending"])
# 低置信新槽 → 进 pending
d2 = SIRDelta(meta={}, slots={"pages": {"value": "首页", "confidence": 0.4, "status": DELTA_STATUS_CONFIRMED}})
n2 = apply_delta(new_sir_root(), d2, source="llm_delta")
check("低置信新槽 status=pending", n2["slots"]["pages"]["status"] == SIR_STATUS_PENDING)
check("低置信新槽进 pending 列表", "pages" in n2["pending"])

# ───────────── 4 规则组合: 来源高位 + 低置信 → 仍受 pending 约束 ─────────────
print("\n[组合] 来源高位(rule_strong) + 低置信 0.3 → 仍进 pending")
d = SIRDelta(meta={}, slots={"style": {"value": "科技", "confidence": 0.3, "status": DELTA_STATUS_CONFIRMED}})
n = apply_delta(new_sir_root(), d, source="rule_strong")
check("rule_strong 低置信仍 pending", n["slots"]["style"]["status"] == SIR_STATUS_PENDING)
check("rule_strong 低置信进 pending", "style" in n["pending"])

# ───────────── constraints upsert / delete ─────────────
print("\n[constraints] upsert 与 delete(value=None)")
old = mk()
old["constraints"] = [{"type": "exclude", "key": "color", "value": "red"}]
d = SIRDelta(meta={}, slots={}, constraints=[{"type": "exclude", "key": "color", "value": "blue"}])
n = apply_delta(old, d)
check("constraint 同 key 覆盖", n["constraints"] == [{"type": "exclude", "key": "color", "value": "blue"}])
d2 = SIRDelta(meta={}, slots={}, constraints=[{"type": "exclude", "key": "color", "value": None}])
n2 = apply_delta(n, d2)
check("constraint value=None 删除", n2["constraints"] == [])

# ───────────── normalize_sir 老扁平兜底 ─────────────
print("\n[normalize_sir] 老扁平数据兜底")
legacy = {"intent_id": "build_site", "slots": {"style": "简约"}, "clarify_rounds": 0, "confidence": 1.0, "updated_at": 123.0}
n = normalize_sir(legacy)
check("老数据 active_intent 映射", n["meta"]["active_intent"] == "build_site")
check("老数据裸值包成 confirmed", n["slots"]["style"]["status"] == SIR_STATUS_CONFIRMED)
check("老数据无 meta 不抛错", "meta" in n)
check("None → 全新空 SIR", normalize_sir(None)["meta"]["active_intent"] == "")

# ───────────── compute_missing / derive_decision ─────────────
print("\n[compute_missing / derive_decision]")
# build_site 需 goal/pages/style
sir_full = build_sir_for_shortcut("build_site", slots={
    "goal": "博客", "pages": ["首页", "列表"], "style": "简约",
})
sir_partial = build_sir_for_shortcut("build_site", slots={"goal": "博客"})
check("满槽 → 无缺失", compute_missing(sir_full, "build_site") == [])
miss = compute_missing(sir_partial, "build_site")
check("缺 pages/style", set(miss) == {"pages", "style"})
check("满槽 → route", derive_decision(sir_full, "build_site") == "route")
check("缺槽 → clarify", derive_decision(sir_partial, "build_site") == "clarify")
# pending 驱动 clarify
sir_pending = apply_delta(new_sir_root(), SIRDelta(meta={"active_intent": "build_site"},
    slots={"style": {"value": "简约", "confidence": 0.3, "status": DELTA_STATUS_CONFIRMED}}), source="llm_delta")
check("有 pending → clarify", derive_decision(sir_pending, "build_site") == "clarify")

# ───────────── parse_sir_delta 校验 ─────────────
print("\n[parse_sir_delta] 非法输入校验")
ok, err = parse_sir_delta({"meta": {"active_intent": "build_site"},
    "slots": {"style": {"value": "x", "confidence": 0.8, "status": "confirmed"}}})
check("合法 delta 解析成功", ok is not None and err is None)
bad, err = parse_sir_delta({"slots": {"style": {"status": "bogus"}}})
check("非法 status 被拒", bad is None and err is not None)
none_delta, err = parse_sir_delta(None)
check("None 被拒", none_delta is None)

# ───────────── 结果 ─────────────
print("\n" + "=" * 64)
print(f"DST 回归结果: PASS={PASS}  FAIL={FAIL}")
print("=" * 64)
if FAIL:
    sys.exit(1)
print("ALL PASS ✅")
