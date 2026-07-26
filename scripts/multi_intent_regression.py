"""多意图 A+B 路由 + 强规则离线回归(不依赖真实 Chroma/Redis/LLM, 可全环境运行)。

覆盖 OPTIMIZE_PLAN:
  §2.1 强规则白名单: 10 号(写 PRD)直路由 build_requirement; 诗歌/摘要不误伤 doc。
  §2.2 多意图门控: 并列连词(并且/还要/另外/同时)直接强触发 hybrid split。
  T1~T9 方案 A+B 路由既有回归。

运行: python scripts/multi_intent_regression.py
"""

import os
import sys
import types
import asyncio

# ── 1) 把 backend/ 加到 sys.path(单进程后代码在 backend/app/agent/intent) ──
_ROOT = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(_ROOT))

for _name in ("langchain_openai", "httpx", "chromadb", "langchain"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)
sys.modules["langchain_openai"].ChatOpenAI = object
sys.modules["httpx"].Timeout = object

# redis 伪包: 支持 `import redis.asyncio as aioredis`
if "redis" not in sys.modules:
    _redis_pkg = types.ModuleType("redis")
    _redis_asyncio = types.ModuleType("redis.asyncio")
    _redis_asyncio.from_url = lambda *a, **k: None
    _redis_asyncio.Redis = object
    _redis_pkg.asyncio = _redis_asyncio
    sys.modules["redis"] = _redis_pkg
    sys.modules["redis.asyncio"] = _redis_asyncio

if "pydantic_settings" not in sys.modules:
    _ps = types.ModuleType("pydantic_settings")
    _ps.BaseSettings = object
    _ps.SettingsConfigDict = dict
    sys.modules["pydantic_settings"] = _ps

import app.agent.analytics as _analytics
_analytics._get_redis = lambda: None  # type: ignore

from app.agent.intent import multi_intent as mi  # noqa: E402
from app.agent.intent import cascade as _cascade  # noqa: E402
from app.agent.intent.cascade import PipelineResult  # noqa: E402
from app.agent.intent.catalog import skill_for  # noqa: E402
from app.agent.intent.rulesmatcher import match_rules  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK] {name} {detail}")
    else:
        print(f"[FAIL] {name} {detail}")
        failures.append(name)


def strong_target(text: str):
    hits = match_rules(text)
    s = next((h for h in hits if h.strength == "strong"), None)
    return s.intent_id if s else None


# ── 2) monkeypatch 逐段分类器(_classify_segment) ──
_SEG_MAP: dict[str, tuple] = {}


def _fake_classify_segment(messages, model_id="deepseek", **kwargs):
    text = ""
    if messages:
        c = messages[-1].get("content") if isinstance(messages[-1], dict) else ""
        text = c if isinstance(c, str) else ""
    l1, l2, ind, conf, skill = "learn", "casual", "other", 0.9, "agent_chat"
    for key, val in _SEG_MAP.items():
        if key in text:
            l1, l2, ind, conf, skill = val
            break
    return PipelineResult(
        decision="route",
        selected_skill=skill,
        intent={"level1": l1, "level2": l2, "industry": ind, "confidence": conf},
        evidence={"source": "stub"},
    )


async def _afake_classify_segment(messages, model_id="deepseek", **kwargs):
    return _fake_classify_segment(messages, model_id, **kwargs)


_cascade._classify_segment = _afake_classify_segment  # type: ignore


# ── 3) monkeypatch 方案A 的 LLM(_classify 走 get_chat_model) ──
_A_JSON = {"v": None}


class _FakeChat:
    async def ainvoke(self, msgs):
        return types.SimpleNamespace(content=_A_JSON["v"] or '{"is_multi":false}')


mi.get_chat_model = lambda *a, **k: _FakeChat()  # type: ignore
mi.resolve_fallback_order = lambda *a, **k: ["stub"]  # type: ignore


