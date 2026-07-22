"""Builder Agent: 基于需求文档生成/修改单文件HTML。

前置条件: projects.requirement_doc 已存在(包含品牌/页面/功能/风格)。
"""

from __future__ import annotations

import json
import logging
import os
import time
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



async def builder_agent_handler(
    model_id: str, messages: list, trace_id: str | None = None,
    is_cancelled=None, requirement_doc: dict | None = None, **kwargs,
) -> AsyncGenerator[Dict, None]:
    req_pages = len(requirement_doc.get("pages", [])) if requirement_doc else 0
    req_features = len(requirement_doc.get("features", [])) if requirement_doc else 0
    AGENT_LOG.info("[builder] [1/5] 开始建站 trace=%s model=%s 需求文档:%s 页面数=%d 功能数=%d msgs=%d",
                   trace_id, model_id, "有" if requirement_doc else "无", req_pages, req_features, len(messages))

    yield ev("node", stage="enter_planner", agent_id="builder_agent")
    yield ev("think", stage="planner", content="规划技术方案…", agent_id="builder_agent")

    # ── [2/5] Planner ──
    AGENT_LOG.info("[builder] [2/5] 规划技术方案 调LLM...")
    plan_prompt = json.dumps(requirement_doc, ensure_ascii=False) if requirement_doc else ""
    user_input = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_input = m.get("content", "") or ""
            break
    t0 = time.time()
    chat = get_chat_model(model_id, streaming=False)
    plan_resp = chat.invoke([{"role": "system", "content": SYS_PLANNER},
                              {"role": "user", "content": f"{plan_prompt}\n\n用户消息: {user_input}"}])
    plan_raw = (plan_resp.content or "").strip()
    m = __import__("re").search(r"\{[\s\S]*\}", plan_raw)
    plan = json.loads(m.group(0)) if m else {}
    AGENT_LOG.info("[builder] [2/5] Planner完成 耗时=%.0fms 页面数=%s 输出长度=%d",
                   (time.time() - t0) * 1000, plan.get("pages"), len(plan_raw))

    if plan.get("notes"):
        yield ev("think", stage="planner", content=plan["notes"], agent_id="builder_agent")

    # ── [3/5] Coder ──
    AGENT_LOG.info("[builder] [3/5] 生成代码 model=%s 使用流式调用...")
    yield ev("node", stage="enter_coder", agent_id="builder_agent")
    yield ev("think", stage="coder", content="生成代码中…", agent_id="builder_agent")

    coder_msgs = [
        {"role": "user", "content": f"需求文档:\n{plan_prompt}\n\n技术方案:\n{json.dumps(plan, ensure_ascii=False)}\n\n指令: {user_input}"},
    ]
    html_parts = []
    t0 = time.time()
    async for chunk, _ in astream_with_fallback(model_id, coder_msgs, system=SYS_CODER):
        text = getattr(chunk, "content", chunk)
        if text:
            html_parts.append(text)
            yield ev("token", data=text, agent_id="builder_agent")
    html = _extract_html("".join(html_parts))
    AGENT_LOG.info("[builder] [3/5] 代码生成完成 耗时=%.0fms HTML大小=%d字节 token数≈%d",
                   (time.time() - t0) * 1000, len(html), len(html_parts))

    # ── [4/5] Preview + COS ──
    AGENT_LOG.info("[builder] [4/5] 预览发布 开始...")
    yield ev("node", stage="previewing", agent_id="builder_agent")
    url = None
    try:
        from ..tools.cos_upload import cos_upload
        AGENT_LOG.info("[builder] [4/5] COS上传 trace=%s 文件大小=%d", trace_id, len(html))
        art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
        site_dir = art_dir / "sites" / (trace_id or "site")
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "index.html").write_text(html, encoding="utf-8")
        cos_key = f"previews/{trace_id or 'site'}/index.html"
        t0 = time.time()
        res = cos_upload(str(site_dir / "index.html"), cos_key)
        if res.get("ok"):
            url = res.get("url")
            AGENT_LOG.info("[builder] [4/5] COS上传成功 耗时=%.0fms url=%s", (time.time() - t0) * 1000, url)
        else:
            AGENT_LOG.warning("[builder] [4/5] COS上传失败 res=%s", res)
    except Exception as e:
        AGENT_LOG.warning("[builder] [4/5] COS上传异常: %s", e)
    AGENT_LOG.info("[builder] [4/5] 预览完成 url=%s fallback=srcdoc", url or "(无)")

    # ── [5/5] Done ──
    yield ev("preview", url=url, fallback="srcdoc" if not url else None, agent_id="builder_agent")
    yield ev("node", stage="done", agent_id="builder_agent")
    AGENT_LOG.info("[builder] [5/5] 建站完成 trace=%s HTML=%d字节 有预览:%s", trace_id, len(html), bool(url))


register_skill(name="builder_agent", display_name="建站小胡", avatar="⚡", role="代码生成",
    intent_tags=["建站", "生成", "网站", "网页", "页面", "做", "开发"],
    handler=builder_agent_handler,
    is_graph=False,
    description="代码生成: 基于需求文档生成/修改HTML",
)
