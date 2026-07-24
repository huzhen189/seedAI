"""混合级联 v1.2.0 离线校验(不依赖真实 Chroma/Redis/LLM, 可全环境运行)。

通过 stub 重型依赖(langchain_openai/httpx/redis/chromadb)使模块可导入, 并对
classify_v3 的 LLM 调用做 monkeypatch, 覆盖:
  T1 目录: 14 意图 / 单一来源 / 技能白名单
  T2 规则: 强信号直路由 + 中信号
  T3 向量: 离线 bigram 召回 top1 正确
  T4 槽位: 跨轮存取 / 重置(强制进程内兜底)
  T5 级联 super-fast: 强规则+向量对齐 → 跳过 LLM 直接路由
  T6 级联 新奇度兜底: top5 全低 → 闲聊
  T7 级联 LLM 终判 + 澄清门控: 缺槽位 → clarify(≤2 轮)

运行: python scripts/cascade_validation.py
"""

import os
import sys
import types

# ── 1) stub 重型依赖, 让 cascade/providers 等模块可导入(真实依赖在运行环境已装) ──
_ROOT = os.path.join(os.path.dirname(__file__), "..", "backend", "ai_service")
sys.path.insert(0, os.path.abspath(_ROOT))

for _name in ("langchain_openai", "httpx", "redis", "chromadb", "langchain"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)
# langchain_openai.ChatOpenAI / httpx.Timeout 必须存在(providers 顶层引用)
sys.modules["langchain_openai"].ChatOpenAI = object
sys.modules["httpx"].Timeout = object

# pydantic_settings(config.py 顶层引用) —— 用极简替身, 仅满足 class 属性默认值
if "pydantic_settings" not in sys.modules:
    _ps = types.ModuleType("pydantic_settings")
    _ps.BaseSettings = object
    _ps.SettingsConfigDict = dict
    sys.modules["pydantic_settings"] = _ps

# 强制 store 走进程内兜底(无 redis)
import app.analytics as _analytics
_analytics._get_redis = lambda: None  # type: ignore

from app.intent.catalog import get_intent, intent_list, skill_whitelist  # noqa: E402
from app.intent.rulesmatcher import match_rules  # noqa: E402
from app.intent.vector_store import retrieve_intents  # noqa: E402
from app.intent.store import load_slots, reset_slots, save_slots  # noqa: E402
from app.intent import cascade  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK] {name} {detail}")
    else:
        print(f"[FAIL] {name} {detail}")
        failures.append(name)


# ── T1 目录 ──
intents = intent_list()
ids = [i["id"] for i in intents]
check("T1-目录数量=14", len(intents) == 14, f"(实际 {len(intents)})")
check("T1-意图id唯一", len(ids) == len(set(ids)))
wl = skill_whitelist()
check("T1-技能白名单含 agent_generate_site", "agent_generate_site" in wl, f"({wl})")
check("T1-get_intent(build_site)", get_intent("build_site") is not None)


# ── T2 规则 ──
r1 = match_rules("帮我生成一个网站")
check("T2-强规则 build_site", bool(r1) and r1[0].intent_id == "build_site" and r1[0].strength == "strong",
      f"(命中 {[ (h.intent_id,h.strength) for h in r1] })")
r2 = match_rules("解释一下什么是闭包")
check("T2-中规则 chat_explain", bool(r2) and r2[0].intent_id == "chat_explain" and r2[0].strength == "medium",
      f"(命中 {[ (h.intent_id,h.strength) for h in r2] })")


# ── T3 向量离线召回 ──
v1 = retrieve_intents("帮我生成一个企业官网", top_k=5)
check("T3-向量top1=build_site", bool(v1) and v1[0]["intent_id"] == "build_site",
      f"(top={v1[0]['intent_id']} score={v1[0]['score']:.2f})" if v1 else "(空)")
v2 = retrieve_intents("你好呀", top_k=5)
check("T3-向量 闲聊 命中 chat_casual", bool(v2) and v2[0]["intent_id"] == "chat_casual",
      f"(top={v2[0]['intent_id']})" if v2 else "(空)")


