"""RAG 检索增强(②-a · 文档 §7):多集合 Chroma 索引(v0.9.0 六集合扩展)。

- `build_rag_context`:检索 components(组件库)+ memory(历史记忆),拼为 Planner 可用上下文。
- `save_memory`:生成成功后异步回写 memory 集合(记忆闭环)。
- `seed_components`:批量写入 components 集合(数据准备,由 scripts/seed_rag_components.py 调用)。
- 新增集合: user_preferences / project_memory / project_code / error_patterns (§四)

依赖:chromadb + Qwen text-embedding(§7 已配)。embedding key / chroma 不可用时**优雅降级**
(返回空上下文 / 跳过回写),不阻断主生成流。
"""

from __future__ import annotations

import hashlib
import logging

from ..config import settings


logger = logging.getLogger("ai_service.rag")

# 注入 Planner 的 RAG 上下文上限,防 prompt 过长
_RAG_INJECT_MAX_CHARS = 4000


def _client():
    from urllib.parse import urlparse

    import chromadb

    p = urlparse(settings.chroma_url)
    return chromadb.HttpClient(host=p.hostname or "localhost", port=p.port or 8000)


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


def _short_hash(s: str) -> str:
    """稳定短哈希, 用于 Chroma id 后缀."""
    return hashlib.md5(s.encode()).hexdigest()[:8]


# ---- 通用检索 ----

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


def _retrieve_where(query: str, collection: str, where: dict, top_k: int | None = None) -> list[dict]:
    """带 metadata where 过滤的语义检索。"""
    if not _available():
        return []
    try:
        col = _client().get_or_create_collection(name=collection, embedding_function=_ef())
        res = col.query(
            query_texts=[query], n_results=top_k or settings.rag_top_k, where=where,
        )
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        return [
            {"content": d, "metadata": m, "score": (1 - s) if s is not None else None}
            for d, m, s in zip(docs, metas, dists)
        ]
    except Exception as e:
        logger.warning("rag _retrieve_where(%s, %s) failed: %s", collection, where, e)
        return []


def _upsert(collection: str, ids: list[str], docs: list[str], metas: list[dict]) -> None:
    """通用 upsert, 失败仅 warn。"""
    if not _available():
        return
    try:
        col = _client().get_or_create_collection(name=collection, embedding_function=_ef())
        col.upsert(ids=ids, documents=docs, metadatas=metas)
    except Exception as e:
        logger.warning("rag _upsert(%s, %d) failed: %s", collection, len(ids), e)


# ---- 原有集合(components / memory) ----

def build_rag_context(query: str, project_id: int | None = None,
                      user_id: int | None = None) -> str:
    """检索 components + memory,拼接为 Planner 可用上下文字符串(空则返回 '')。
    v0.9.0: 可选 project_id(user_id 预留)过滤 memory, 避免跨项目污染。"""
    if not _available():
        return ""
    parts: list[str] = []
    comps = retrieve(query, settings.chroma_collection_components)
    if comps:
        snippets = "\n\n".join(f"- {c['content']}" for c in comps)
        parts.append(f"【组件库参考】\n{snippets}")
    # memory 检索: 有 project_id 则加 where 过滤
    if project_id is not None:
        mems = _retrieve_where(
            query, settings.chroma_collection_memory,
            where={"project_id": project_id},
        )
    else:
        mems = retrieve(query, settings.chroma_collection_memory)
    if mems:
        snippets = "\n\n".join(f"- {m['content']}" for m in mems)
        parts.append(f"【历史记忆】\n{snippets}")
    ctx = "\n\n".join(parts)
    return ctx[:_RAG_INJECT_MAX_CHARS] if ctx else ""


