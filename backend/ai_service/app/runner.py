"""Skill 运行包装(§5.2 / §5.5)。

run_skill() 是统一的「一次生成请求 → 事件流」入口:
- 产出 router 级 node 事件(enter_router / dispatch)
- 调用目标 Skill 的 handler,并把其输出归一化为事件流:
  * handler 是异步生成器 → 逐 item 产出(若 item 自带 event 则原样透传,否则包成 token)
  * handler 是普通协程 → 其结果包成 token 事件
- 末尾产出 done(异常时先产 error 再 done,保证 SSE 流一定有关闭帧)
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from typing import Any, Callable, Dict, Optional

from .events import ev
from .registry import SkillRegistry


async def run_skill(
    skill_name: str,
    model_id: str,
    messages: list,
    *,
    trace_id: Optional[str] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    yield ev("node", stage="enter_router")

    entry = SkillRegistry.get(skill_name) or SkillRegistry.get("generate_site")
    if entry is None:
        yield ev("error", message="无可用 Skill")
        yield ev("done")
        return

    yield ev("node", stage="dispatch", skill=entry.name)

    handler = entry.handler
    try:
        if entry.is_graph or inspect.isasyncgenfunction(handler):
            async for item in handler(
                model_id=model_id,
                messages=messages,
                trace_id=trace_id,
                is_cancelled=is_cancelled,
            ):
                if isinstance(item, dict) and "event" in item:
                    yield item
                else:
                    yield ev("token", data=item if isinstance(item, str) else str(item))
        else:
            result = await handler(model_id=model_id, messages=messages)
            if isinstance(result, dict) and "event" in result:
                yield result
            else:
                yield ev("token", data=result if isinstance(result, str) else str(result))
    except Exception as e:  # 不应让异常裸奔中断 SSE;以 error 事件结束
        yield ev("error", message=f"{type(e).__name__}: {e}")
    yield ev("done")
