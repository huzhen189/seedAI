"""Skill: generate_doc(生成文档/说明 · 单次 LLM 直出 · §5.2)。"""

from __future__ import annotations

from ..providers import ModelUnavailableError, get_chat_model, resolve_fallback_order
from ..registry import register_skill


SYS_DOC = "你是一名技术文档工程师。根据用户需求产出清晰、结构化的文档/说明,使用 Markdown。"


async def generate_doc_skill(model_id: str, messages: list, **kwargs) -> str:
    try:
        chat = get_chat_model(model_id, streaming=False)
        resp = chat.invoke([{"role": "system", "content": SYS_DOC}, *messages])
        return resp.content
    except Exception as e:
        order = resolve_fallback_order(model_id)
        suggested = [m for m in order if m != model_id]
        raise ModelUnavailableError(
            failed=model_id, message=f"模型 {model_id} 不可用: {e}", suggested=suggested
        ) from e


register_skill(
    name="generate_doc",
    intent_tags=["文档", "doc", "说明", "教程", "readme", "wiki"],
    handler=generate_doc_skill,
    is_graph=False,
    description="生成文档/说明(单次 LLM 直出)",
)
