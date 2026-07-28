"""Skill: generate_doc(生成文档/Markdown · 流式 SSE 输出 · §5.2)。"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from typing import Any, Dict, Optional

from ..events import ev
from ..providers import (
    ModelUnavailableError,
    astream_with_fallback,
    resolve_fallback_order,
)
from ..registry import register_skill
logger = logging.getLogger("ai_service.skills.agent_doc")


SYS_DOC = (
    "你是一名技术文档工程师。根据用户需求产出清晰、结构化的文档, 使用 Markdown 格式。"
    "如果用户没有指定输出格式, 默认输出 .md。\n\n"
    "⚠️ 平台约束（重要）：本平台是一个「仅生成纯静态前端网页」的 AI 助手, "
    "所有产出都是纯前端技术(HTML + CSS + JavaScript), 不支持、也不允许接入任何后端服务、数据库、"
    "服务端运行时或第三方服务端 API。\n"
    "• 仅涉及纯前端技术：HTML、CSS、JavaScript（含内联或独立的 .css/.js 文件）\n"
    "• 严禁在文档中推荐或要求用户使用以下技术来实现平台功能："
    "Next.js / Nuxt / SSR / Node.js 服务端 / PHP / Python(Django·Flask 等)后端 / Java 后端 / Go 后端 / "
    "数据库 / 用户登录注册后端 / 支付后端 / 服务端 API 代理\n"
    "• 若文档涉及「网站 / 页面 / 功能」的实现方案, 必须明确其为纯静态前端实现; "
    "若用户需求确实包含后端能力(如数据存储、登录), 请在文档中友好提示："
    "「该功能需后端支持, 本平台当前仅提供静态前端方案 / 演示界面」, 不要给出后端框架的具体搭建步骤。\n"
    "请始终以「纯静态前端」为前提撰写技术文档。"
)

GEN_LOG = logging.getLogger("ai_service.generate")


def _sanitize_filename(name: str, max_len: int = 40) -> str:
    """把任意标题清洗成安全的文件名(保留中文), 失败回退 '开发文档'。"""
    if not name:
        return "开发文档"
    # 去掉 markdown 标题符号
    name = re.sub(r"^#+\s*", "", name.strip(), flags=re.MULTILINE)
    # 去掉跨平台非法字符(Windows/Linux/macOS 通用)
    name = re.sub(r'[\\/:*?"<>|\r\n\t\x00-\x1f]', "", name)
    name = name.strip().strip(".").strip()
    name = re.sub(r"\s+", " ", name)  # 折叠多余空白
    return name[:max_len] if name else "开发文档"


def _derive_doc_name(full_md: str, messages: list) -> str:
    """从生成文档的首个 H1 或用户请求推导产物文件名(不含扩展名)。

    优先级: 生成文档的第一个有意义 H1 > 最近一条用户消息首行 > '开发文档'。
    """
    # 太泛的标题词直接跳过, 退回用用户请求命名, 更有辨识度
    _GENERIC = {
        "文档", "说明", "笔记", "文档说明",
        "开发文档", "开发说明", "需求文档", "设计文档", "技术文档", "项目文档", "开发文档说明",
        "readme", "doc", "docs",
    }
    # 1) 优先用生成文档的第一个 H1
    for line in full_md.splitlines():
        s = line.strip()
        if s.startswith("# ") and len(s) > 2:
            cand = _sanitize_filename(s[2:])
            if cand and len(cand) >= 2 and cand not in _GENERIC:
                return cand
    # 2) 退而求其次: 最近一条用户消息首行
    user_text = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                user_text = c
            elif isinstance(c, list):  # 多模态 content 数组
                user_text = " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict)
                )
            if user_text.strip():
                break
    if user_text.strip():
        cand = _sanitize_filename(user_text.strip().split("\n")[0])
        if cand and len(cand) >= 2:
            return cand
    return "开发文档"


async def _cancelled_now(fn) -> bool:
    import inspect
    if not fn:
        return False
    res = fn()
    if inspect.isawaitable(res):
        return bool(await res)
    return bool(res)


async def generate_doc_skill(
    model_id: str,
    messages: list,
    trace_id: Optional[str] = None,
    is_cancelled=None,
    **kwargs,
) -> AsyncGenerator[Dict[str, Any], None]:
    """流式生成文档, 支持取消 + 模型回退。"""
    GEN_LOG.info("[doc] 开始 trace=%s model=%s", trace_id, model_id)
    yield ev("node", stage="writing")
    parts: list[str] = []
    try:
        async for chunk, _ in astream_with_fallback(
            model_id, messages, system=SYS_DOC
        ):
            if await _cancelled_now(is_cancelled):
                yield ev("aborted")
                return
            text = getattr(chunk, "content", chunk)
            if text:
                parts.append(text)
                yield ev("token", data=text)
    except ModelUnavailableError as e:
        GEN_LOG.warning("[doc] 模型不可用 trace=%s: %s", trace_id, e)
        yield ev(
            "retry",
            failed=e.failed,
            suggested=e.suggested,
            message=str(e),
        )
        yield ev("aborted")
        return

    full_md = "".join(parts)
    GEN_LOG.info("[doc] 完成 trace=%s chars=%s", trace_id, len(full_md))
    # Fix B (#482): 把完整 Markdown 作为产物文件下发, 供 proxy 落库为 artifact(右侧面板预览/下载)
    # 产物名按对话主题动态命名(首个 H1 / 用户请求), 而非固定 "开发文档.md"
    if full_md.strip():
        doc_name = _derive_doc_name(full_md, messages) + ".md"
        GEN_LOG.info("[doc] 产物名 trace=%s name=%s", trace_id, doc_name)
        yield ev("node", stage="doc_file", data={"name": doc_name, "content": full_md})
    yield ev("node", stage="done")


register_skill(
    name="agent_doc",
    intent_tags=["文档", "doc", "说明", "教程", "readme", "wiki", "方案", "计划"],
    handler=generate_doc_skill,
    is_graph=True,
    display_name="文档生成",
    avatar="📝",
    role="文档工程师",
    description="生成文档/说明(Markdown, 流式输出)",
)
