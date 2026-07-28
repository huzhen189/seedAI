"""Skill: generate_doc(生成文档/Markdown · 流式 SSE 输出 · §5.2)。"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import AsyncGenerator
from typing import Any, Dict, Optional

from ..events import ev
from ..providers import (
    ModelUnavailableError,
    astream_with_fallback,
    get_chat_model,
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


def _upload_doc_to_cos(
    full_md: str, doc_name: str, *, trace_id: Optional[str],
    user_id=None, project_id=None, version=None,
) -> str:
    """把 Markdown 落临时文件并上传 COS, 返回预览直链; 失败返回 '' (优雅降级, 不阻断主流程)。

    与站点产物一致的版本化 key: previews/{user_id}/{project_id}/v{version}/{doc_name}。
    """
    try:
        from ..tools.cos_upload import cos_upload

        ver_seg = f"v{version}" if version else (trace_id or "doc")
        uid = user_id if user_id is not None else "anon"
        pid = project_id if project_id is not None else "anon"
        base_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/{uid}/{pid}/{ver_seg}"
        cos_key = f"{base_key}/{doc_name}"  # 中文名直接进 key(UTF-8 合法), 前端预览/下载时会编码
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tf:
            tf.write(full_md)
            tmp_path = tf.name
        try:
            res = cos_upload(tmp_path, cos_key)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        if res.get("ok") and res.get("url"):
            return res["url"]
        GEN_LOG.warning("[doc] COS 投递未返回 URL trace=%s name=%s cos_key=%s res=%s",
                        trace_id, doc_name, cos_key, res)
    except Exception as e:
        GEN_LOG.warning("[doc] COS 上传异常 trace=%s name=%s: %s", trace_id, doc_name, e)
    return ""


async def _cancelled_now(fn) -> bool:
    import inspect
    if not fn:
        return False
    res = fn()
    if inspect.isawaitable(res):
        return bool(await res)
    return bool(res)


async def _gen_outline(model_id: str, messages: list) -> str:
    """非流式生成文档大纲(章节标题树), 供轨迹 '规划结构' 步骤实时展示。

    失败返回 '' (优雅降级, 不阻断主流程): 无 key / 模型不可用 / 异常都只告警跳过。
    """
    try:
        sys_outline = (
            "你是一名技术文档架构师。仅根据用户需求输出文档的章节大纲, "
            "使用纯 Markdown 标题(最多三级, 不超过 12 行), 不要正文、不要解释、不要代码块。"
            "直接以 # 主标题开头, 例如:\n"
            "# 文档主标题\n## 1. 背景与目标\n## 2. 方案概述\n### 2.1 技术选型\n"
        )
        for mid in resolve_fallback_order(model_id):
            try:
                chat = get_chat_model(mid, streaming=False)
                resp = await chat.ainvoke(
                    [{"role": "system", "content": sys_outline}] + messages
                )
                text = getattr(resp, "content", None) or ""
                text = text.strip()
                if text:
                    GEN_LOG.info("[doc] 大纲生成成功 model=%s lines=%s", mid, text.count("\n") + 1)
                    return text
            except Exception as e:
                GEN_LOG.warning("[doc] 大纲生成失败 model=%s: %s", mid, e)
        return ""
    except Exception as e:
        GEN_LOG.warning("[doc] 大纲生成异常(跳过): %s", e)
        return ""


async def _polish_doc(model_id: str, outline: str, body: str) -> str:
    '''把大纲拼进正文首部, 让模型对「大纲 + 正文」整篇略作润色。

    润色仅统一格式/标题层级、优化衔接过渡、修正笔误, 保持不变结构与信息。
    失败/异常优雅降级: 直接把大纲拼到首部返回(用 H2 区块, 不抢占正文 H1), 不阻断主流程。
    '''
    head = '## 文档大纲\n\n' + outline.strip() + '\n\n---\n\n' if outline else ''
    combined = head + body
    try:
        sys_polish = (
            '你是技术文档润色助手。下面是一份文档：开头是 ## 文档大纲 区块，其后为完整正文。'
            '请对整体略作润色——保持原有章节结构与全部信息不变，仅统一 Markdown 格式与标题层级、'
            '优化章节衔接过渡、修正明显笔误与标点；不要增删章节、不要改写实质内容。'
            '必须原样保留开头的 ## 文档大纲 区块（不得删除或移动），它作为文档目录需始终位于文首。'
            '仅输出润色后的完整 Markdown，不要任何前言或解释。'
        )
        for mid in resolve_fallback_order(model_id):
            try:
                chat = get_chat_model(mid, streaming=False)
                resp = await chat.ainvoke([
                    {'role': 'system', 'content': sys_polish},
                    {'role': 'user', 'content': combined},
                ])
                text = getattr(resp, 'content', None) or ''
                text = text.strip()
                if text:
                    if outline and '文档大纲' not in text:
                        GEN_LOG.warning('[doc] 润色输出丢失大纲区块, 强制拼回首部 model=%s', mid)
                        return head + text
                    GEN_LOG.info('[doc] 润色成功 model=%s chars=%s', mid, len(text))
                    return text
            except Exception as e:
                GEN_LOG.warning('[doc] 润色失败 model=%s: %s', mid, e)
        return combined
    except Exception as e:
        GEN_LOG.warning('[doc] 润色异常(降级为直接拼接): %s', e)
        return combined


async def generate_doc_skill(
    model_id: str,
    messages: list,
    trace_id: Optional[str] = None,
    is_cancelled=None,
    **kwargs,
) -> AsyncGenerator[Dict[str, Any], None]:
    """流式生成文档, 支持取消 + 模型回退。"""
    GEN_LOG.info("[doc] 开始 trace=%s model=%s", trace_id, model_id)
    # 分层轨迹: 规划结构 → 撰写正文 → 校对格式(由各阶段节点驱动, 给前端 trail 更细的层次感)
    yield ev("node", stage="doc_plan")
    if await _cancelled_now(is_cancelled):
        yield ev("aborted")
        return
    # 规划结构阶段: 非流式先生成大纲, 作为轨迹 think 实时展示(撰写正文时再逐节展开)
    # 失败优雅降级: 仅跳过大纲, 直接进入正文撰写, 不影响主流程。
    outline = await _gen_outline(model_id, messages)
    if outline:
        yield ev("think", stage="doc_plan", content=outline)
    parts: list[str] = []
    _emit_write = False
    # 主生成: 若有大纲则作为参考骨架, 让正文章节结构与之对齐
    sys_doc = SYS_DOC
    if outline:
        sys_doc = SYS_DOC + "\n\n参考大纲(请严格遵循其章节结构逐节展开, 不要偏离主题):\n" + outline
    try:
        async for chunk, _ in astream_with_fallback(
            model_id, messages, system=sys_doc
        ):
            if await _cancelled_now(is_cancelled):
                yield ev("aborted")
                return
            text = getattr(chunk, "content", chunk)
            if text:
                if not _emit_write:
                    yield ev("node", stage="doc_write")
                    _emit_write = True
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

    raw_md = ''.join(parts)
    GEN_LOG.info('[doc] 正文完成 trace=%s chars=%s', trace_id, len(raw_md))
    # 产物名基于正文本体(raw_md)推导, 避免被拼入首部的大纲(用 H2, 不抢正文 H1)影响命名
    if raw_md.strip():
        doc_name = _derive_doc_name(raw_md, messages) + '.md'
        # 校对格式 + 润色阶段: 把大纲拼进正文首部, 让模型对整篇「大纲+正文」略作润色
        yield ev('node', stage='doc_proofread')
        if outline:
            yield ev(
                'think', stage='doc_proofread',
                content='正在对「大纲 + 正文」做整体润色：统一 Markdown 格式、优化章节衔接、修正笔误。',
            )
        full_md = await _polish_doc(model_id, outline, raw_md)
        GEN_LOG.info('[doc] 润色后 trace=%s chars=%s', trace_id, len(full_md))
        # Fix B (#482): 把完整 Markdown 作为产物文件下发, 供 proxy 落库为 artifact(右侧面板预览/下载)
        # md 也上传 COS(与站点产物一致: 版本化直链, 右侧可下载)
        cos_url = _upload_doc_to_cos(
            full_md, doc_name, trace_id=trace_id,
            user_id=kwargs.get('user_id'),
            project_id=kwargs.get('project_id'),
            version=kwargs.get('version'),
        )
        doc_data: Dict[str, Any] = {
            'name': doc_name,
            'content': full_md,
            'size': len(full_md.encode('utf-8')),
        }
        if cos_url:
            doc_data['url'] = cos_url
        GEN_LOG.info('[doc] 产物名 trace=%s name=%s cos=%s', trace_id, doc_name, bool(cos_url))
        yield ev('node', stage='doc_file', data=doc_data)
    yield ev('node', stage='done')


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
