"""Skill: write_code(编码 · 按 level2 切 System Prompt · §5.2)。"""

from __future__ import annotations

import logging
import time

from ..providers import ModelUnavailableError, get_chat_model, resolve_fallback_order
from ..registry import register_skill


# 按 level2 子意图切换 System Prompt
SYS_PROMPTS: dict[str, str] = {
    "snippet": "你是算法专家。只输出内联代码片段，带极简中文注释。不要解释。",
    "component": "你是前端组件专家。输出完整组件代码(HTML/CSS/JS)，含样式和使用示例。",
    "fix": "你是 Bug 修复专家。标注问题根因和修复后的代码，用 diff 风格示意改动。",
    "refactor": "你是代码质量专家。输出重构后的代码，附带改进点列表和优化原因。",
    "code_lang": "你是跨语言翻译专家。把代码逐行准确地翻译到目标语言，保持逻辑不变。",
}
SYS_DEFAULT = SYS_PROMPTS["snippet"]

SKILL_LOG = logging.getLogger("ai_service.write_code")


async def write_code_skill(
    model_id: str,
    messages: list,
    trace_id: str | None = None,
    level2: str | None = None,
    **kwargs,
) -> str:
    sys_prompt = SYS_PROMPTS.get(level2 or "", SYS_DEFAULT)
    SKILL_LOG.info("[code] 编程开始 trace=%s model=%s level2=%s", trace_id, model_id, level2)
    t0 = time.time()
    try:
        chat = get_chat_model(model_id, streaming=False)
        resp = chat.invoke([{"role": "system", "content": sys_prompt}, *messages])
        result = resp.content
        elapsed = time.time() - t0
        SKILL_LOG.info("[code] 编程完成 trace=%s chars=%s 耗时 %.1fs", trace_id, len(result), elapsed)
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
    handler=write_code_skill,
    description="编码实战(代码片段/组件/Bug修复/重构/跨语言翻译, 按 level2 切 prompt)",
)
