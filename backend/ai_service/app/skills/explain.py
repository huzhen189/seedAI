"""Skill: explain(解释/问答 · 单次 LLM 直出 · §5.2)。"""

from __future__ import annotations

from ..providers import ModelUnavailableError, get_chat_model, resolve_fallback_order
from ..registry import register_skill


SYS_EXPLAIN = "你是一名耐心、严谨的助手。回答用户问题,给出准确、易懂的解释,必要时举例。"


async def explain_skill(model_id: str, messages: list, **kwargs) -> str:
    try:
        chat = get_chat_model(model_id, streaming=False)
        resp = chat.invoke([{"role": "system", "content": SYS_EXPLAIN}, *messages])
        return resp.content
    except Exception as e:
        order = resolve_fallback_order(model_id)
        suggested = [m for m in order if m != model_id]
        raise ModelUnavailableError(
            failed=model_id, message=f"模型 {model_id} 不可用: {e}", suggested=suggested
        ) from e


register_skill(
    name="explain",
    intent_tags=["解释", "问答", "问", "什么", "怎么", "为什么", "explain", "问问题"],
    handler=explain_skill,
    is_graph=False,
    description="解释/问答(单次 LLM 直出)",
)
