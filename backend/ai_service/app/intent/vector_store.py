"""意图向量索引(混合级联第②步: 语义召回 top5, v1.2.0)。

- ensure_intent_index(): 把 intent_catalog.json 的 examples 写进 Chroma `intents` 集合
  (幂等: 稳定 id + get_or_create + upsert, 启动期调用)。embedding 复用 knowledge.chroma 的 _ef。
- retrieve_intents(query, top_k): 在 intents 集合语义检索, 按 intent_id 聚合取最高相似度,
  返回有序候选 [(intent_id, score, intent_data)]。
- 优雅降级: Chroma / embedding 不可用时, 退回纯 Python bigram 重叠打分(离线, 无外部依赖),
  保证分类链路在无向量库的环境下仍可运行(仅精度下降)。
"""

from __future__ import annotations

import logging

from ..config import settings
from ..knowledge.chroma import _available, _client, _ef
from .catalog import get_intent, intent_list

logger = logging.getLogger("ai_service.intent.vector")

_INTENT_COLLECTION = settings.chroma_collection_intents


def _bigrams(s: str) -> set[str]:
    s = (s or "").strip()
    if len(s) <= 1:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _overlap_sim(a: str, b: str) -> float:
    """bigram 重叠系数(0~1), 对中文短语足够区分。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    inter = len(A & B)
    return inter / min(len(A), len(B))


def ensure_intent_index() -> None:
    """把意图目录 examples 写入 Chroma `intents` 集合(幂等)。失败仅 warn。"""
    if not _available():
        logger.warning("[向量] embedding 不可用, 跳过意图索引构建(降级离线 bigram)")
        return
    try:
        client = _client()
        ef = _ef()
        col = client.get_or_create_collection(name=_INTENT_COLLECTION, embedding_function=ef)
        intents = intent_list()
        ids: list[str] = []
        docs: list[str] = []
        metas: list[dict] = []
        for it in intents:
            iid = it["id"]
            for j, ex in enumerate(it.get("examples", [])):
                ids.append(f"intent_{iid}_{j}")
                docs.append(ex)
                metas.append({
                    "intent_id": iid,
                    "level1": it.get("level1", ""),
                    "level2": it.get("level2", ""),
                    "skill": it.get("skill", ""),
                    "title": it.get("title", ""),
                })
        if ids:
            col.upsert(ids=ids, documents=docs, metadatas=metas)
            logger.info("[向量] 意图索引已构建 %d 条(集合=%s)", len(ids), _INTENT_COLLECTION)
    except Exception as e:  # pragma: no cover
        logger.warning("[向量] 意图索引构建失败(可忽略): %s", e)


def _offline_scores(query: str) -> list[tuple[str, float]]:
    """离线 bigram 打分: 对每个意图取其 examples 与 query 的最高重叠系数。"""
    out: list[tuple[str, float]] = []
    for it in intent_list():
        best = 0.0
        for ex in it.get("examples", []):
            s = _overlap_sim(query, ex)
            if s > best:
                best = s
        out.append((it["id"], best))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def retrieve_intents(query: str, top_k: int = 5) -> list[dict]:
    """语义召回意图候选, 按 intent_id 聚合最高相似度, 返回有序列表。

    返回: [{"intent_id", "score", "intent"(目录条目)}, ...]
    无 Chroma 时退回离线 bigram 打分(同样返回该结构)。
    """
    if not query or not query.strip():
        return []

    # ── 离线兜底 ──
    if not _available():
        ranked = _offline_scores(query)[:top_k]
        return [
            {"intent_id": iid, "score": score, "intent": get_intent(iid)}
            for iid, score in ranked if get_intent(iid) is not None
        ]

    # ── Chroma 主路径 ──
    try:
        from ..knowledge.chroma import retrieve as _chroma_retrieve
        raw = _chroma_retrieve(query, _INTENT_COLLECTION, top_k=top_k * 4 or 20)
        # 按 intent_id 聚合取最高分
        best: dict[str, float] = {}
        for r in raw:
            iid = (r.get("metadata") or {}).get("intent_id")
            sc = r.get("score")
            if not iid or sc is None:
                continue
            if iid not in best or sc > best[iid]:
                best[iid] = sc
        ranked = sorted(best.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [
            {"intent_id": iid, "score": score, "intent": get_intent(iid)}
            for iid, score in ranked if get_intent(iid) is not None
        ]
    except Exception as e:
        logger.warning("[向量] Chroma 召回失败, 退回离线打分: %s", e)
        ranked = _offline_scores(query)[:top_k]
        return [
            {"intent_id": iid, "score": score, "intent": get_intent(iid)}
            for iid, score in ranked if get_intent(iid) is not None
        ]