async def _run():
    # ── §2.1 强规则白名单 ──
    check("§2.1-10号 PRD 直路由 build_requirement",
          strong_target("帮我写一份产品需求文档，关于一个待办事项应用") == "build_requirement",
          strong_target("帮我写一份产品需求文档，关于一个待办事项应用"))
    check("§2.1-『写一份PRD』直路由 build_requirement",
          strong_target("帮我写一份PRD") == "build_requirement")
    check("§2.1-诗歌(写首诗)不误伤 doc 强规则",
          strong_target("帮我写一首关于春天的短诗") is None,
          strong_target("帮我写一首关于春天的短诗"))
    check("§2.1-摘要(总结这段话)不误伤 doc 强规则",
          strong_target("帮我总结一下这段话：人工智能...") is None,
          strong_target("帮我总结一下这段话"))

    # ── §2.2 多意图并列连词门控 ──
    check("§2.2-『并且』直接强触发多意图",
          mi._lightweight_multi_check([{"role": "user", "content": "帮我生成一个公司官网，并且写一篇介绍文章"}]))
    check("§2.2-『还要』直接强触发多意图",
          mi._lightweight_multi_check([{"role": "user", "content": "生成电商站，还要配上用户故事"}]))
    check("§2.2-『另外』直接强触发多意图",
          mi._lightweight_multi_check([{"role": "user", "content": "做个博客，另外再写份部署文档"}]))
    check("§2.2-单一建站意图不误触发",
          not mi._lightweight_multi_check([{"role": "user", "content": "帮我做一个个人博客网站"}]))

    # ── T1 单意图门控 ──
    _SEG_MAP.clear()
    r = await mi.recognize_intents([{"role": "user", "content": "什么是闭包?帮我解释一下"}])
    check("T1-单意图门控→不拆", (not r.is_multi) and r.source == "",
          f"(is_multi={r.is_multi}, source={r.source!r})")

    # ── T2 多意图门控(≥2 意图大类 或 连词强触发) ──
    r = await mi.recognize_intents([{"role": "user", "content": "帮我做个电商官网，另外再写一份部署文档。"}])
    check("T2-多意图门控→进入路由", r.source != "", f"(source={r.source!r})")

    # ── T3 方案B 并行 ──
    _SEG_MAP.clear()
    _SEG_MAP.update({
        "电商官网": ("build", "site", "corp", 0.9, skill_for("build", "site") or "agent_generate_site"),
        "部署文档": ("doc", "readme", "corp", 0.9, skill_for("doc", "readme") or "agent_generate_doc"),
    })
    r = await mi.recognize_intents([{"role": "user", "content": "帮我做个电商官网另外写一份部署文档。"}])
    check("T3-方案B路径=hybrid", r.source == "hybrid", f"(source={r.source!r})")
    check("T3-识别为多意图", r.is_multi and len(r.sub_tasks) >= 2, f"(n={len(r.sub_tasks)})")
    check("T3-并行策略", r.strategy == "parallel", f"(strategy={r.strategy})")
    check("T3-无依赖边", not any(s.dependencies for s in r.sub_tasks),
          f"(deps={[s.dependencies for s in r.sub_tasks]})")

    # ── T4 方案B 串行依赖 ──
    _SEG_MAP.clear()
    _SEG_MAP.update({
        "电商官网": ("build", "site", "corp", 0.9, skill_for("build", "site") or "agent_generate_site"),
        "部署文档": ("doc", "readme", "corp", 0.9, skill_for("doc", "readme") or "agent_generate_doc"),
    })
    text = "帮我做个电商官网然后根据刚生成的站点写一份部署文档。"
    r = await mi.recognize_intents([{"role": "user", "content": text}])
    check("T4-串行依赖边非空", any(s.dependencies for s in r.sub_tasks),
          f"(deps={[s.dependencies for s in r.sub_tasks]})")
    check("T4-策略=mixed", r.strategy == "mixed", f"(strategy={r.strategy})")

    # ── T5 升级判定 B→A(平均置信过低) ──
    _SEG_MAP.clear()
    _SEG_MAP.update({
        "电商官网": ("build", "site", "corp", 0.1, skill_for("build", "site") or "agent_generate_site"),
        "部署文档": ("doc", "readme", "corp", 0.1, skill_for("doc", "readme") or "agent_generate_doc"),
    })
    _A_JSON['v'] = (
        '{"is_multi":true,"reason":"建站+文档",'
        '"sub_tasks":['
        '{"goal":"电商官网","original_text":"电商官网","level1":"build","level2":"site",'
        '"industry":"corp","skill":"agent_generate_site","context_hint":"","risk_level":"low","dependencies":[]},'
        '{"goal":"部署文档","original_text":"部署文档","level1":"doc","level2":"readme",'
        '"industry":"corp","skill":"agent_generate_doc","context_hint":"依据站点","risk_level":"low","dependencies":["sub_0"]}'
        ']}'
    )
    r = await mi.recognize_intents([{"role": "user", "content": "做个电商官网并写部署文档"}])
    check("T5-升级到方案A", r.source == "llm", f"(source={r.source!r})")
    check("T5-升级后多意图", r.is_multi and len(r.sub_tasks) >= 2, f"(n={len(r.sub_tasks)})")
    check("T5-方案A 保留依赖", any(s.dependencies for s in r.sub_tasks),
          f"(deps={[s.dependencies for s in r.sub_tasks]})")

    # ── T6 两路均未拆出多意图 → 退回单意图 ──
    _SEG_MAP.clear()
    _SEG_MAP.update({
        "电商官网": ("build", "site", "corp", 0.1, skill_for("build", "site") or "agent_generate_site"),
        "部署文档": ("build", "site", "corp", 0.1, skill_for("build", "site") or "agent_generate_site"),
    })
    _A_JSON['v'] = '{"is_multi":false,"reason":"单意图"}'
    r = await mi.recognize_intents([{"role": "user", "content": "做个电商官网。部署文档也来一份。"}])
    check("T6-两路均未拆→退回单意图", not r.is_multi, f"(is_multi={r.is_multi}, source={r.source!r})")
    check("T6-退回来源=hybrid", r.source == "hybrid", f"(source={r.source!r})")

    # ── T7 方案B 禁用 → 直走方案A ──
    mi.settings.split_b_enabled = False
    _A_JSON['v'] = (
        '{"is_multi":true,"reason":"x",'
        '"sub_tasks":['
        '{"goal":"建站","original_text":"电商官网","level1":"build","level2":"site",'
        '"industry":"corp","skill":"agent_generate_site","context_hint":"","risk_level":"low","dependencies":[]},'
        '{"goal":"文档","original_text":"部署文档","level1":"doc","level2":"readme",'
        '"industry":"corp","skill":"agent_generate_doc","context_hint":"","risk_level":"low","dependencies":[]}'
        ']}'
    )
    r = await mi.recognize_intents([{"role": "user", "content": "做个电商官网并写部署文档"}])
    check("T7-方案B禁用→直走方案A", r.source == "llm", f"(source={r.source!r})")
    mi.settings.split_b_enabled = True

    # ── T8 超长截断 ──
    mi.settings.split_b_max_subtasks = 2
    _SEG_MAP.clear()
    _SEG_MAP.update({
        "官网": ("build", "site", "corp", 0.9, skill_for("build", "site") or "agent_generate_site"),
        "部署文档": ("doc", "readme", "corp", 0.9, skill_for("doc", "readme") or "agent_generate_doc"),
        "解释": ("learn", "explain", "corp", 0.9, skill_for("learn", "explain") or "agent_chat"),
    })
    text = "做个官网，另外写部署文档，顺便解释一下什么是响应式设计。"
    r = await mi.recognize_intents([{"role": "user", "content": text}])
    check("T8-超长截断到上限", len(r.sub_tasks) == 2, f"(n={len(r.sub_tasks)})")
    mi.settings.split_b_max_subtasks = 6

    # ── T9 埋点: 内存假 Redis 验证 ai:mi 统计 ──
    class _FakeRedis:
        def __init__(self):
            self.h: dict = {}
            self.z: dict = {}

        async def hincrby(self, k, f, amt=1):
            self.h.setdefault(k, {})
            self.h[k][f] = self.h[k].get(f, 0) + amt
            return self.h[k][f]

        async def hget(self, k, f):
            return self.h.get(k, {}).get(f)

        async def hgetall(self, k):
            return self.h.get(k, {})

        async def zadd(self, k, mapping):
            self.z.setdefault(k, {})
            for m, s in mapping.items():
                self.z[k][m] = s

        async def zremrangebyrank(self, k, a, b):
            return 0

        async def zcard(self, k):
            return len(self.z.get(k, {}))

        async def zrange(self, k, a, b, withscores=False):
            items = sorted(self.z.get(k, {}).items(), key=lambda x: x[1])
            if a < 0:
                a = max(0, len(items) + a)
            if b < 0:
                b = len(items) + b
            sl = items[a:b + 1] if b >= a else []
            if withscores:
                return [(m, s) for m, s in sl]
            return [m for m, s in sl]

        async def keys(self, pat):
            return []

    _fr = _FakeRedis()
    _analytics._get_redis = lambda: _fr  # type: ignore

    _SEG_MAP.clear()
    _SEG_MAP.update({
        "电商官网": ("build", "site", "corp", 0.9, skill_for("build", "site") or "agent_generate_site"),
        "部署文档": ("doc", "readme", "corp", 0.9, skill_for("doc", "readme") or "agent_generate_doc"),
    })
    _A_JSON['v'] = '{"is_multi":false,"reason":"单意图"}'
    await mi.recognize_intents([{"role": "user", "content": "帮我做个电商官网，另外再写一份部署文档。"}])

    _SEG_MAP.clear()
    _SEG_MAP.update({
        "电商官网": ("build", "site", "corp", 0.1, skill_for("build", "site") or "agent_generate_site"),
        "部署文档": ("doc", "readme", "corp", 0.1, skill_for("doc", "readme") or "agent_generate_doc"),
    })
    _A_JSON['v'] = (
        '{"is_multi":true,"reason":"x",'
        '"sub_tasks":['
        '{"goal":"建站","original_text":"电商官网","level1":"build","level2":"site",'
        '"industry":"corp","skill":"agent_generate_site","context_hint":"","risk_level":"low","dependencies":[]},'
        '{"goal":"文档","original_text":"部署文档","level1":"doc","level2":"readme",'
        '"industry":"corp","skill":"agent_generate_doc","context_hint":"","risk_level":"low","dependencies":[]}'
        ']}'
    )
    await mi.recognize_intents([{"role": "user", "content": "做个电商官网并写部署文档"}])

    stats = await _analytics.multi_intent_stats()
    check("T9-埋点总数≥2", stats.get("total", 0) >= 2, f"(total={stats.get('total')})")
    check("T9-路径 hybrid≥1", stats.get("path_dist", {}).get("hybrid", 0) >= 1, f"({stats.get('path_dist')})")
    check("T9-路径 llm≥1", stats.get("path_dist", {}).get("llm", 0) >= 1, f"({stats.get('path_dist')})")
    check("T9-升级计数≥1", stats.get("escalated", 0) >= 1, f"(escalated={stats.get('escalated')})")
    check("T9-升级率存在", "escalate_rate" in stats, f"(escalate_rate={stats.get('escalate_rate')})")
    check("T9-A/B 占比存在", "ab_ratio" in stats, f"(ab_ratio={stats.get('ab_ratio')})")
    _analytics._get_redis = lambda: None  # type: ignore


asyncio.run(_run())

print("\n==== 结果 ====")
if failures:
    print(f"失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print("全部通过 ✅")
