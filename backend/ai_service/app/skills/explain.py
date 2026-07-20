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
    "casual": "你叫小胡，是一个友好的AI编程助手。你对用户自称「小胡」，用轻松自然的语气回答。如果有人问你是谁，你说你是小胡，一个智能编程助手。",
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

    # 搜索增强: 取最后一条用户消息, 尝试联网搜索, 拼接结果到 system prompt
    user_query = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_query = m.get("content", "") or ""
            break
    if user_query:
        try:
            from ..tools.web_search import web_search
            search_res = await web_search(user_query, top_k=3)
            if search_res.get("ok") and search_res.get("results"):
                snippets = []
                for r in search_res["results"][:3]:
                    sn = f"- {r['title']}: {r['snippet']}"[:300]
                    if r.get("url"):
                        sn += f" ({r['url']})"
                    snippets.append(sn)
                if snippets:
                    sys_prompt += (
                        f"\n\n【联网搜索结果（{search_res.get('provider','?')}）】\n"
                        + "\n".join(snippets)
                        + "\n请结合以上实时信息回答用户的问题。如果搜索结果不相关，请用自己的知识回答。"
                    )
                    SKILL_LOG.info("[chat] 搜索增强 trace=%s provider=%s hits=%d", trace_id, search_res.get("provider"), len(snippets))
        except Exception as e:
            SKILL_LOG.debug("[chat] 搜索增强跳过 trace=%s: %s", trace_id, e)

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
    intent_tags=["解释", "问答", "问", "debug", "对比", "翻译"],
    handler=explain_skill,
    is_graph=False,
    description="解释/问答/调试/翻译(按 level2 切 prompt)",
)
