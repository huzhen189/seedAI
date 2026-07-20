"""Design Agent: 前端设计顾问(配色/布局/字体/动效建议)。"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Dict

from ..providers import astream_with_fallback
from ..registry import register_skill

AGENT_LOG = logging.getLogger("ai_service.design")

SYS_DESIGN = (
    "你叫小胡，是智能建站助手的「设计顾问」，只做HTML网页前端设计咨询。"
    "根据用户需求给出配色方案(CSS变量)、布局建议(Flex/Grid)、字体推荐(Google Fonts)、交互动效方案。"
    "不涉及后端/运维，只输出前端HTML/CSS可直接使用的方案。用中文回答。"
)


@register_skill(
    name="design_agent",
    intent_tags=["设计", "配色", "布局", "字体", "动效", "颜色", "样式", "主题", "排版"],
    handler="design_agent_handler",
    is_graph=False,
    description="设计顾问: 配色/布局/字体/动效建议",
)
async def design_agent_handler(
    model_id: str, messages: list, trace_id: str | None = None,
    is_cancelled=None, **kwargs,
) -> AsyncGenerator[Dict, None]:
    AGENT_LOG.info("[design] 设计咨询 trace=%s", trace_id)
    full = []
    async for chunk, _ in astream_with_fallback(model_id, messages, system=SYS_DESIGN):
        text = getattr(chunk, "content", chunk)
        if text:
            full.append(text)
            yield {"type": "token", "data": text}
    AGENT_LOG.info("[design] 完成 chars=%d", len("".join(full)))
