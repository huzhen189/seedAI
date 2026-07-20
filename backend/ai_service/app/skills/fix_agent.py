"""Fix Agent: Bug 修复 / 错误排查(直接给代码级修复方案)。"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Dict

from ..providers import astream_with_fallback
from ..registry import register_skill

AGENT_LOG = logging.getLogger("ai_service.fix")

SYS_FIX = (
    "你叫小胡，是智能建站助手的「HTML修复专家」。只修复前端网页代码(HTML/CSS/JS)。\n"
    "1. 定位问题根因\n2. 给出修复方案\n3. 输出修复后的完整HTML代码\n"
    "用中文回答，代码用 ```html 包裹。不涉及后端或非网页技术。"
)



async def fix_agent_handler(
    model_id: str, messages: list, trace_id: str | None = None,
    is_cancelled=None, **kwargs,
) -> AsyncGenerator[Dict, None]:
    AGENT_LOG.info("[fix] 修复任务 trace=%s", trace_id)
    full = []
    async for chunk, _ in astream_with_fallback(model_id, messages, system=SYS_FIX):
        text = getattr(chunk, "content", chunk)
        if text:
            full.append(text)
            yield {"type": "token", "data": text}
    AGENT_LOG.info("[fix] 完成 chars=%d", len("".join(full)))

register_skill(
    name="fix_agent",
    intent_tags=["修复", "报错", "bug", "不生效", "改改", "出错", "error", "fix", "修", "改一下", "改下"],
    handler=fix_agent_handler,
    is_graph=False,
    description="Bug修复: 错误排查 + 代码级修复方案",
)
register_skill(name="fix_agent",intent_tags=["修复","报错","bug","不生效","改改","出错","error","fix","修","改一下","改下"],handler=fix_agent_handler,is_graph=False,description="Bug修复: 错误排查 + 代码级修复方案")
