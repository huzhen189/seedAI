"""Search Agent: 联网搜索增强问答(DuckDuckGo + 关键词概括)。"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from typing import Dict

from ..providers import astream_with_fallback, get_chat_model
from ..registry import register_skill

AGENT_LOG = logging.getLogger("ai_service.search")

SYS_SEARCH = (
    "你叫小胡，是智能建站助手的「搜索专家」。根据搜索结果回答网页前端/HTML建站相关问题。"
    "只涉及网页前端技术，不涉及后端、App、游戏等。用中文。"
)

SYS_SUMMARIZE = "把以下网页内容总结为 3 条关键信息,每条 ≤80 字。用中文。"


def _duckduckgo(query: str, max_results: int = 3) -> str:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            return "\n".join(f"{i+1}. {r['title']}: {r['body'][:200]}" for i, r in enumerate(results))
    except Exception as e:
        AGENT_LOG.warning("DuckDuckGo search failed: %s", e)
        return ""


@register_skill(
    name="search_agent",
    intent_tags=["搜索", "查", "搜", "search", "找一下", "帮我查", "最新", "最近有什么"],
    handler="search_agent_handler",
    is_graph=False,
    description="联网搜索: 查资料/最新资讯",
)
async def search_agent_handler(
    model_id: str, messages: list, trace_id: str | None = None,
    is_cancelled=None, **kwargs,
) -> AsyncGenerator[Dict, None]:
    AGENT_LOG.info("[search] 联网搜索 trace=%s", trace_id)
    # 取最后一条用户消息作为搜索词
    query = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            query = m.get("content", "") or ""
            break
    if not query:
        yield {"type": "token", "data": "请输入要搜索的内容。"}
        return
    yield {"type": "think", "data": f"正在搜索: {query[:50]}..."}
    raw = _duckduckgo(query)
    if not raw:
        raw = "搜索暂无结果, 请换个关键词试试。"
    search_ctx = f"搜索结果:\n{raw}\n\n用户问题: {query}"
    full = []
    async for chunk, _ in astream_with_fallback(
        model_id, [{"role": "user", "content": search_ctx}], system=SYS_SEARCH
    ):
        text = getattr(chunk, "content", chunk)
        if text:
            full.append(text)
            yield {"type": "token", "data": text}
    AGENT_LOG.info("[search] 完成 chars=%d", len("".join(full)))
