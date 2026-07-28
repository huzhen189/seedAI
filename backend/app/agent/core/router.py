"""Router:意图管道 + Skill 分发。

- detect_intent_v2: 混合级联意图识别(classify_v3, 含多意图拆分) → PipelineResult → 兼容旧 dict
- skill_for: (level1, level2) → skill_name(委托意图目录单一来源)
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from ..intent.cascade import classify_v3
from ..intent.catalog import skill_for as catalog_skill_for
from ..registry import SkillRegistry


logger = logging.getLogger("ai_service.router")

# 两级标签(v1.0: Chat/Build/Manage 三大方向)
LEVEL1_LABELS = {
    "chat": "智能对话", "build": "建站生成", "manage": "管理操作", "unsupported": "不支持",
}

LEVEL2_LABELS: dict[str, str] = {
    # Chat 方向
    "casual": "闲聊", "explain": "概念解释", "compare": "技术对比",
    "search": "联网搜索", "design": "设计咨询", "translate": "翻译",
    # Build 方向
    "requirement": "需求分析", "site": "完整网站", "page": "单页",
    "modify": "修改已有", "fix": "修复Bug", "review": "代码评审",
    "game": "互动游戏", "doc": "文档生成",
}


async def detect_intent_v2(messages: list[dict], model_id: str = "deepseek",
                           conversation_id: int | None = None,
                           context_hint: str = "",
                           project_status: str = "draft",
                           project_constraints: list[str] | None = None,
                           user_id: int | None = None,
                           project_id: int | None = None,
                           has_requirement_doc: bool = False,
                           has_site_artifact: bool = False) -> dict:
    """意图识别入口: 统一走混合级联 classify_v3(自 v1.2.0 起为唯一分类器)。

    v0.9.0: 新增 user_id/project_id 用于 Chroma 上下文增强。
    v1.0.7: 新增 has_requirement_doc, 透传给工具路由决定是否放行建站。
    v1.2.0: 收敛为单一分类器(cascade), 移除 SIR(classify_v2)双轨分支。
    v1.2.6: 新增 has_site_artifact, 供上下文闸门(已落地站点 → 追问直路由 build_modify)。
    """
    t0 = time.time()
    # 精细日志[发送结构体]:打印意图识别的完整入参(消息数 + 末条用户输入 + 全部透传参数)
    _last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            _last_user = (m.get("content", "") or "")[:200]
            break
    logger.info(
        "[意图][发送结构体] msgs=%d model=%s "
        "conversation_id=%s project_status=%s has_req_doc=%s user_id=%s project_id=%s "
        "context_hint=%.80s project_constraints=%s | 末条用户输入=%.200s",
        len(messages), model_id, conversation_id, project_status, has_requirement_doc,
        user_id, project_id, context_hint or "无", project_constraints or [], _last_user,
    )
    logger.info("[意图] ENTER 混合级联 msgs=%d model=%s", len(messages), model_id)
    # 单一分类器: 混合级联(不再有 SIR 回退分支, 避免双轨不一致)
    result = await classify_v3(
        messages, model_id,
        conversation_id=conversation_id,
        context_hint=context_hint,
        project_status=project_status,
        project_constraints=project_constraints,
        user_id=user_id,
        project_id=project_id,
        has_requirement_doc=has_requirement_doc,
        has_site_artifact=has_site_artifact,
    )
    l1 = result.intent["level1"]
    l2 = result.intent["level2"]
    elapsed = time.time() - t0
    label1 = LEVEL1_LABELS.get(l1, l1)
    label2 = LEVEL2_LABELS.get(l2, l2)
    logger.info("[意图v2] %s→%s | 决策=%s skill=%s | 置信度%.0f%% | 耗时%.1fs",
                label1, label2, result.decision, result.selected_skill,
                result.intent["confidence"] * 100, elapsed)
    # 精细日志[返回结构体]:打印意图识别返回的完整 PipelineResult 关键字段
    logger.info(
        "[意图][返回结构体] intent=%s industry=%s decision=%s selected_skill=%s "
        "risk=%s sub_tasks=%d split_reason=%s clarify=%s",
        result.intent, result.intent.get("industry"), result.decision, result.selected_skill,
        result.risk.risk_level if result.risk else "?", len(result.sub_tasks),
        result.split_reason or "", result.clarify_questions or [],
    )
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
        # SIR 新增: 澄清 / 可观测
        "clarify_questions": result.clarify_questions,
        "clarify_rounds": result.clarify_rounds,
        # 澄清结构化选项(前端浮动卡片: 单选/多选 + 推荐标记 + 自由输入)
        "clarify_options": result.clarify_options,
        "clarify_multi": result.clarify_multi,
        "clarify_allow_free_text": result.clarify_allow_free_text,
        "clarify_free_text_hint": result.clarify_free_text_hint,
        "request_id": result.request_id,
        # 多意图编排(§多意图 v1.0): 拆分结果透传给 worker / 前端
        "sub_tasks": [dataclasses.asdict(s) for s in result.sub_tasks],
        "split_reason": result.split_reason,
    }


def skill_for(level1: str, level2: str) -> str | None:
    """(level1, level2) → skill_name。委托给意图目录单一来源(catalog.skill_for)。"""
    return catalog_skill_for(level1, level2)


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
