"""Skill 运行包装(§5.2 / §5.5)。"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncGenerator
from typing import Any, Callable, Dict, Optional

from .events import ev
from .registry import SkillRegistry


logger = logging.getLogger("ai_service.runner")


async def run_skill(
    skill_name: str,
    model_id: str,
    messages: list,
    *,
    trace_id: Optional[str] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
    intent_info: Optional[dict] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """统一入口:意图 → 分发 → Skill 执行 → done。"""
    logger.info(
        "▶ 开始执行 trace=%s skill=%s model=%s intent=%s/%s msgs=%d",
        trace_id, skill_name, model_id,
        intent_info.get("level1") if intent_info else "?",
        intent_info.get("level2") if intent_info else "?",
        len(messages),
    )
    yield ev("node", stage="enter_router", agent_id=skill_name)

    # 意图信息透传给前端(两级 + 行业)
    if intent_info:
        yield ev(
            "intent",
            level1=intent_info.get("level1"),
            level2=intent_info.get("level2"),
            label=intent_info.get("label"),
            level1_label=intent_info.get("level1_label"),
            level2_label=intent_info.get("level2_label"),
            confidence=intent_info.get("confidence"),
            industry=intent_info.get("industry"),
            agent_id=skill_name,
        )

    # unsupported: 直接返回提示
    if intent_info and intent_info.get("level1") == "unsupported":
        logger.info("◼ trace=%s 不支持该意图, 返回提示", trace_id)
        yield ev("node", stage="unsupported", message="暂不支持此功能, 请尝试其他类型请求")
        yield ev("done")
        return

    entry = SkillRegistry.get(skill_name)
    if entry is None:
        logger.warning("trace=%s Skill '%s' 未注册", trace_id, skill_name)
        yield ev("error", message=f"Skill '{skill_name}' 不存在")
        yield ev("done")
        return

    logger.info(
        "▸ trace=%s 分发到 Skill: %s(is_graph=%s)",
        trace_id, entry.name, entry.is_graph,
    )
    yield ev("node", stage="dispatch", skill=entry.name, agent_id=skill_name)

    # 参数透传(供 handler 按 level2/industry 调整行为)
    level2 = intent_info.get("level2") if intent_info else None
    industry = intent_info.get("industry", "other") if intent_info else "other"
    intent_val = intent_info.get("level1") if intent_info else None

    handler = entry.handler
    try:
        if entry.is_graph or inspect.isasyncgenfunction(handler):
            async for item in handler(
                model_id=model_id,
                messages=messages,
                trace_id=trace_id,
                is_cancelled=is_cancelled,
                intent=intent_val,
                level2=level2,
                industry=industry,
            ):
                if isinstance(item, dict) and "event" in item:
                    yield item
                else:
                    yield ev("token", data=item if isinstance(item, str) else str(item))
        else:
            result = await handler(model_id=model_id, messages=messages, trace_id=trace_id)
            if isinstance(result, dict) and "event" in result:
                yield result
            else:
                yield ev("token", data=result if isinstance(result, str) else str(result))
    except Exception as e:
        logger.warning("Skill '%s' 异常 trace=%s: %s", skill_name, trace_id, e)
        yield ev("error", message=f"{type(e).__name__}: {e}")
    logger.info("◼ trace=%s 执行完毕, 发送 done", trace_id)
    yield ev("done")
