"""Router:意图识别 + Skill 分发(§5.2,运行于核心 AI 服务)。

- 轻量规则匹配:基于 SkillRegistry.match(intent_tags 子串命中),零额外调用。
- 生产可替换为一次小模型调用(§5.8),对外接口不变(detect_intent -> skill 名)。
- dispatch:流式技能(is_graph / async generator)逐 token 转发;普通技能单次返回。
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from typing import Any

from .registry import SkillRegistry


def detect_intent(messages: list[dict]) -> str:
    """从最近一条 user 消息判定意图,返回 Skill 名;无命中兜底 generate_site。"""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "")
            break
    entry = SkillRegistry.match(last)
    return entry.name if entry else "generate_site"


async def dispatch(
    skill_name: str, model_id: str, messages: list[dict], **kwargs
) -> AsyncGenerator[Any, None]:
    entry = SkillRegistry.get(skill_name) or SkillRegistry.get("generate_site")
    if entry is None:
        yield "[error] 无可用 Skill"
        return
    handler = entry.handler
    if entry.is_graph or inspect.isasyncgenfunction(handler):
        async for chunk in handler(model_id=model_id, messages=messages, **kwargs):
            yield chunk
    else:
        result = await handler(model_id=model_id, messages=messages, **kwargs)
        yield result
