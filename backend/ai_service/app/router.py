"""Router:两级意图分类 + Skill 分发(§5.2)。

- detect_intent: 先走 LLM 分类器, 返回 {level1, level2, confidence, industry, label}
- 二维映射 (level1,level2) → skill
- unsupported 意图不调 skill, 直接返回 unsupported 事件
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from .intent_classifier import classify
from .registry import SkillRegistry


logger = logging.getLogger("ai_service.router")

# (level1, level2) → skill_name 二维映射
INTENT_SKILL_MAP: dict[tuple[str, str], str] = {
    ("learn", "explain"): "explain",
    ("learn", "debug"): "explain",
    ("learn", "compare"): "explain",
    ("learn", "casual"): "explain",
    ("code", "snippet"): "write_code",
    ("code", "component"): "write_code",
    ("code", "fix"): "write_code",
    ("code", "refactor"): "write_code",
    ("build", "page"): "generate_site",
    ("build", "site"): "generate_site",
    ("build", "modify"): "generate_site",
    ("build", "game"): "generate_site",
    ("doc", "readme"): "generate_doc",
    ("doc", "tutorial"): "generate_doc",
    ("doc", "plan"): "generate_doc",
    ("translate", "text"): "explain",
    ("translate", "code_lang"): "write_code",
}

# 两级标签(前端展示)
LEVEL1_LABELS = {
    "learn": "学习理解",
    "code": "编码实战",
    "build": "建站生成",
    "doc": "文档方案",
    "translate": "翻译转换",
    "unsupported": "不支持",
}

LEVEL2_LABELS: dict[str, str] = {
    "explain": "概念解释",
    "debug": "排查报错",
    "compare": "技术对比",
    "casual": "日常闲聊",
    "snippet": "函数片段",
    "component": "UI组件",
    "fix": "修复Bug",
    "refactor": "重构优化",
    "page": "单页/落地页",
    "site": "完整网站",
    "modify": "修改已有",
    "game": "互动游戏",
    "readme": "README",
    "tutorial": "教程指南",
    "plan": "方案设计",
    "text": "文本翻译",
    "code_lang": "代码翻译",
}


def detect_intent(messages: list[dict], model_id: str = "hy3") -> dict:
    """返回 {level1, level2, confidence, industry, label}。"""
    t0 = time.time()
    result = classify(messages, model_id)
    l1 = result["level1"]
    l2 = result["level2"]
    elapsed = time.time() - t0
    label1 = LEVEL1_LABELS.get(l1, l1)
    label2 = LEVEL2_LABELS.get(l2, l2)
    logger.info(
        "[意图] 识别: %s → %s | 置信度 %s | 行业 %s | 耗时 %.1fs",
        label1, label2,
        result.get("confidence", "?"),
        result.get("industry", "none"),
        elapsed,
    )
    result["label"] = f"{label1} · {label2}"
    result["level1_label"] = label1
    result["level2_label"] = label2
    result["industry"] = result.get("industry", "other") or "other"
    return result


def skill_for(level1: str, level2: str) -> str | None:
    return INTENT_SKILL_MAP.get((level1, level2))


async def dispatch(
    skill_name: str, model_id: str, messages: list[dict], **kwargs
) -> AsyncGenerator[Any, None]:
    t0 = time.time()
    entry = SkillRegistry.get(skill_name)
    if entry is None:
        yield {"event": "error", "data": {"message": f"Skill '{skill_name}' 不存在"}}
        return

    logger.info("[路由] 分发 -> %s | model=%s", skill_name, model_id)
    handler = entry.handler
    if entry.is_graph or inspect.isasyncgenfunction(handler):
        async for chunk in handler(model_id=model_id, messages=messages, **kwargs):
            yield chunk
    else:
        result = await handler(model_id=model_id, messages=messages, **kwargs)
        yield result

    elapsed = time.time() - t0
    logger.info("[完成] skill=%s 总耗时 %.1fs", skill_name, elapsed)
