"""Skill: write_code(写/改代码片段 · 单次 LLM 直出 · §5.2)。

成熟来源:单 LLM 调用(ReAct 思路的简化形态,§5.7「仅作简单 skill 实现思路」)。
非图技能(is_graph=False):一次 PROVIDERS[model_id] 调用即返回。
"""

from __future__ import annotations

import logging
import time

from ..providers import ModelUnavailableError, get_chat_model, resolve_fallback_order
from ..registry import register_skill


SYS_WRITE = (
    "你是一名资深工程师。根据用户需求直接产出代码片段,"
    "只输出代码本身(必要时加极简注释),不要冗长解释。"
)

SKILL_LOG = logging.getLogger("ai_service.write_code")


async def write_code_skill(model_id: str, messages: list, **kwargs) -> str:
    trace_id = kwargs.get("trace_id", "-")
    SKILL_LOG.info("[code] 编程开始 trace=%s model=%s", trace_id, model_id)
    t0 = time.time()
    try:
        chat = get_chat_model(model_id, streaming=False)
        resp = chat.invoke([{"role": "system", "content": SYS_WRITE}, *messages])
        result = resp.content
        elapsed = time.time() - t0
        SKILL_LOG.info(
            "[code] 编程完成 trace=%s chars=%s 耗时 %.1fs",
            trace_id, len(result), elapsed,
        )
        return result
    except Exception as e:
        order = resolve_fallback_order(model_id)
        suggested = [m for m in order if m != model_id]
        SKILL_LOG.warning("[code] 模型不可用 trace=%s: %s", trace_id, e)
        raise ModelUnavailableError(
            failed=model_id, message=f"模型 {model_id} 不可用: {e}", suggested=suggested
        ) from e


register_skill(
    name="write_code",
    intent_tags=["代码", "code", "脚本", "函数", "snippet", "编程"],
    handler=write_code_skill,
    is_graph=False,
    description="写/改代码片段(单次 LLM 直出)",
)
