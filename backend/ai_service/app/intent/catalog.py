"""意图目录加载器(单一来源, v1.2.0 混合级联)。

- 分类(向量召回 + LLM 终判)与多意图拆分(splitter)共用同一份 intent_catalog.json,
  根治旧 splitter.SKILL_WHITELIST 与 INTENT_SKILL_MAP 两处维护漂移的 R3 风险。
- 提供: load_catalog / get_intent / intent_list / catalog_for_llm / skill_whitelist。
- 纯 JSON + 缓存, 可离线(无 Chroma / 无 Redis)直接测试。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("ai_service.intent.catalog")

_CATALOG_PATH = Path(__file__).resolve().parent / "intent_catalog.json"


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    """加载并缓存意图目录(进程内单次解析)。"""
    try:
        with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        intents = data.get("intents", [])
        logger.info("[目录] 加载意图目录: %d 个意图 (version=%s)", len(intents), data.get("version", "?"))
        return data
    except Exception as e:  # pragma: no cover
        logger.error("[目录] 加载失败: %s", e)
        return {"version": "error", "intents": []}


def intent_list() -> list[dict]:
    return load_catalog().get("intents", [])


def get_intent(intent_id: str) -> dict | None:
    for it in intent_list():
        if it.get("id") == intent_id:
            return it
    return None


def intent_by_level(level1: str, level2: str) -> dict | None:
    for it in intent_list():
        if it.get("level1") == level1 and it.get("level2") == level2:
            return it
    return None


def examples_of(intent_id: str) -> list[str]:
    it = get_intent(intent_id)
    return it.get("examples", []) if it else []


def required_slots_of(intent_id: str) -> list[str]:
    it = get_intent(intent_id)
    return it.get("required_slots", []) if it else []


def catalog_for_llm(top_ids: list[tuple[str, float]] | None = None) -> str:
    """生成供 LLM 终判 prompt 使用的候选意图描述文本。

    top_ids: [(intent_id, score), ...] 来自向量召回, 按相似度排序。
    不传则输出全部意图(兜底)。每条含 id/标题/描述/所需槽位/相似度。
    """
    intents = intent_list()
    if top_ids:
        by_id = {it["id"]: it for it in intents}
        ordered = []
        for iid, score in top_ids:
            it = by_id.get(iid)
            if it:
                ordered.append((it, score))
    else:
        ordered = [(it, 0.0) for it in intents]

    lines: list[str] = []
    for idx, (it, score) in enumerate(ordered, 1):
        slots = it.get("required_slots", [])
        slot_txt = "、".join(slots) if slots else "无"
        lines.append(
            f"{idx}. [{it['id']}] {it.get('title','')} —— {it.get('description','')}"
            f"（level1={it['level1']} level2={it['level2']} skill={it['skill']}；"
            f"需填槽位: {slot_txt}"
            + (f"；向量相似度={score:.2f}" if score else "")
            + "）"
        )
    return "\n".join(lines)


def skill_whitelist() -> str:
    """生成供 splitter LLM 使用的『可用技能』白名单文本(从目录派生, 单一来源)。

    旧 splitter.SKILL_WHITELIST 硬编码了 agent_chat/explain/write_code 等失效名,
    这里改为直接读目录, 保证与 INTENT_SKILL_MAP / 实际注册技能一致。
    """
    # 去重保留 skill → 代表性标题
    seen: dict[str, str] = {}
    for it in intent_list():
        sk = it.get("skill", "")
        if sk and sk not in seen:
            seen[sk] = it.get("title", sk)
    parts = [f"{sk}({title})" for sk, title in seen.items()]
    return "、".join(parts)
