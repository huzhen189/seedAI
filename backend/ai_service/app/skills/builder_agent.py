"""Builder Agent: 基于需求文档生成/修改单文件HTML。

前置条件: projects.requirement_doc 已存在(包含品牌/页面/功能/风格)。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Dict, Optional

from ..events import ev
from ..providers import astream_with_fallback, get_chat_model
from ..registry import register_skill

AGENT_LOG = logging.getLogger("ai_service.builder")

SYS_PLANNER = (
    "你叫小胡，是建站师。根据需求文档规划技术方案，只输出JSON。\n"
    '{"pages":["首页","列表","详情"],"layout":"header-hero-features-footer",'
    '"tech":"HTML+CSS+JS","notes":"技术要点"}\n'
    "用户需求: "
)

SYS_CODER = (
    "你叫小胡，是智能建站助手。根据需求文档生成单文件HTML。\n"
    "要求: 响应式、现代设计、内联CSS/JS、完整可运行、语义化标签。\n"
    "输出: 只输出HTML代码，用 ```html 包裹。"
)


def _extract_html(text: str) -> str:
    if "```html" in text:
        return text.split("```html", 1)[1].split("```", 1)[0].strip()
    if "```" in text:
        return text.split("```", 1)[1].split("```", 1)[0].strip()
    return text


@register_skill(
    name="builder_agent",
    intent_tags=["建站", "生成", "网站", "网页", "页面", "做", "开发"],
    handler=builder_agent_handler,
    is_graph=False,
    description="代码生成: 基于需求文档生成/修改HTML",
)
async def builder_agent_handler(
    model_id: str, messages: list, trace_id: str | None = None,
    is_cancelled=None, requirement_doc: dict | None = None, **kwargs,
) -> AsyncGenerator[Dict, None]:
    AGENT_LOG.info("[builder] 开始 trace=%s has_req=%s msgs=%d",
                   trace_id, bool(requirement_doc), len(messages))

    yield ev("node", stage="enter_planner", agent_id="builder_agent")
    yield ev("think", stage="planner", content="规划技术方案…", agent_id="builder_agent")

    # Planner
    plan_prompt = json.dumps(requirement_doc, ensure_ascii=False) if requirement_doc else ""
    user_input = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_input = m.get("content", "") or ""
            break

    chat = get_chat_model(model_id, streaming=False)
    plan_resp = chat.invoke([{"role": "system", "content": SYS_PLANNER},
                              {"role": "user", "content": f"{plan_prompt}\n\n用户消息: {user_input}"}])
    plan_raw = (plan_resp.content or "").strip()
    m = __import__("re").search(r"\{[\s\S]*\}", plan_raw)
    plan = json.loads(m.group(0)) if m else {}
    AGENT_LOG.info("[builder] Planner完成 pages=%s", plan.get("pages"))

    if plan.get("notes"):
        yield ev("think", stage="planner", content=plan["notes"], agent_id="builder_agent")

    yield ev("node", stage="enter_coder", agent_id="builder_agent")
    yield ev("think", stage="coder", content="生成代码中…", agent_id="builder_agent")

    # Coder
    coder_msgs = [
        {"role": "user", "content": f"需求文档:\n{plan_prompt}\n\n技术方案:\n{json.dumps(plan, ensure_ascii=False)}\n\n指令: {user_input}"},
    ]
    html_parts = []
    async for chunk, _ in astream_with_fallback(model_id, coder_msgs, system=SYS_CODER):
        text = getattr(chunk, "content", chunk)
        if text:
            html_parts.append(text)
            yield ev("token", data=text, agent_id="builder_agent")
    html = _extract_html("".join(html_parts))
    AGENT_LOG.info("[builder] 代码生成完成 html_len=%d", len(html))

    # Preview
    yield ev("node", stage="previewing", agent_id="builder_agent")
    try:
        from ..tools.cos_upload import cos_upload
        art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
        site_dir = art_dir / "sites" / (trace_id or "site")
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "index.html").write_text(html, encoding="utf-8")
        res = cos_upload(str(site_dir / "index.html"), f"previews/{trace_id or 'site'}/index.html")
        url = res.get("url") if res.get("ok") else None
    except Exception:
        url = None
    AGENT_LOG.info("[builder] 预览 url=%s", url)
    yield ev("node", stage="preview", url=url, fallback="srcdoc" if not url else None, agent_id="builder_agent")
    yield ev("node", stage="done", agent_id="builder_agent")
