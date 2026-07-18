"""Skill: write_code(写/改代码片段 · 单次 LLM 直出 · §5.2)。

成熟来源:单 LLM 调用(ReAct 思路的简化形态,§5.7「仅作简单 skill 实现思路」)。
非图技能(is_graph=False):一次 PROVIDERS[model_id] 调用即返回。
"""

from __future__ import annotations

from ..providers import get_chat_model
from ..registry import register_skill


SYS_WRITE = (
    "你是一名资深工程师。根据用户需求直接产出代码片段,"
    "只输出代码本身(必要时加极简注释),不要冗长解释。"
)


async def write_code_skill(model_id: str, messages: list, **kwargs) -> str:
    chat = get_chat_model(model_id, streaming=False)
    resp = chat.invoke([{"role": "system", "content": SYS_WRITE}, *messages])
    return resp.content


register_skill(
    name="write_code",
    intent_tags=["代码", "code", "脚本", "函数", "snippet", "编程"],
    handler=write_code_skill,
    is_graph=False,
    description="写/改代码片段(单次 LLM 直出)",
)
