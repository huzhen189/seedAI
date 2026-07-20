"""Build Agent: 完整建站流程(需求分析→文案确认→代码生成)。

Phase 1: 需求分析 — 深度挖掘用户需求, 产出内容文案 + 页面结构方案
Phase 2: 代码生成 — 用户确认"确认开始"后, Planner→Coder→Reviewer→预览

职责: 确保用户确认方案后再动手写代码, 避免生成不符合预期的产物。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Dict, Optional

from ..events import ev
from ..providers import (
    ModelUnavailableError,
    astream_with_fallback,
    get_chat_model,
    resolve_fallback_order,
)
from ..registry import register_skill

AGENT_LOG = logging.getLogger("ai_service.build_agent")

# ---- Phase 1: 需求分析 + 文案生成 ----
SYS_ANALYST = (
    "你叫小胡，是智能建站助手的「需求分析师」。你的职责是：\n"
    "1. 深度理解用户想要的网站类型、定位、目标用户\n"
    "2. 根据用户描述，生成一份**内容文案**（品牌名、slogan、介绍文案）\n"
    "3. 规划**页面结构**（首页有哪些模块、子页面有哪些）\n"
    "4. 列出**功能清单**（联系表单、搜索、轮播图等）\n\n"
    "请只输出一个 JSON 对象:\n"
    '{"brand": {"name":"品牌名","slogan":"一句话口号","intro":"一段品牌介绍(200字内)"},'
    '"pages": [{"title":"首页","sections":[{"name":"hero","content":"..."}]}],'
    '"features": ["功能A","功能B"],"design_style":"简约/科技/复古/活泼/商务","color_hint":"蓝白/暗黑/暖色"}\n\n'
    "如果用户信息不完整（缺少行业/功能需求等），请主动问用户关键问题，用 ask 模式返回:\n"
    '{"ask": "关键问题", "current_plan": {...}}\n\n'
    "用户输入: "
)


# ---- Phase 2: Planner/Coder/Reviewer (复用 generate_site) ----
SYS_PLANNER = (
    "你是技术架构师。根据需求分析师产出的文案方案, 拆解成代码实现规格。\n"
    "请只输出一个 JSON 对象:\n"
    '{"title":"项目标题","goal":"一句话目标","steps":["步骤1","步骤2"],"reasoning":"技术考虑"}\n'
    "用户输入: "
)

SYS_CODER = (
    "你叫小胡，是智能建站助手。用户已经确认了内容方案，现在根据需求规格生成单文件HTML。\n"
    "要求：响应式设计、现代UI风格、内联CSS/JS、可直接部署运行。语义化HTML标签，适当的微交互和过渡动画。"
)


def _chat(model_id: str, system: str, msgs: list) -> str:
    chat = get_chat_model(model_id, streaming=False)
    resp = chat.invoke([{"role": "system", "content": system}, *msgs])
    return (resp.content or "").strip()


def _parse_json(raw: str) -> dict:
    m = __import__("re").search(r"\{.*\}", raw, __import__("re").DOTALL)
    return json.loads(m.group(0)) if m else {}


def _extract_html(text: str) -> str:
    if "```html" in text:
        return text.split("```html", 1)[1].split("```", 1)[0].strip()
    if "```" in text:
        return text.split("```", 1)[1].split("```", 1)[0].strip()
    return text


async def build_agent(
    model_id: str,
    messages: list,
    trace_id: str | None = None,
    is_cancelled=None,
    intent: str | None = None,
    level2: str | None = None,
    industry: str | None = "other",
    checkpoint: dict | None = None,
    resume_mode: str = "resume",
    **kwargs,
) -> AsyncGenerator[Dict, None]:
    """完整建站流程: Phase1 需求分析 → 确认 → Phase2 代码生成。"""

    AGENT_LOG.info("[build] [1/4] 需求分析开始 trace=%s model=%s intent=%s", trace_id, model_id, intent)

    # 取第一条用户消息作为需求输入
    first_user = ""
    for m in messages:
        if m.get("role") == "user":
            first_user = m.get("content", "") or ""
            break

    # ---- Phase 1: 需求分析 + 文案生成 ----
    AGENT_LOG.info("[build] Phase1 需求分析 trace=%s input=%.100s", trace_id, first_user)
    yield ev("node", stage="analyzing", skill="build_agent")
    yield ev("think", stage="analyst", content="正在分析您的需求，生成内容方案…")

    analyst_msgs = [{"role": "user", "content": first_user}]
    raw = _chat(model_id, SYS_ANALYST, analyst_msgs)
    plan = _parse_json(raw)

    AGENT_LOG.info("[build] [2/4] 需求分析完成 plan_keys=%s ask=%s",
                   list(plan.keys()), bool(plan.get("ask")))

    # 如果 AI 需要反问用户
    if plan.get("ask"):
        yield ev("think", stage="analyst", content=plan["ask"])
        yield ev("paused", stage="await_info", question=plan["ask"], plan=plan.get("current_plan", {}))
        return

    # 展示方案
    brand = plan.get("brand", {})
    pages = plan.get("pages", [])
    features = plan.get("features", [])

    summary_parts = []
    if brand.get("name"):
        summary_parts.append(f"**品牌**: {brand['name']} — {brand.get('slogan', '')}")
    if brand.get("intro"):
        summary_parts.append(f"**介绍**: {brand['intro'][:200]}")
    if pages:
        page_names = " → ".join(p.get("title", "?") for p in pages[:5])
        summary_parts.append(f"**页面**: {page_names}")
    if features:
        summary_parts.append(f"**功能**: {'、'.join(features[:8])}")
    if plan.get("design_style"):
        summary_parts.append(f"**风格**: {plan['design_style']} (配色: {plan.get('color_hint', '默认')})")

    yield ev("think", stage="analyst", content="\n\n".join(summary_parts))
    yield ev(
        "plan",
        title=brand.get("name", "建站方案"),
        goal=brand.get("intro", "")[:100],
        steps=[f"页面: {', '.join(p.get('title','') for p in pages)}", f"功能: {', '.join(features)}"],
    )

    # 等用户确认
    AGENT_LOG.info("[build] [3/4] 方案已输出, 等待用户确认 trace=%s title=%s pages=%d features=%d",
                   trace_id, brand.get("name","?"), len(pages), len(features))
    yield ev(
        "paused",
        stage="await_confirm",
        plan_title=brand.get("name", "建站方案"),
        plan_goal=brand.get("intro", "")[:100],
        plan_steps=plan.get("pages", []),
    )
    return


async def build_agent_coder(
    model_id: str,
    messages: list,
    trace_id: str | None = None,
    is_cancelled=None,
    intent: str | None = None,
    level2: str | None = None,
    industry: str | None = "other",
    checkpoint: dict | None = None,
    resume_mode: str = "resume",
    **kwargs,
) -> AsyncGenerator[Dict, None]:
    """Phase 2: 用户确认后执行代码生成(Planner→Coder→Reviewer→Preview)。"""

    AGENT_LOG.info("[build] [4/4] 代码生成开始 trace=%s model=%s", trace_id, model_id)
    yield ev("node", stage="building", skill="build_agent")

    # Planner
    first_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            first_user = m.get("content", "") or ""
            break
    if not first_user:
        for m in messages:
            if m.get("role") == "user":
                first_user = m.get("content", "") or ""
                break

    yield ev("node", stage="enter_planner")
    yield ev("think", stage="planner", content="规划技术方案…")
    plan_msgs = [{"role": "user", "content": first_user}]
    spec = _chat(model_id, SYS_PLANNER, plan_msgs)
    plan = _parse_json(spec)

    AGENT_LOG.info("[build] [4a] Planner 完成 trace=%s title=%s steps=%d 进入代码生成",
                   trace_id, plan.get("title"), len(plan.get("steps", [])))
    if plan.get("reasoning"):
        yield ev("think", stage="planner", content=plan["reasoning"])

    # Coder
    coder_prompt = SYS_CODER
    AGENT_LOG.info("[build] [4b] 代码生成中 trace=%s 开始调用 LLM", trace_id)
    yield ev("node", stage="enter_coder")
    yield ev("think", stage="coder", content="生成代码中…")
    coder_msgs = [
        {"role": "user", "content": f"需求规格:\n{json.dumps(plan, ensure_ascii=False)}"},
        *messages,
    ]
    html_parts = []
    async for chunk, _ in astream_with_fallback(model_id, coder_msgs, system=coder_prompt):
        text = getattr(chunk, "content", chunk)
        if text:
            html_parts.append(text)
            yield ev("token", data=text)
    html = _extract_html("".join(html_parts))

    AGENT_LOG.info("[build] [4c] 代码生成完成 trace=%s html_len=%d tokens≈%d",
                   trace_id, len(html), len(html_parts))
    yield ev("node", stage="previewing")
    url = _deliver(html, trace_id or "site")
    AGENT_LOG.info("[build] [4d] 预览投递 trace=%s cos_url=%s", trace_id, url or "(无)")
    yield ev("node", stage="preview", url=url, fallback="srcdoc" if not url else None)
    yield ev("node", stage="done")


def _deliver(html: str, trace_id: str) -> Optional[str]:
    try:
        from ..tools.cos_upload import cos_upload
        art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
        site_dir = art_dir / "anon" / (trace_id or "site")
        site_dir.mkdir(parents=True, exist_ok=True)
        idx = site_dir / "index.html"
        idx.write_text(html, encoding="utf-8")
        cos_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/anon/{trace_id or 'site'}/index.html"
        res = cos_upload(str(idx), cos_key)
        if res.get("ok"):
            return res.get("url")
    except Exception:
        pass
    return None


register_skill(
    name="build_agent",
    intent_tags=["建站", "生成", "网站", "网页", "页面"],
    handler=build_agent,
    is_graph=False,
    description="智能建站助手: 需求分析→文案确认→代码生成",
)

register_skill(
    name="build_agent_coder",
    intent_tags=[],
    handler=build_agent_coder,
    is_graph=False,
    description="建站代码生成阶段(供内部恢复用)",
)