def save_memory(trace_id: str, title: str, content: str,
                tags: list[str] | None = None,
                project_id: int | None = None,
                user_id: int | None = None) -> None:
    """生成成功后回写 memory 集合(②-a 记忆闭环)。失败仅记录,不阻断。
    v0.9.0: 新增 project_id / user_id (可选, 用于按项目隔离)。"""
    if not _available():
        return
    try:
        col = _client().get_or_create_collection(
            name=settings.chroma_collection_memory, embedding_function=_ef()
        )
        summary = (title + "\n" + content)[:2000]
        meta = {"trace_id": trace_id, "title": title, "tags": ",".join(tags or [])}
        if project_id is not None:
            meta["project_id"] = project_id
        if user_id is not None:
            meta["user_id"] = user_id
        col.upsert(
            ids=[f"mem_{trace_id}"],
            documents=[summary],
            metadatas=[meta],
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


# ============================================================
# v0.9.0 新增集合: user_preferences / project_memory /
#               project_code / error_patterns
# ============================================================

# ---- user_preferences (用户偏好, 按 user_id 隔离) ----

def upsert_user_preference(user_id: int, ptype: str, content: str,
                           importance: int = 3, source: str = "distill") -> None:
    """写入/更新一条用户偏好。id 基于 user_id+type+content hash 做幂等。
    ptype: "style"|"constraint"|"habit"
    """
    if not _available() or not content.strip():
        return
    hash4 = _short_hash(content)
    pid = f"upref_{user_id}_{ptype}_{hash4}"
    doc = content[:2000]
    import time
    _upsert(
        settings.chroma_collection_user_preferences,
        ids=[pid], docs=[doc],
        metas=[{"user_id": user_id, "type": ptype, "importance": importance,
                "source": source, "ts": int(time.time())}],
    )
    logger.info("[向量] 用户偏好 upsert user=%s type=%s hash=%s", user_id, ptype, hash4)


def retrieve_user_preferences(user_id: int, query: str, top_k: int = 5) -> list[dict]:
    """检索用户偏好，按 user_id 隔离。"""
    return _retrieve_where(
        query, settings.chroma_collection_user_preferences,
        where={"user_id": user_id}, top_k=top_k,
    )


# ---- project_memory (项目记忆, 按 project_id 隔离) ----

def upsert_project_memory(project_id: int, user_id: int, ptype: str,
                          content: str, importance: int = 3) -> None:
    """写入/更新一条项目记忆(决策/约束/需求/产物/事实)。
    ptype: "decision"|"constraint"|"requirement"|"artifact"|"fact"
    """
    if not _available() or not content.strip():
        return
    hash4 = _short_hash(content)
    pid = f"pmem_{project_id}_{ptype}_{hash4}"
    doc = content[:2000]
    import time
    _upsert(
        settings.chroma_collection_project_memory,
        ids=[pid], docs=[doc],
        metas=[{"project_id": project_id, "user_id": user_id, "type": ptype,
                "importance": importance, "ts": int(time.time())}],
    )
    logger.info("[向量] 项目记忆 upsert proj=%s type=%s hash=%s", project_id, ptype, hash4)


def retrieve_project_memory(project_id: int, query: str, top_k: int = 5) -> list[dict]:
    """检索项目记忆，按 project_id 隔离。"""
    return _retrieve_where(
        query, settings.chroma_collection_project_memory,
        where={"project_id": project_id}, top_k=top_k,
    )


# ---- project_code (大项目代码语义索引, 按 project_id 隔离, hash 去重) ----

def upsert_project_code(project_id: int, file_path: str, chunk_text: str,
                        chunk_hash: str, function_name: str | None = None,
                        language: str = "html") -> None:
    """写入/更新一个代码块。id 基于 project_id+file_path+chunk_hash 幂等。"""
    if not _available() or not chunk_text.strip():
        return
    pid = f"pcode_{project_id}_{file_path.replace('/', '_')}_{chunk_hash[:8]}"
    doc = chunk_text[:2000]
    import time
    _upsert(
        settings.chroma_collection_project_code,
        ids=[pid], docs=[doc],
        metas=[{"project_id": project_id, "file_path": file_path,
                "function_name": function_name or "", "language": language,
                "chunk_hash": chunk_hash, "ts": int(time.time())}],
    )


def retrieve_project_code(project_id: int, query: str, top_k: int = 8) -> list[dict]:
    """检索项目代码块，按 project_id 隔离。"""
    return _retrieve_where(
        query, settings.chroma_collection_project_code,
        where={"project_id": project_id}, top_k=top_k,
    )


# ---- error_patterns (全局错误模式库, 跨项目共享) ----

def upsert_error_pattern(error_type: str, trigger_pattern: str, fix_pattern: str,
                         language: str = "general") -> None:
    """写入/更新一条错误模式(修复确认后调用)。success_count += 1 在业务层处理。"""
    if not _available():
        return
    hash4 = _short_hash(error_type)
    eid = f"err_{hash4}"
    doc = f"{trigger_pattern} → {fix_pattern}"
    import time
    _upsert(
        settings.chroma_collection_error_patterns,
        ids=[eid], docs=[doc[:2000]],
        metas=[{"error_type": error_type, "trigger_pattern": trigger_pattern,
                "fix_pattern": fix_pattern, "language": language,
                "success_count": 1, "ts": int(time.time())}],
    )
    logger.info("[向量] 错误模式 upsert type=%s hash=%s", error_type, hash4)


def retrieve_error_patterns(query: str, top_k: int = 5) -> list[dict]:
    """检索全局错误模式(无需隔离 where)。"""
    return retrieve(query, settings.chroma_collection_error_patterns, top_k=top_k)


# ---- 批量种子(错误模式库冷启动) ----

_ERROR_SEEDS: list[dict] = [
    {"error_type": "flex overflow", "trigger": "display:flex 无 min-width:0",
     "fix": "加 min-width:0 或 overflow:hidden 到 flex 子元素", "language": "css"},
    {"error_type": "z-index stacking", "trigger": "z-index 无定位上下文",
     "fix": "确保父元素 position:relative 且非 auto z-index", "language": "css"},
    {"error_type": "grid overflow", "trigger": "grid 列使用 1fr 但内容超宽",
     "fix": "加 minmax(0,1fr) 或 overflow:hidden", "language": "css"},
    {"error_type": "button hover missing", "trigger": "按钮无 :hover/:focus 样式",
     "fix": "添加 hover 变色 + focus-visible 轮廓", "language": "css"},
    {"error_type": "img alt missing", "trigger": "<img> 标签无 alt 属性",
     "fix": "添加描述性 alt 文本", "language": "html"},
    {"error_type": "missing viewport meta", "trigger": "无 <meta name='viewport'>",
     "fix": "加 <meta name='viewport' content='width=device-width,initial-scale=1'>", "language": "html"},
    {"error_type": "semantic heading skip", "trigger": "h1→h3 跳级(无 h2)",
     "fix": "使用顺序标题层级 h1→h2→h3", "language": "html"},
    {"error_type": "color contrast low", "trigger": "浅色文字在浅色背景上(对比度<4.5)",
     "fix": "调深文字色或加深背景,确保 WCAG AA 对比度≥4.5", "language": "css"},
    {"error_type": "responsive breakpoint missing", "trigger": "固定宽度 px 值在移动端溢出",
     "fix": "使用 max-width+百分比 或 @media 断点适配", "language": "css"},
    {"error_type": "CSS variable fallback missing", "trigger": "var(--custom) 无回退值",
     "fix": "加 var(--custom, fallback) 确保旧浏览器兼容", "language": "css"},
    {"error_type": "form label missing", "trigger": "<input> 无关联 <label>",
     "fix": "添加 <label for='id'> 或 aria-label", "language": "html"},
    {"error_type": "nav accessibility", "trigger": "导航无 <nav> 标签或 aria 属性",
     "fix": "用 <nav aria-label='主导航'> 包裹导航链接", "language": "html"},
    {"error_type": "section no heading", "trigger": "<section> 无标题元素",
     "fix": "每个 <section> 包含一个 h2-h6 标题", "language": "html"},
    {"error_type": "hover only interaction", "trigger": "仅 :hover 触发交互无 :focus",
     "fix": "同时添加 :focus 或 :focus-visible 支持键盘导航", "language": "css"},
    {"error_type": "absolute positioning no relative parent", "trigger": "position:absolute 无 position:relative 父元素",
     "fix": "给直接父元素加 position:relative", "language": "css"},
    {"error_type": "font stack no fallback", "trigger": "font-family 只有自定义字体无系统兜底",
     "fix": "加 sans-serif / serif 系统字体兜底", "language": "css"},
    {"error_type": "click target too small", "trigger": "可点击元素 < 44x44px",
     "fix": "设 min-width/min-height:44px 或 padding 扩大点击区", "language": "css"},
    {"error_type": "animation no reduce-motion", "trigger": "动画无 prefers-reduced-motion 适配",
     "fix": "用 @media(prefers-reduced-motion:reduce) 禁用/减弱动画", "language": "css"},
    {"error_type": "link text not descriptive", "trigger": "链接文字为'点击这里''了解更多'",
     "fix": "使用描述性链接文字如'查看产品文档'", "language": "html"},
    {"error_type": "page title missing", "trigger": "<title> 缺失或为空",
     "fix": "每个页面包含描述性 <title>", "language": "html"},
]


def seed_error_patterns() -> int:
    """写入错误模式种子数据(冷启动)。幂等: 同 error_type 覆盖。返回写入条数。"""
    if not _available():
        return 0
    count = 0
    import time
    now = int(time.time())
    for item in _ERROR_SEEDS:
        hash4 = _short_hash(item["error_type"])
        _upsert(
            settings.chroma_collection_error_patterns,
            ids=[f"err_{hash4}"],
            docs=[f"{item['trigger']} → {item['fix']}"],
            metas=[{"error_type": item["error_type"],
                    "trigger_pattern": item["trigger"],
                    "fix_pattern": item["fix"],
                    "language": item["language"],
                    "success_count": 0, "ts": now}],
        )
        count += 1
    logger.info("[向量] 错误模式种子写入 %d 条", count)
    return count
