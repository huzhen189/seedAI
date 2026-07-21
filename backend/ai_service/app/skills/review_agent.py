"""Review Agent: 代码评审 / 优化建议(性能/SEO/可访问性)。"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Dict

from ..providers import astream_with_fallback
from ..registry import register_skill

AGENT_LOG = logging.getLogger("ai_service.review")

SYS_REVIEW = (
    "你叫小胡，是智能建站助手的「HTML代码评审专家」，只评审前端网页代码。分析HTML/CSS/JS给出：\n"
    "1. 性能优化(加载速度/资源大小)\n2. SEO改进(语义化/meta标签/结构化数据)\n"
    "3. 可访问性(ARIA/对比度/键盘导航)\n4. 代码质量(可读性/重复/最佳实践)\n"
    "用中文回答，按优先级排序。只涉及前端HTML网页，不涉及后端。"
)



async def review_agent_handler(
    model_id: str, messages: list, trace_id: str | None = None,
    is_cancelled=None, **kwargs,
) -> AsyncGenerator[Dict, None]:
    AGENT_LOG.info("[review] 代码评审 trace=%s", trace_id)
    full = []
    async for chunk, _ in astream_with_fallback(model_id, messages, system=SYS_REVIEW):
        text = getattr(chunk, "content", chunk)
        if text:
            full.append(text)
            yield {"type": "token", "data": text}
    AGENT_LOG.info("[review] 完成 chars=%d", len("".join(full)))

register_skill(name="review_agent", display_name="评审小胡", avatar="🔍", role="代码评审", intent_tags=["优化","评审","review","建议","检查","看看","帮忙看","能不能更好"], handler=review_agent_handler, is_graph=False, description="代码评审: 性能/SEO/可访问性/代码质量")
