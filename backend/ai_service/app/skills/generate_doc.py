"""Skill: generate_doc(生成文档/Markdown · 流式 SSE 输出 · §5.2)。"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any, Dict, Optional

from ..events import ev
from ..providers import (
    ModelUnavailableError,
    astream_with_fallback,
    resolve_fallback_order,
)
from ..registry import register_skill


SYS_DOC = (
    "你是一名技术文档工程师。根据用户需求产出清晰、结构化的文档, 使用 Markdown 格式。"
    "如果用户没有指定输出格式, 默认输出 .md。"
)

GEN_LOG = logging.getLogger("ai_service.generate")


async def _cancelled_now(fn) -> bool:
    import inspect
    if not fn:
        return False
    res = fn()
    if inspect.isawaitable(res):
        return bool(await res)
    return bool(res)


async def generate_doc_skill(
    model_id: str,
    messages: list,
    trace_id: Optional[str] = None,
    is_cancelled=None,
    **kwargs,
) -> AsyncGenerator[Dict[str, Any], None]:
    """流式生成文档, 支持取消 + 模型回退。"""
    GEN_LOG.info("[doc] 开始 trace=%s model=%s", trace_id, model_id)
    yield ev("node", stage="writing")
    parts: list[str] = []
    try:
        async for chunk, _ in astream_with_fallback(
            model_id, messages, system=SYS_DOC
        ):
            if await _cancelled_now(is_cancelled):
                yield ev("aborted")
                return
            text = getattr(chunk, "content", chunk)
            if text:
                parts.append(text)
                yield ev("token", data=text)
    except ModelUnavailableError as e:
        GEN_LOG.warning("[doc] 模型不可用 trace=%s: %s", trace_id, e)
        yield ev(
            "retry",
            failed=e.failed,
            suggested=e.suggested,
            message=str(e),
        )
        yield ev("aborted")
        return

    GEN_LOG.info("[doc] 完成 trace=%s chars=%s", trace_id, len("".join(parts)))
    yield ev("node", stage="done")


register_skill(
    name="generate_doc",
    intent_tags=["文档", "doc", "说明", "教程", "readme", "wiki", "方案", "计划"],
    handler=generate_doc_skill,
    is_graph=True,  # 改为异步生成器
    description="生成文档/说明(Markdown, 流式输出)",
)