# ── T4 槽位存取 ──
reset_slots(99001)
save_slots(99001, {"intent_id": "build_site", "slots": {"pages": "3"}, "clarify_rounds": 1, "confidence": 0.6})
loaded = load_slots(99001)
check("T4-槽位保存/读取", loaded.get("intent_id") == "build_site" and loaded.get("slots", {}).get("pages") == "3",
      f"({loaded})")
reset_slots(99001)
check("T4-槽位重置", load_slots(99001).get("intent_id") == "")


# ── monkeypatch LLM(供 T5/T6/T7 使用) ──
_SCRIPT = {"json": None}  # 由测试设置脚本化 ruling


class _FakeModel:
    async def ainvoke(self, msgs):
        content = _SCRIPT["json"] or '{"intent_id":"chat_casual","confidence":0.4}'
        return types.SimpleNamespace(content=content)


cascade.get_chat_model = lambda *a, **k: _FakeModel()  # type: ignore
cascade.resolve_fallback_order = lambda *a, **k: ["stub"]  # type: ignore


async def _run():
    # ── T5 super-fast 直通(强规则+向量对齐 → 跳过 LLM) ──
    res = await cascade.classify_v3(
        [{"role": "user", "content": "帮我生成一个网站"}],
        conversation_id=99002,
    )
    check("T5-superfast 路由 build_site", res.decision == "route" and res.selected_skill == "agent_generate_site",
          f"(decision={res.decision} skill={res.selected_skill})")
    check("T5-未调用LLM(source=superfast)", res.evidence.get("source") == "superfast",
          f"(source={res.evidence.get('source')})")

    # ── T6 新奇度兜底(无规则 + 向量全低 → 闲聊, 不调LLM) ──
    res6 = await cascade.classify_v3(
        [{"role": "user", "content": "zxqwlkasjdhf 随机乱码测试"}],
        conversation_id=99003,
    )
    check("T6-新奇度兜底 chat", res6.decision == "route" and res6.selected_skill == "agent_chat",
          f"(decision={res6.decision} skill={res6.selected_skill})")
    check("T6-source=novelty", res6.evidence.get("source") == "novelty",
          f"(source={res6.evidence.get('source')})")

    # ── T7 LLM 终判 + 澄清门控(缺槽位 → clarify) ──
    # 用 "建一个商品售卖网页": 无强规则命中 + 向量 top≥0.45 + 非聊天 → 走 LLM 终判
    _SCRIPT["json"] = (
        '{"intent_id":"build_site","confidence":0.6,"industry":"corp",'
        '"missing_slots":["pages","style"],"collected_slots":{},'
        '"questions":["需要几个页面?","希望什么风格?"],"reason":"缺关键参数"}'
    )
    res7 = await cascade.classify_v3(
        [{"role": "user", "content": "建一个商品售卖网页"}],
        conversation_id=99004,
    )
    check("T7-缺槽位→clarify", res7.decision == "clarify", f"(decision={res7.decision})")
    check("T7-澄清问题非空", bool(res7.clarify_questions), f"(qs={res7.clarify_questions})")
    check("T7-澄清轮次=1", res7.clarify_rounds == 1, f"(rounds={res7.clarify_rounds})")

    # ── T7b 澄清第二轮仍缺 → 轮次累加(同会话, 依赖持久化 slots) ──
    res7b = await cascade.classify_v3(
        [{"role": "user", "content": "建一个商品售卖网页"}],
        conversation_id=99004,
    )
    check("T7b-澄清轮次累加=2", res7b.clarify_rounds == 2, f"(rounds={res7b.clarify_rounds})")

    # ── T7c 轮次耗尽 → 提交(不再 clarify) ──
    res7c = await cascade.classify_v3(
        [{"role": "user", "content": "建一个商品售卖网页"}],
        conversation_id=99004,
    )
    check("T7c-轮次耗尽→route", res7c.decision == "route", f"(decision={res7c.decision} rounds={res7c.clarify_rounds})")


import asyncio
asyncio.run(_run())

print("\n==== 结果 ====")
if failures:
    print(f"失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("全部通过 ✅")
