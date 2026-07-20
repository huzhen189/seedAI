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
    **extra_kwargs,  # 透传: requirement_doc, project_status, conversation_summary 等
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
    logger.info(
        "[Runner] [1/3] 分发 skill=%s 意图=%s/%s 行业=%s is_graph=%s doc=%s status=%s summary=%s",
        entry.name, intent_val or "-", level2 or "-", industry,
        entry.is_graph,
        "有" if extra_kwargs.get("requirement_doc") else "无",
        extra_kwargs.get("project_status", "?"),
        "有" if extra_kwargs.get("conversation_summary") else "无",
    )

    handler = entry.handler
    t0 = time.time()
    event_cnt = 0
    try:
        if entry.is_graph or inspect.isasyncgenfunction(handler):
            logger.info("[Runner] [2/3] 开始执行 skill=%s (async生成器)", entry.name)
            async for item in handler(
                model_id=model_id,
                messages=messages,
                trace_id=trace_id,
                is_cancelled=is_cancelled,
                intent=intent_val,
                level2=level2,
                industry=industry,
                **extra_kwargs,
            ):
                event_cnt += 1
                if isinstance(item, dict) and "event" in item:
                    yield item
                else:
                    yield ev("token", data=item if isinstance(item, str) else str(item))
        else:
            logger.info("[Runner] [2/3] 开始执行 skill=%s (同步)", entry.name)
            result = await handler(model_id=model_id, messages=messages, trace_id=trace_id)
            event_cnt += 1
            if isinstance(result, dict) and "event" in result:
                yield result
            else:
                yield ev("token", data=result if isinstance(result, str) else str(result))
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error("[Runner] skill=%s 执行异常 耗时=%.0fms 错误=%s: %s",
                    entry.name, elapsed, type(e).__name__, e)
        yield ev("error", message=f"{type(e).__name__}: {e}")
    elapsed = (time.time() - t0) * 1000
    logger.info("[Runner] [3/3] 执行完毕 skill=%s 事件数=%d 耗时=%.0fms", entry.name, event_cnt, elapsed)
    yield ev("done")
