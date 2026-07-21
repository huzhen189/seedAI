"""上下文模块: 历史关联检测 + 意图修正。

三层兜底: 1.WebLLM前端hint 2.Chroma向量检索 3.零依赖关键词
输出 ContextResult {has_context, hint, correction, source}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("ai_service.intent.context")


@dataclass
class ContextResult:
    has_context: bool = False
    hint: str = ""
    correction: dict | None = None  # {level1, level2, reason} 可选的意图修正
    source: str = "none"            # "webllm"|"chroma"|"fallback"|"none"


def run_context(messages: list[dict], conversation_id: int | None = None,
                frontend_hint: str = "") -> ContextResult:
    """上下文检测入口。"""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last:
        logger.info("[上下文] 输入为空,跳过")
        return ContextResult()

    # 1. 前端 WebLLM hint
    if frontend_hint:
        logger.info("[上下文] 来源=前端WebLLM | 内容=%.60s", frontend_hint)
        return ContextResult(has_context=True, hint=frontend_hint, source="webllm")

    # 2. Chroma 向量检索
    if conversation_id:
        try:
            from ..knowledge.chroma import find_relevant_messages
            logger.info("[上下文] 尝试Chroma向量检索 conv=%s", conversation_id)
            relevant_ids = find_relevant_messages(last, conversation_id)
            if relevant_ids:
                relevant = [m for m in messages if m.get("_msg_id") in relevant_ids]
                ctx_text = " ".join(m.get("content", "")[:200] for m in relevant[-6:])
                hint = _summarize_context(ctx_text)
                if hint:
                    logger.info("[上下文] 来源=Chroma向量 | 相关消息=%d | 摘要=%.60s",
                               len(relevant_ids), hint)
                    correction = _infer_correction(hint)
                    return ContextResult(has_context=True, hint=hint, correction=correction, source="chroma")
            else:
                logger.info("[上下文] Chroma未找到相关消息 conv=%s", conversation_id)
        except Exception as e:
            logger.debug("[上下文] Chroma检索异常: %s", e)

    # 3. 零依赖兜底
    logger.info("[上下文] 使用零依赖兜底(关键词匹配)")
    for m in reversed(messages):
        if m.get("role") == "assistant":
            hint = _summarize_context(m.get("content", ""))
            if hint:
                logger.info("[上下文] 来源=关键词兜底 | 摘要=%.60s", hint)
                correction = _infer_correction(hint)
                return ContextResult(has_context=True, hint=hint, correction=correction, source="fallback")
            break
    logger.info("[上下文] 所有来源均未检测到上下文")
    return ContextResult()


def _summarize_context(text: str) -> str:
    """从 assistant 回复提取简短主题摘要(关键词匹配, 不调LLM)。"""
    t = text[:500].lower()
    mapping = [
        ("天气", "天气"), ("温度", "天气"), ("下雨", "天气"), ("湿度", "天气"),
        ("网站", "网站制作"), ("网页", "网页制作"), ("html", "网页制作"),
        ("编程", "编程"), ("代码", "代码"), ("翻译", "翻译"),
        ("教程", "教程"), ("文档", "文档"), ("游戏", "游戏开发"),
        ("商城", "电商"), ("商品", "电商"), ("订单", "电商"),
        ("个人站", "个人网站"), ("博客", "博客"), ("简历", "简历"),
        ("模板", "模板"), ("前端", "前端开发"),
        ("颜色", "设计"), ("配色", "设计"), ("字体", "设计"),
        ("布局", "页面布局"), ("导航", "导航设计"),
        ("餐厅", "餐饮建站"), ("美食", "餐饮建站"), ("外卖", "餐饮建站"),
        ("酒店", "旅游建站"), ("景点", "旅游建站"), ("攻略", "旅游建站"),
        ("课程", "教育建站"), ("学生", "教育建站"),
        ("诊所", "医疗建站"), ("预约", "医疗建站"),
        ("需求", "需求分析"), ("方案", "需求分析"),
        ("修复", "代码修复"), ("报错", "代码修复"), ("bug", "代码修复"),
    ]
    for kw, topic in mapping:
        if kw in t:
            return topic
    return ""


def _infer_correction(hint: str) -> dict | None:
    """从上下文摘要推测意图修正。"""
    if not hint:
        return None
    correction_map = {
        "网站制作": {"level1": "build", "level2": "site", "reason": "上条在讨论建站"},
        "网页制作": {"level1": "build", "level2": "page", "reason": "上条在讨论网页"},
        "前端开发": {"level1": "build", "level2": "site", "reason": "上条在讨论前端"},
        "电商": {"level1": "build", "level2": "site", "reason": "上条在讨论电商"},
        "餐饮建站": {"level1": "build", "level2": "site", "reason": "上条在讨论餐饮"},
        "需求分析": {"level1": "build", "level2": "requirement", "reason": "上条在讨论需求"},
        "代码修复": {"level1": "code", "level2": "fix", "reason": "上条在讨论修复"},
    }
    for topic, correction in correction_map.items():
        if topic in hint:
            return correction
    return None
