"""Skill: rag_retrieve(用户显式触发的检索 · §5.9「双入口」)。

同一实现的两个入口:
  - 对外 Skill(本文件):用户说"检索我的组件库"/"搜索记忆" → Router 分发到此,返回片段给用户
  - 对内 Tool(tools.rag_retrieve):generate_site 的 Planner 检索上下文增强生成质量
本质复用 tools.rag_retrieve.rag_retrieve 同一函数(§5.9 澄清)。
"""
from __future__ import annotations

from ..tools.rag_retrieve import rag_retrieve
from ..registry import register_skill


async def rag_retrieve_skill(model_id: str, messages: list, **kwargs) -> dict:
    query = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            query = m.get("content", "")
            break
    collection = kwargs.get("collection", "components")
    return await rag_retrieve(query, collection=collection)


register_skill(
    name="rag_retrieve",
    intent_tags=["检索", "组件库", "搜索我的", "找组件", "记忆", "搜一下"],
    handler=rag_retrieve_skill,
    is_graph=False,
    description="向量检索组件库/记忆(用户显式触发),返回相关片段",
)
