"""RAG 检索增强(②-a · 文档 §7):components + memory 双库检索注入 Planner + 生成后回写 memory。

- `build_rag_context`:检索 components(组件库)+ memory(历史记忆),拼为 Planner 可用上下文。
- `save_memory`:生成成功后异步回写 memory 集合(记忆闭环)。
- `seed_components`:批量写入 components 集合(数据准备,由 scripts/seed_rag_components.py 调用)。

依赖:chromadb + Qwen text-embedding(§7 已配)。embedding key / chroma 不可用时**优雅降级**
(返回空上下文 / 跳过回写),不阻断主生成流。
"""

from __future__ import annotations

import logging

from ..config import settings


logger = logging.getLogger("ai_service.rag")

# 注入 Planner 的 RAG 上下文上限,防 prompt 过长
_RAG_INJECT_MAX_CHARS = 4000


def _client():
    import chromadb

    return chromadb.HttpClient(base_url=settings.chroma_url)


def _ef():
    from chromadb.utils import embedding_functions

    # 优先 Qwen, 其次 DeepSeek, 最后本地 sentence-transformers
    if settings.qwen_embedding_key:
        return embedding_functions.OpenAIEmbeddingFunction(
            api_key=settings.qwen_embedding_key,
            api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_name=settings.qwen_embedding_model,
        )
    if settings.deepseek_api_key:
        return embedding_functions.OpenAIEmbeddingFunction(
            api_key=settings.deepseek_api_key,
            api_base="https://api.deepseek.com/v1",
            model_name="deepseek-chat",
        )
    # 本地模型兜底(无需 API key)
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )


def _available() -> bool:
    try:
        return _ef() is not None
    except Exception:
        return False


def retrieve(query: str, collection: str, top_k: int | None = None) -> list[dict]:
    """在指定 collection 语义检索,返回 [{content, metadata, score}]。不可用则返回 []。"""
    if not _available():
        return []
    try:
        col = _client().get_or_create_collection(name=collection, embedding_function=_ef())
        res = col.query(query_texts=[query], n_results=top_k or settings.rag_top_k)
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        return [
            {"content": d, "metadata": m, "score": (1 - s) if s is not None else None}
            for d, m, s in zip(docs, metas, dists)
        ]
    except Exception as e:
        logger.warning("rag retrieve(%s) failed: %s", collection, e)
        return []


def build_rag_context(query: str) -> str:
    """检索 components + memory,拼接为 Planner 可用上下文字符串(空则返回 '')。"""
    if not _available():
        return ""
    parts: list[str] = []
    comps = retrieve(query, settings.chroma_collection_components)
    if comps:
        snippets = "\n\n".join(f"- {c['content']}" for c in comps)
        parts.append(f"【组件库参考】\n{snippets}")
    mems = retrieve(query, settings.chroma_collection_memory)
    if mems:
        snippets = "\n\n".join(f"- {m['content']}" for m in mems)
        parts.append(f"【历史记忆】\n{snippets}")
    ctx = "\n\n".join(parts)
    return ctx[:_RAG_INJECT_MAX_CHARS] if ctx else ""


def save_memory(trace_id: str, title: str, content: str, tags: list[str] | None = None) -> None:
    """生成成功后回写 memory 集合(②-a 记忆闭环)。失败仅记录,不阻断。"""
    if not _available():
        return
    try:
        col = _client().get_or_create_collection(
            name=settings.chroma_collection_memory, embedding_function=_ef()
        )
        summary = (title + "\n" + content)[:2000]
        col.upsert(
            ids=[f"mem_{trace_id}"],
            documents=[summary],
            metadatas=[
                {"trace_id": trace_id, "title": title, "tags": ",".join(tags or [])}
            ],
        )
    except Exception as e:
        logger.warning("rag save_memory failed: %s", e)


def get_collection(name: str):
    """获取(或创建)指定 Chroma 集合(供数据准备脚本使用)。"""
    return _client().get_or_create_collection(name=name, embedding_function=_ef())


def seed_components(items: list[dict]) -> int:
    """批量写入 components 集合;items=[{content, metadata}]。返回写入条数。"""
    if not items:
        return 0
    col = get_collection(settings.chroma_collection_components)
    ids = [f"comp_{i}" for i in range(len(items))]
    docs = [it["content"] for it in items]
    metas = [it.get("metadata", {}) for it in items]
    col.upsert(ids=ids, documents=docs, metadatas=metas)
    return len(ids)


# ---- 对话上下文关联(向量相似度边界检测) ----
CTX_COLLECTION = "conversation_context"
CTX_SIMILARITY_THRESHOLD = 0.55  # 余弦相似度 < 0.55 视为无关


def index_message(msg_id: int, conversation_id: int, role: str, content: str) -> None:
    """将消息写入 Chroma 上下文集合(供相似度检测)。"""
    if not _available() or not content.strip():
        return
    try:
        col = _client().get_or_create_collection(name=CTX_COLLECTION, embedding_function=_ef())
        col.upsert(
            ids=[f"msg_{msg_id}"],
            documents=[content[:2000]],
            metadatas=[{"conversation_id": conversation_id, "role": role, "msg_id": msg_id}],
        )
        logger.info("[向量] 索引消息 msg=%s conv=%s role=%s content=%.80s", msg_id, conversation_id, role, content)
    except Exception as e:
        logger.warning("[向量] 索引消息失败 msg=%s: %s", msg_id, e)


def find_relevant_messages(query: str, conversation_id: int, top_k: int = 10) -> list[int]:
    """找与 query 相关的历史消息 id(按相似度排序)。只限同一会话。"""
    if not _available():
        return []
    try:
        col = _client().get_collection(name=CTX_COLLECTION, embedding_function=_ef())
        res = col.query(
            query_texts=[query],
            n_results=min(top_k, 20),
            where={"conversation_id": conversation_id},
        )
        ids_raw = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        relevant = []
        discarded = 0
        for rid, d in zip(ids_raw, dists):
            sim = 1 - d  # 余弦距离 → 相似度
            if sim >= CTX_SIMILARITY_THRESHOLD:
                msg_id = int(rid.replace("msg_", ""))
                relevant.append((msg_id, sim))
            else:
                discarded += 1
        relevant.sort(key=lambda x: x[0])
        result = [r[0] for r in relevant]
        logger.info(
            "[向量] 上下文检索 query=%.60s conv=%s 匹配=%d/%d(阈值=%.2f) ids=%s",
            query, conversation_id, len(result), len(result) + discarded,
            CTX_SIMILARITY_THRESHOLD, result,
        )
        return result
    except Exception as e:
        logger.warning("[向量] 上下文检索失败: %s", e)
        return []
