"""Tool: rag_retrieve(向量检索 · Chroma + Qwen text-embedding · §7)。

成熟来源:
  - 向量库:chromadb(成熟开源向量数据库,HTTP client 模式)
  - 嵌入:Qwen text-embedding(DashScope,OpenAI 兼容模式,§7 已定)
双入口(§5.9):
  - scope=user_exposed  → 用户说"检索我的组件库"时由 Router 分发到同名 Skill
  - 内部 agent          → generate_site 的 Planner 检索上下文增强质量
说明:M0 不接向量检索,未配 embedding key / chroma 不可用时返回清晰错误,不阻塞启动。
"""
from __future__ import annotations

from ..config import settings
from ..registry import tool


@tool(
    name="rag_retrieve",
    scope="user_exposed",
    risk="safe",
    description="向量检索组件库/记忆(§7)。返回 Top-K 相关片段(含相似度分数),用于增强生成或用户显式检索。",
    schema={
        "type": "function",
        "function": {
            "name": "rag_retrieve",
            "description": "在指定 Chroma collection 中做语义检索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索查询(用户需求或关键词)"},
                    "collection": {
                        "type": "string",
                        "description": "集合名:components(组件库)/ memory(用户记忆)/ cache_gen(语义缓存)",
                        "enum": ["components", "memory", "cache_gen"],
                    },
                    "top_k": {"type": "integer", "description": "返回条数,默认取配置 rag_top_k"},
                },
                "required": ["query"],
            },
        },
    },
)
async def rag_retrieve(query: str, collection: str = "components", top_k: int | None = None) -> dict:
    top_k = top_k or settings.rag_top_k
    try:
        import chromadb
        from chromadb.utils import embedding_functions
    except ImportError:
        return {"ok": False, "error": "chromadb 未安装(pip install chromadb)", "results": []}
    if not settings.qwen_embedding_key:
        return {
            "ok": False,
            "error": "未配置 qwen_embedding_key,RAG 暂不可用(§7, M1 落地)",
            "results": [],
        }
    try:
        client = chromadb.HttpClient(base_url=settings.chroma_url)
        ef = embedding_functions.OpenAIEmbeddingFunction(
            api_key=settings.qwen_embedding_key,
            api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_name=settings.qwen_embedding_model,
        )
        col = client.get_or_create_collection(name=collection, embedding_function=ef)
        res = col.query(query_texts=[query], n_results=top_k)
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        results = [
            {
                "content": d,
                "metadata": m,
                "score": (1 - s) if s is not None else None,
            }
            for d, m, s in zip(docs, metas, dists)
        ]
        return {"ok": True, "collection": collection, "results": results}
    except Exception as e:
        return {"ok": False, "error": f"检索失败:{e}", "results": []}
