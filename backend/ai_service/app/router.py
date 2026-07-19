"""Router:意图分类 + Skill 分发(§5.2)。

- detect_intent:先走 LLM 分类器, 失败回退关键词兜底。
- dispatch:流式技能逐 token 转发; 普通技能单次返回。
- unsupported 意图不调 skill, 直接返回 unsupported 事件。
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from .intent_classifier import classify
from .registry import SkillRegistry


logger = logging.getLogger("ai_service.router")

INTENT_SKILL_MAP = {
    "chat": "explain",
    "doc": "generate_doc",
    "generate": "generate_site",
    "modify": "generate_site",  # 复用 generate_site, 带上下文
    "translate": "explain",     # 用 explain + translate system prompt
    "code": "write_code",
}

INTENT_LABELS = {
    "chat": "问答",
    "doc": "文档",
    "generate": "生成网站",
    "modify": "修改网站",
    "translate": "翻译",
    "code": "编程",
    "unsupported": "不支持",
}


def detect_intent(messages: list[dict], model_id: str = "hy3") -> dict:
    """返回 {intent, confidence, label}。"""
    t0 = time.time()
    result = classify(messages, model_id)
    intent = result["intent"]
    elapsed = time.time() - t0
    logger.info(
        "[意图] 识别: %s | 置信度 %s | 耗时 %.1fs",
        INTENT_LABELS.get(intent, intent),
        result.get("confidence", "?"),
        elapsed,
    )
    result["label"] = INTENT_LABELS.get(intent, intent)
    return result


def skill_for(intent: str) -> str | None:
    return INTENT_SKILL_MAP.get(intent)


async def dispatch(
    skill_name: str, model_id: str, messages: list[dict], **kwargs
) -> AsyncGenerator[Any, None]:
    t0 = time.time()
    entry = SkillRegistry.get(skill_name)
    if entry is None:
        yield {"event": "error", "data": {"message": f"Skill '{skill_name}' 不存在"}}
        return

    logger.info("[路由] 分发 -> %s | model=%s", skill_name, model_id)
    handler = entry.handler
    if entry.is_graph or inspect.isasyncgenfunction(handler):
        async for chunk in handler(model_id=model_id, messages=messages, **kwargs):
            yield chunk
    else:
        result = await handler(model_id=model_id, messages=messages, **kwargs)
        yield result

    elapsed = time.time() - t0
    logger.info("[完成] skill=%s 总耗时 %.1fs", skill_name, elapsed)
