"""Router:意图管道v2 + Skill 分发。

- detect_intent_v2: 语义异步发射 + 4 同步模块重叠执行 → PipelineResult → 兼容旧 dict
- skill_for: (level1, level2) → skill_name 查表
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from ..intent.pipeline import classify_v2
from ..intent.tools import INTENT_SKILL_MAP
from ..registry import SkillRegistry


logger = logging.getLogger("ai_service.router")

# 两级标签(前端展示)
LEVEL1_LABELS = {
    "learn": "学习理解", "code": "编码实战", "build": "建站生成",
    "doc": "文档方案", "translate": "翻译转换", "unsupported": "不支持",
}

LEVEL2_LABELS: dict[str, str] = {
    "explain": "概念解释", "debug": "排查报错", "compare": "技术对比",
    "casual": "需求沟通", "snippet": "函数片段", "component": "UI组件",
    "fix": "修复Bug", "refactor": "重构优化", "page": "单页/落地页",
    "site": "完整网站", "modify": "修改已有", "game": "互动游戏",
    "readme": "README", "tutorial": "教程指南", "plan": "方案设计",
    "text": "文本翻译", "code_lang": "代码翻译", "design": "UI设计",
    "search": "联网搜索",
}


async def detect_intent_v2(messages: list[dict], model_id: str = "deepseek",
                           conversation_id: int | None = None,
                           context_hint: str = "",
                           project_status: str = "draft",
                           project_constraints: list[str] | None = None,
                           checkpoint_info: dict | None = None) -> dict:
    """v2 意图管道: 5模块并行 → PipelineResult → 兼容旧 dict。"""
    t0 = time.time()
    result = await classify_v2(
        messages, model_id,
        conversation_id=conversation_id,
        context_hint=context_hint,
        project_status=project_status,
        project_constraints=project_constraints,
        checkpoint_info=checkpoint_info,
    )
    l1 = result.intent["level1"]
    l2 = result.intent["level2"]
    elapsed = time.time() - t0
    label1 = LEVEL1_LABELS.get(l1, l1)
    label2 = LEVEL2_LABELS.get(l2, l2)
    logger.info("[意图v2] %s→%s | 决策=%s skill=%s | 置信度%.0f%% | 耗时%.1fs",
                label1, label2, result.decision, result.selected_skill,
                result.intent["confidence"] * 100, elapsed)
    return {
        "level1": l1, "level2": l2,
        "confidence": result.intent["confidence"],
        "industry": result.intent["industry"],
        "checkpoint_relation": "none",
        "label": f"{label1} · {label2}",
        "level1_label": label1, "level2_label": label2,
        # v2 扩展字段
        "decision": result.decision,
        "selected_skill": result.selected_skill,
        "risk_level": result.risk.risk_level,
        "requires_confirm": result.risk.requires_confirm,
        "evidence": result.evidence,
        "plan": result.plan,
    }


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
