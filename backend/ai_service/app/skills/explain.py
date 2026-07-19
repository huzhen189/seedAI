"""Skill: explain(解释/问答 · 按 level2 切 System Prompt · §5.1)。"""

from __future__ import annotations

import logging
import time

from ..providers import ModelUnavailableError, get_chat_model, resolve_fallback_order
from ..registry import register_skill


# 按 level2 子意图切换 System Prompt
SYS_PROMPTS: dict[str, str] = {
    "explain": "你是编程入门导师，用通俗易懂的中文解释概念和知识点，多举实际例子。",
    "debug": "你是资深 Debug 专家。根据用户贴的错误信息给出根因分析、修复方案和预防建议。用中文。",
    "compare": "你是技术选型顾问。对比用户提到的技术方案，列出各自优劣、适用场景和推荐。用中文。",
    "casual": "你是友好的编程助手，轻松自然地回答用户的日常问题。",
    "text": "你是翻译专家。把用户提供的文本准确翻译到目标语言，只输出翻译结果。",
}
SYS_DEFAULT = SYS_PROMPTS["explain"]

SKILL_LOG = logging.getLogger("ai_service.explain")


async def explain_skill(
    model_id: str,
    messages: list,
    trace_id: str | None = None,
    level2: str | None = None,
    **kwargs,
) -> str:
    sys_prompt = SYS_PROMPTS.get(level2 or "", SYS_DEFAULT)
    SKILL_LOG.info("[chat] 问答开始 trace=%s model=%s level2=%s", trace_id, model_id, level2)
    t0 = time.time()
    try:
        chat = get_chat_model(model_id, streaming=False)
        resp = chat.invoke([{"role": "system", "content": sys_prompt}, *messages])
        result = resp.content
        elapsed = time.time() - t0
        SKILL_LOG.info("[chat] 回答完成 trace=%s chars=%s 耗时 %.1fs", trace_id, len(result), elapsed)
        return result
    except Exception as e:
        order = resolve_fallback_order(model_id)
        suggested = [m for m in order if m != model_id]
        SKILL_LOG.warning("[chat] 模型不可用 trace=%s: %s", trace_id, e)
        raise ModelUnavailableError(
            failed=model_id, message=f"模型 {model_id} 不可用: {e}", suggested=suggested
        ) from e


register_skill(
    name="explain",
    handler=explain_skill,
    description="解释/问答/调试/翻译(按 level2 切 prompt)",
)
