"""Skill: explain(解释/问答 · 按 level2 切 System Prompt · §5.1)。"""

from __future__ import annotations

import logging
import time

from ..providers import ModelUnavailableError, get_chat_model, resolve_fallback_order
from ..registry import register_skill


# 按 level2 子意图切换 System Prompt
# 统一 System Prompt（小胡：纯HTML前端建站助手）
SYS_BASE = (
    "你叫小胡，是一个智能建站助手。你**只做网页HTML前端开发**，不涉及后端、数据库、服务器、App、游戏引擎、数据分析、运维部署等。\n"
    "核心能力：从零搭建单文件HTML网站、改造已有页面、生成HTML/CSS/JS代码组件。\n"
    "工作流程：先深度理解需求（行业/风格/功能），再出内容方案等用户确认，最后生成代码。\n"
    "始终用中文交流，自称「小胡」。\n"
    "生成的代码要求：单文件HTML、响应式设计、现代UI风格、内联CSS/JS、语义化标签、可直接在浏览器打开运行。\n"
    "如果用户提出后端/数据库/App/游戏等不属于网页前端的需求，礼貌告知这是纯建站助手，引导用户回到网页制作方向。"
)

SYS_PROMPTS: dict[str, str] = {
    "explain": SYS_BASE + "当前场景：用户想了解技术概念。用通俗易懂的中文解释，多举前端网页相关的实际例子。用纯文字回答，不要主动推销建站服务，除非用户表现出建站意向。",
    "debug": SYS_BASE + "当前场景：用户遇到了前端网页报错或bug。根据错误信息给出根因分析、修复方案和预防建议。如果是前端HTML/CSS/JS代码问题，直接给出修复后的完整代码。",
    "compare": SYS_BASE + "当前场景：用户想对比技术方案。对比用户提到的选项（框架/模板/部署方式等），列出各自优劣和适用场景。用纯文字回答，不要强行推销。",
    "casual": SYS_BASE + "当前场景：用户在打招呼或闲聊。用轻松自然的语气像朋友一样聊天，不要提建站、不要推销、不要说'我可以帮你做网站'。回答了用户的问题就停。如果用户主动问你能做什么，再简单介绍建站能力。",
    "text": "你是翻译专家。把用户提供的文本准确翻译到目标语言，只输出翻译结果。",
}
SYS_DEFAULT = SYS_PROMPTS["explain"]

SKILL_LOG = logging.getLogger("ai_service.explain")


def _sanitize_search(results: list, max_total: int = 1500) -> str:
    """校验并格式化搜索结果: 去HTML标签/去空/截断/限总长。"""
    import re
    lines = []
    total = 0
    for r in results:
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        # 去 HTML 标签
        snippet = re.sub(r"<[^>]+>", "", snippet)
        # 去连续空白
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if not title and not snippet:
            continue
        line = f"- {title}: {snippet}"[:300]
        if total + len(line) > max_total:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return (
        "\n\n【联网搜索结果】\n"
        + "\n".join(lines)
        + "\n请结合以上实时信息回答。如果搜索结果不相关，请用自己的知识回答。"
    )


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
                safe = _sanitize_search(search_res["results"])
                if safe:
                    sys_prompt += safe
                    SKILL_LOG.info("[chat] 搜索增强 trace=%s provider=%s chars=%d", trace_id, search_res.get("provider"), len(safe))
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
