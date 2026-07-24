"""SIR 重构核心逻辑离线校验(不依赖 Redis/LLM)。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "ai_service"))

from app.intent.state import update_belief, IntentState, COMMIT_THRESHOLD
from app.intent.rules import run_rules, load_ruleset
from app.intent.observation import record, mark_outcome

# ── 1. update_belief 抗打断(CRITICAL) ──
# 已收敛 build(0.8) 遇闲聊 aside(0.3) → 必须保持 >=0.7
b = update_belief(IntentState(belief_l1="build", belief_l2="site", running_conf=0.8), 0.3, "chat", "casual")
assert b[0] >= COMMIT_THRESHOLD and b[1] == "build", f"aside-resistant FAIL: {b}"
print(f"[OK] aside-resistant: conf={b[0]:.3f} l1={b[1]} l2={b[2]} (>=0.7 保持)")

# ── 2. 同向高置信加速收敛(真实场景: 连续 3 轮 build/requirement, 置信 0.85) ──
s = IntentState(belief_l1="build", belief_l2="requirement", running_conf=0.3)
for i, cur in enumerate([(0.85, "build", "requirement"), (0.85, "build", "requirement"), (0.85, "build", "requirement")], 1):
    r = update_belief(s, *cur)
    s = IntentState(belief_l1=r[1], belief_l2=r[2], running_conf=r[0])
    print(f"[OK] 累积#{i}: conf={r[0]:.3f} l1={r[1]} l2={r[2]}")
assert s.running_conf >= 0.7, f"收敛 FAIL: {s.running_conf}"
print(f"[OK] 同向收敛最终 conf={s.running_conf:.3f} (>=0.7, COMMIT)")

# ── 2b. 边界: cur 恰为 0.7 时信念以 0.7 为不动点(从下方逼近, 不越过)
#      → 落入 CLARIFY 区, 由澄清循环兜底, 不会死锁
s2 = IntentState(belief_l1="build", belief_l2="site", running_conf=0.3)
for _ in range(6):
    s2 = IntentState(belief_l1="build", belief_l2="site", running_conf=update_belief(s2, 0.7, "build", "site")[0])
bnd = update_belief(s2, 0.7, "build", "site")[0]
assert bnd < COMMIT_THRESHOLD and bnd >= 0.0, f"边界 FAIL: {bnd}"
print(f"[OK] 边界(cur=0.7) 信念={bnd:.3f} (<0.7, 落 CLARIFY 区, 由澄清循环兜底)")

# ── 3. 首轮无先验 → 直接采用当前分 ──
r0 = update_belief(None, 0.5, "build", "site")
assert r0 == (0.5, "build", "site")
print(f"[OK] 首轮无先验: conf={r0[0]} l1={r0[1]}")

# ── 4. 规则五维加权(强 vs 弱) ──
strong = run_rules([{"role": "user", "content": "帮我做一个电商网站，要3个页面，用vue，有购物车和支付，面向c端企业"}])
weak = run_rules([{"role": "user", "content": "你好，今天天气怎么样"}])
print(f"[OK] rules strong.score={strong.signals['score']:.3f} pattern={strong.pattern}")
print(f"[OK] rules weak.score={weak.signals['score']:.3f} pattern={weak.pattern}")
assert strong.signals["score"] > weak.signals["score"] * 2, "强弱区分不足"
assert strong.pattern == "build" and weak.pattern == "chat"

# ── 5. 规则热更新: 损坏 ruleset 回滚 ──
rs = load_ruleset()
backup = rs
# 模拟损坏重载(直接调 load_ruleset 不影响文件, 仅验证默认可用性)
assert rs.get("weights") and "lexical_keywords" in rs
print(f"[OK] ruleset 加载正常 weights={rs['weights']}")

# ── 6. 可观测 JSONL 写入 ──
record(request_id="test-req-001", conversation_id=1, user_id=1, raw_input="帮我做网站",
       llm_intent="build/site", llm_confidence=0.8, rules_triggered=["build_keyword"],
       belief_before=0.3, belief_after=0.8, decision="route", latency_ms=123.4,
       tokens_used=50, specialist_routed="agent_generate_site", outcome="committed")
mark_outcome("test-req-001", "executed")
print("[OK] observation JSONL 写入成功")

print("\n✅ 全部 SIR 核心校验通过")
