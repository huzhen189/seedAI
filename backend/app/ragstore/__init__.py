"""RagStore：SeedAI 向量库的统一读写后端（Chroma HTTP）。

设计约束（必须遵守）：
  - **全部 fail-soft**：Chroma 不可达 / 集合不存在 / 依赖缺失时，读取返回空列表、
   写入静默跳过，并在日志留痕；**绝不**抛异常中断主链路（S1~S9）。
  - **读写分离**：读取在意图/建站/研究/召回各 stage 内同步 await；写入一律走
    ``safe_upsert_bg`` 后台任务，避免阻塞用户响应（"随生产进行随时补充"的能力）。
  - **集合名唯一真相源在 ``settings``**：本模块只做传输，不硬编码业务集合名。

集合语义（与 reset_all.PRESERVED/RUNTIME 对齐）：
  - 知识底座（PRESERVED，重置保留）：intents / components / error_patterns /
    kb_design / rag_corpus
  - 项目隔离（RUNTIME，重置清空）：project_memory / project_code /
    conversation_context / user_preferences / memory
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import numpy as np

from app.config import settings

logger = logging.getLogger("app.ragstore")

_CLIENT = None  # 进程内单例，避免每次新建 HttpClient
_EF = None  # 进程内单例：Qwen text-embedding-v3 嵌入函数（与既有集合 1024 维对齐）


class _QwenEmbeddingFunction:
    """OpenAI 兼容的 Qwen text-embedding-v3 嵌入函数（1024 维）。

    既有 intents/components 集合由同一模型（客户端嵌入）写入，故检索/新写必须复用，
    否则维度不一致（1024 vs 默认 384）导致查询/写入失败。

    chromadb 1.5.x 要求嵌入函数实现 ``embed_documents`` / ``embed_query`` 两个方法，
    且 ``name()`` 返回与既有集合持久化配置一致的值（服务端默认记录为 "default"），
    否则报 embedding function conflict。本函数 name 返回 "default" 以匹配，
    实际嵌入始终由本函数（Qwen）执行。

    fail-soft：缺 key 或调用失败抛错，由上层 retrieve/upsert 统一吞掉，不中断主链路。
    """

    def __init__(self) -> None:
        self._key = settings.qwen_embedding_key
        self._base = settings.qwen_embedding_base_url.rstrip("/")
        self._model = settings.qwen_embedding_model
        # Qwen text-embedding-v3 单次请求最多 10 条文本，超过返回
        # "batch size is invalid, it should not be larger than 10"。
        self._batch = 10

    def name(self) -> str:
        return "default"

    def _embed_chunk(self, chunk: list[str]) -> list[list[float]]:
        import json
        import urllib.request

        url = f"{self._base}/embeddings"
        payload = json.dumps({"model": self._model, "input": chunk}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = {d.get("index", i): d["embedding"] for i, d in enumerate(data["data"])}
        return [items[i] for i in range(len(chunk))]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 分块调用，规避 Qwen 单次 ≤10 条的限制。
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            out.extend(self._embed_chunk(texts[i : i + self._batch]))
        return out

    @staticmethod
    def _as_texts(input: Any) -> list[str]:
        """chromadb 1.5.9 在 query 与 add/upsert 路径都会以 list 形式传入文本，
        这里统一归一化为字符串列表（单条字符串也包成列表），保证返回 2D 向量。
        """
        if isinstance(input, str):
            return [input]
        return [str(d) for d in input]

    def embed_documents(self, input: Any) -> "np.ndarray":
        # chromadb 1.5.x 序列化嵌入向量时会调用 .tolist()，期望 numpy 数组；
        # add/upsert 路径传 list，返回形状 (N, 1024) 的 2D 数组。
        texts = self._as_texts(input)
        return np.array(self._embed(texts), dtype=np.float32)

    def embed_query(self, input: Any) -> "np.ndarray":
        # 关键：query 路径 chromadb 同样以 list(query_texts) 调用 embed_query，
        # 期望返回"向量列表"(N, 1024)，而非单条扁平向量——否则服务端反序列化报
        # "query_embeddings[0] floating point ... expected a sequence"。
        texts = self._as_texts(input)
        return np.array(self._embed(texts), dtype=np.float32)

    def __call__(self, input: Any) -> "np.ndarray":
        return self.embed_documents(input)


def _ef():
    """返回缓存的 Qwen 嵌入函数单例。"""
    global _EF
    if _EF is None:
        _EF = _QwenEmbeddingFunction()
    return _EF


@dataclass
class RetrievalHit:
    """单条检索结果。"""
    id: str
    text: str
    metadata: dict[str, Any]
    distance: float | None = None


def _get_client() -> Any | None:
    """返回缓存的 Chroma HttpClient；不可用时返回 None（不缓存失败）。"""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    try:
        import chromadb
    except ImportError:
        logger.warning("[ragstore] 缺少 chromadb 依赖，向量检索不可用")
        return None
    try:
        parsed = urlparse(settings.chroma_url)
        _CLIENT = chromadb.HttpClient(
            host=parsed.hostname or "localhost",
            port=parsed.port or 8000,
            ssl=parsed.scheme == "https",
        )
        # 轻量探活，失败则视为不可用（下次调用仍会重试，不缓存 None）。
        _CLIENT.heartbeat()
        return _CLIENT
    except Exception as exc:  # noqa: BLE001 — 后端不可用不应中断主链路
        logger.warning("[ragstore] Chroma 后端不可达: %s", exc)
        _CLIENT = None
        return None


def _get_collection(name: str) -> Any | None:
    """取集合；不存在则按需创建（cosine 距离，便于语义相似度）。

    必须携带 Qwen 嵌入函数，与既有 1024 维集合对齐（否则维度不一致）。
    """
    client = _get_client()
    if client is None:
        return None
    try:
        return client.get_or_create_collection(
            name, embedding_function=_ef(), metadata={"hnsw:space": "cosine"}
        )
    except Exception as exc:  # noqa: BLE001 — 集合可能尚未播种或后端不可用
        logger.warning("[ragstore] 集合 %s 不可用: %s", name, exc, exc_info=True)
        return None


def _stable_id(prefix: str, text: str) -> str:
    """用内容 hash 生成稳定 id，保证 upsert 幂等去重（同内容只更新不新增）。"""
    return f"{prefix}_{hashlib.md5(text.encode('utf-8')).hexdigest()[:16]}"


async def retrieve(
    collection: str,
    query: str,
    *,
    top_k: int | None = None,
    where: dict[str, Any] | None = None,
) -> list[RetrievalHit]:
    """语义检索。空 query / 后端不可用 / 出错均返回 ``[]``（fail-soft）。"""
    if not query or not query.strip():
        return []
    col = _get_collection(collection)
    if col is None:
        return []
    n = top_k or settings.rag_top_k
    try:
        res = await asyncio.to_thread(
            lambda: col.query(query_texts=[query], n_results=n, where=where)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ragstore] 检索 %s 失败: %s", collection, exc, exc_info=True)
        return []
    if not res or not res.get("ids"):
        return []
    ids = res["ids"][0]
    docs = res["documents"][0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    hits: list[RetrievalHit] = []
    for i, doc_id in enumerate(ids):
        hits.append(RetrievalHit(
            id=doc_id,
            text=docs[i] if docs else "",
            metadata=(metas[i] or {}) if metas else {},
            distance=dists[i] if dists else None,
        ))
    logger.debug("[ragstore] 检索 %s 命中 %d 条 (top_k=%d)", collection, len(hits), n)
    return hits


async def upsert(
    collection: str,
    documents: list[str],
    *,
    metadatas: list[dict[str, Any]] | None = None,
    ids: list[str] | None = None,
    id_prefix: str = "auto",
) -> int:
    """写入/更新向量。返回实际写入条数；后端不可用时返回 0（fail-soft）。"""
    docs = [d for d in (documents or []) if d and d.strip()]
    if not docs:
        return 0
    col = _get_collection(collection)
    if col is None:
        return 0
    if ids is None:
        ids = [_stable_id(id_prefix, d) for d in docs]
    if metadatas is None:
        metadatas = [{} for _ in docs]
    try:
        await asyncio.to_thread(
            lambda: col.upsert(documents=docs, metadatas=metadatas, ids=ids)
        )
        logger.debug("[ragstore] 写入 %s 成功 %d 条", collection, len(docs))
        return len(docs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ragstore] 写入 %s 失败: %s", collection, exc, exc_info=True)
        return 0


async def count(collection: str) -> int:
    """集合条目数；不可用返回 0。"""
    col = _get_collection(collection)
    if col is None:
        return 0
    try:
        cnt = await asyncio.to_thread(lambda: col.count())
        logger.debug("[ragstore] count %s = %d", collection, cnt)
        return cnt
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ragstore] count %s 失败: %s", collection, exc, exc_info=True)
        return 0


async def safe_upsert_bg(
    collection: str,
    documents: list[str],
    *,
    metadatas: list[dict[str, Any]] | None = None,
    ids: list[str] | None = None,
    id_prefix: str = "auto",
) -> None:
    """后台写回封装：吞掉一切异常，供 ``asyncio.create_task`` 调用。

    用于"随生产进行随时补充向量库"——建站成功/意图识别后异步沉淀知识，
    不占用用户响应时间，也不因写入失败影响主流程。
    """
    logger.debug("[ragstore] 后台写回 %s (%d 条)", collection, len(documents or []))
    try:
        await upsert(collection, documents, metadatas=metadatas, ids=ids, id_prefix=id_prefix)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ragstore] 后台写回 %s 异常(已忽略): %s", collection, exc, exc_info=True)


def format_hits_for_prompt(hits: list[RetrievalHit], *, label: str = "参考") -> str:
    """把检索结果拼成可读文本块，注入 LLM 提示词或日志。"""
    if not hits:
        return ""
    lines = [f"【{label}】"]
    for i, h in enumerate(hits, start=1):
        meta = h.metadata or {}
        tag = meta.get("kind") or meta.get("type") or ""
        tag = f"({tag}) " if tag else ""
        lines.append(f"{i}. {tag}{h.text}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 管理面读写封装（供统计系统「向量库」可视化工具使用）
# 复用上方 _get_client / _get_collection / _ef，保证维度(1024)与既有集合一致。
# 全部 fail-soft：后端不可用 / 集合不存在 / 出错返回空或 0，不抛异常中断。
# ─────────────────────────────────────────────────────────────────────────────
async def list_collections() -> list[dict[str, Any]]:
    """列出全部集合：名称 / 条目数 / 元数据（hnsw 空间等）。"""
    client = _get_client()
    if client is None:
        return []
    try:
        cols = await asyncio.to_thread(lambda: client.list_collections())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ragstore] 列集合失败: %s", exc, exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for c in cols:
        name = c.name if hasattr(c, "name") else str(c)
        try:
            cnt = await asyncio.to_thread(lambda: client.get_collection(name).count())
        except Exception:  # noqa: BLE001
            cnt = 0
        meta: dict[str, Any] = {}
        try:
            meta = dict(c.metadata or {})
        except Exception:  # noqa: BLE001
            meta = {}
        out.append({"name": name, "count": cnt, "metadata": meta})
    return out


async def peek(
    collection: str,
    *,
    limit: int = 50,
    offset: int = 0,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """分页浏览集合内的向量点（id / document / metadata，不含原始向量）。"""
    col = _get_collection(collection)
    if col is None:
        return []
    try:
        res = await asyncio.to_thread(
            lambda: col.get(limit=limit, offset=offset, where=where, include=["documents", "metadatas"])
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ragstore] 浏览 %s 失败: %s", collection, exc, exc_info=True)
        return []
    ids = res.get("ids") or []
    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    out: list[dict[str, Any]] = []
    for i, pid in enumerate(ids):
        out.append({
            "id": pid,
            "document": docs[i] if docs else "",
            "metadata": (metas[i] or {}) if metas else {},
        })
    return out


async def get_point(
    collection: str,
    point_id: str,
    *,
    with_embedding: bool = False,
) -> dict[str, Any] | None:
    """取单条向量点；with_embedding=True 时附带原始向量（1024 维，体积大）。"""
    col = _get_collection(collection)
    if col is None:
        return None
    include = ["documents", "metadatas"]
    if with_embedding:
        include.append("embeddings")
    try:
        res = await asyncio.to_thread(lambda: col.get(ids=[point_id], include=include))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ragstore] 取点 %s/%s 失败: %s", collection, point_id, exc, exc_info=True)
        return None
    ids = res.get("ids") or []
    if not ids:
        return None
    doc = (res.get("documents") or [None])[0]
    meta = (res.get("metadatas") or [None])[0] or {}
    emb = None
    if with_embedding:
        raw_embs = res.get("embeddings")
        if raw_embs is not None and len(raw_embs) > 0:
            emb = raw_embs[0]
            # chromadb 返回的 numpy 数组需转成 Python list，避免前端/JSON 序列化报错
            try:
                emb = emb.tolist()
            except Exception:  # noqa: BLE001
                pass
    return {"id": ids[0], "document": doc, "metadata": meta, "embedding": emb}


async def delete_points(
    collection: str,
    *,
    ids: list[str] | None = None,
    where: dict[str, Any] | None = None,
) -> int:
    """删除向量点（按 ids 或 where 过滤）。返回删除条数（where 删除为前后计数差）。"""
    col = _get_collection(collection)
    if col is None:
        return 0
    if not ids and not where:
        return 0
    try:
        if ids:
            await asyncio.to_thread(lambda: col.delete(ids=ids))
            return len(ids)
        before = await asyncio.to_thread(lambda: col.count())
        await asyncio.to_thread(lambda: col.delete(where=where))
        after = await asyncio.to_thread(lambda: col.count())
        return max(0, before - after)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ragstore] 删点 %s 失败: %s", collection, exc, exc_info=True)
        return 0


async def add_points(
    collection: str,
    *,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> int:
    """新增/更新向量点（upsert 幂等）。返回写入条数。"""
    return await upsert(collection, documents, metadatas=metadatas, ids=ids)


async def clear_collection(collection: str) -> int:
    """清空整个集合的所有向量点（保留集合本身，复用嵌入函数重新就绪）。"""
    col = _get_collection(collection)
    if col is None:
        return 0
    try:
        total = await asyncio.to_thread(lambda: col.count())
        removed = 0
        while removed < total:
            batch = await asyncio.to_thread(lambda: col.get(limit=1000, offset=removed, include=[]))
            ids = batch.get("ids") or []
            if not ids:
                break
            await asyncio.to_thread(lambda: col.delete(ids=ids))
            removed += len(ids)
        return removed
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ragstore] 清空 %s 失败: %s", collection, exc, exc_info=True)
        return 0


__all__ = [
    "RetrievalHit",
    "add_points",
    "clear_collection",
    "count",
    "delete_points",
    "format_hits_for_prompt",
    "get_point",
    "list_collections",
    "peek",
    "retrieve",
    "safe_upsert_bg",
    "upsert",
]
