"""系统规则服务：语义召回 → 回查 MySQL → scope/rule_type 仲裁 → 注入系统 Prompt。

双轨范式（与记忆 v2 同源，但面向「刚性规则」）：
  - MySQL(SoT) 存规则全文 content；向量库只存 summary+keywords 索引串，命中后回查 MySQL。
  - 召回：对向量库做语义 ANN，用 scope_key $in 过滤当前会话相关作用域，只取 is_active。
  - 仲裁：按 (scope 优先级, rule_type 优先级, priority, version) 降序，对 rule_key 去重
    （更权威的版本胜出），再按字符预算封顶，避免 prompt 膨胀。
  - 全部 fail-soft：向量不可达/出错返回空块，绝不中断主链路（与 ragstore 一致）。

scope 优先级（越大越具体、越权威）：session(3) > user/project(2) > domain(1) > global(0)
rule_type 优先级（越大越硬）：constraint(3) > guardrail(2) > policy(1) > preference(0)
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db import transaction
from app.models import SystemRule
from app.ragstore import clear_collection, retrieve, upsert

logger = logging.getLogger("app.services.system_rules")

# scope 越具体越权威（用于仲裁排序）。
SCOPE_PRIORITY: dict[str, int] = {
    "global": 0,
    "domain": 1,
    "user": 2,
    "project": 2,
    "session": 3,
}
# rule_type 越硬越权威（用于仲裁排序）。
RULE_TYPE_PRIORITY: dict[str, int] = {
    "constraint": 3,
    "guardrail": 2,
    "policy": 1,
    "preference": 0,
}

# 注入系统 Prompt 时的字符预算（约 2400 汉字，足够覆盖召回到的刚性规则而不过胀）。
RULE_CHAR_BUDGET = 2400
# 语义召回候选数（召回后仲裁+预算裁剪，故取稍大）。
RULE_RECALL_TOP_K = 30


def scope_key_of(scope: str, scope_ref: str | None) -> str:
    """拼出向量 metadata 用的 scope_key：global → "global"，否则 "scope:ref"。"""
    if scope == "global":
        return "global"
    return f"{scope}:{scope_ref or ''}"


async def recall(
    *,
    scopes: set[str],
    query: str,
    top_k: int = RULE_RECALL_TOP_K,
    char_budget: int = RULE_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """语义召回 + 回查 MySQL 取全文 + 仲裁去重 + 预算封顶。

    返回已排序、已裁剪的规则纯 dict 列表（不含 _sortkey），供 format_rules_for_prompt。
    """
    if not scopes:
        return []
    # Chroma 顶层 where 只允许一个操作符，多条件需 $and；此处仅按 scope_key 过滤即可——
    # 失效规则在 rebuild_vector_collection 时已排除出向量集合，且下方回查 MySQL 仍强制
    # is_active，故无需在向量层再过滤 is_active。
    where = {"scope_key": {"$in": list(scopes)}}
    hits = await retrieve(settings.chroma_collection_system_rules, query, top_k=top_k, where=where)
    if not hits:
        return []
    rule_ids = [h.metadata.get("rule_id") for h in hits if h.metadata.get("rule_id")]
    if not rule_ids:
        return []

    # 回查 MySQL 取全文（向量里只有摘要，不存原文——防篡改/防膨胀）。
    async with transaction() as session:
        rows = (
            await session.execute(
                select(SystemRule).where(
                    SystemRule.id.in_(rule_ids), SystemRule.is_active.is_(True)
                )
            )
        ).scalars().all()
        fetched = [
            {
                "id": r.id,
                "rule_key": r.rule_key,
                "scope": r.scope,
                "scope_ref": r.scope_ref,
                "rule_type": r.rule_type,
                "title": r.title,
                "content": r.content,
                "priority": r.priority or 0,
                "version": r.version or 1,
            }
            for r in rows
        ]

    # 仲裁：按权威度排序，对 rule_key 去重（更权威者胜出）。
    best: dict[str, dict[str, Any]] = {}
    for r in fetched:
        sort_key = (
            SCOPE_PRIORITY.get(r["scope"], 0),
            RULE_TYPE_PRIORITY.get(r["rule_type"], 0),
            r["priority"],
            r["version"],
        )
        prev = best.get(r["rule_key"])
        if prev is None or sort_key > prev["_sortkey"]:
            r2 = dict(r)
            r2["_sortkey"] = sort_key
            r2["scope_key"] = scope_key_of(r["scope"], r["scope_ref"])
            best[r["rule_key"]] = r2
    ordered = sorted(best.values(), key=lambda x: x["_sortkey"], reverse=True)

    # 预算封顶（至少保留第一条，确保刚性规则不被整体裁掉）。
    out: list[dict[str, Any]] = []
    used = 0
    for r in ordered:
        text = r["content"] or ""
        if out and used + len(text) > char_budget:
            break
        used += len(text)
        out.append({k: v for k, v in r.items() if k != "_sortkey"})
    return out


def format_rules_for_prompt(rules: list[dict[str, Any]]) -> str:
    """把仲裁后的规则拼成系统 Prompt 块。"""
    if not rules:
        return ""
    lines = [
        "【系统刚性规则（必须遵守，已按权威度排序；与下方其他指引冲突时以本段为准）】"
    ]
    for i, r in enumerate(rules, start=1):
        lines.append(f"{i}. [{r['rule_type']}|{r['scope_key']}] {r['title']}：{r['content']}")
    return "\n".join(lines)


async def get_active_rules_block(scopes: set[str], query: str) -> str:
    """供域服务调用：返回可直接注入 system Prompt 的规则块；任何异常返回空串（fail-soft）。"""
    try:
        rules = await recall(scopes=scopes, query=query)
        return format_rules_for_prompt(rules)
    except Exception as exc:  # noqa: BLE001 — 规则召回失败绝不应中断主链路
        logger.warning("[system_rules] 召回失败(已忽略): %s", exc, exc_info=True)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# 写入面：规则落库（MySQL 真相）+ 向量索引（摘要+关键词）。seed / 管理面调用。
# ─────────────────────────────────────────────────────────────────────────────


def _rule_to_vector(r: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """把规则 dict 转成 (doc, metadata, id) 供向量 upsert。"""
    sk = scope_key_of(r["scope"], r.get("scope_ref"))
    doc = f"{r.get('summary', '')} {r.get('keywords', '')}".strip()
    meta = {
        "rule_id": r["id"],
        "rule_key": r["rule_key"],
        "scope": r["scope"],
        "scope_ref": r.get("scope_ref") or "",
        "scope_key": sk,
        "rule_type": r["rule_type"],
        "priority": r.get("priority", 50),
        "is_active": True,
    }
    return doc, meta, f"sysrule_{r['rule_key']}"


async def rebuild_vector_collection(rules: list[dict[str, Any]]) -> int:
    """用最新 MySQL 行重建向量集合（先清空陈旧点，再批量 upsert 活跃规则）。

    整库重置后 rule_id 会变，旧向量点带陈旧 rule_id，故先 clear 再 upsert 保证一致。
    fail-soft：向量不可达时返回 0，不影响 MySQL 真相。
    """
    collection = settings.chroma_collection_system_rules
    await clear_collection(collection)  # 清掉可能带陈旧 rule_id 的点
    docs: list[str] = []
    metas: list[dict[str, Any]] = []
    ids: list[str] = []
    for r in rules:
        if not r.get("is_active", True):
            continue
        doc, meta, rid = _rule_to_vector(r)
        if not doc:
            continue
        docs.append(doc)
        metas.append(meta)
        ids.append(rid)
    if not docs:
        return 0
    return await upsert(collection, docs, metadatas=metas, ids=ids)


__all__ = [
    "RULE_CHAR_BUDGET",
    "RULE_RECALL_TOP_K",
    "format_rules_for_prompt",
    "get_active_rules_block",
    "rebuild_vector_collection",
    "recall",
    "scope_key_of",
]
