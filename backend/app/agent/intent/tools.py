"""工具模块: 意图→技能映射 + 状态路由 + 置信度排序(v1.2.5)。

技能映射唯一来源 = intent_catalog.json(经由 catalog.skill_for 派生),
彻底消除旧 tools.INTENT_SKILL_MAP 与目录两处维护的 R3 漂移。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .catalog import skill_for

logger = logging.getLogger("ai_service.intent.tools")


@dataclass
class SkillCandidate:
    name: str
    confidence: float
    reason: str = ""
    requires_doc: bool = False


@dataclass
class ToolResult:
    skills: list[SkillCandidate] = field(default_factory=list)
    fallback: str = "explain"


# 这些技能执行前必须先有「需求文档」
REQUIRES_DOC_SKILLS = frozenset({"agent_generate_site", "agent_build"})


def _mk_candidate(name: str, confidence: float, reason: str) -> SkillCandidate:
    return SkillCandidate(
        name=name, confidence=confidence, reason=reason,
        requires_doc=name in REQUIRES_DOC_SKILLS,
    )


def run_tools(level1: str, level2: str, confidence: float,
              industry: str = "other",
              project_status: str = "draft",
              has_requirement_doc: bool = False,
              has_conversation_requirement: bool = False) -> ToolResult:
    """意图→技能映射 + 状态路由(doc-gating)。

    技能名统一从 catalog 派生(单一来源); 若目录无对应条目则降级 explain。
    """
    skill_name = skill_for(level1, level2)
    if not skill_name:
        logger.info("[工具] 无匹配技能 intent=%s/%s → 降级explain", level1, level2)
        return ToolResult(fallback="explain")

    # 状态路由: 完整建站(agent_generate_site)要求先有需求文档。
    # 仅当「没有需求文档」且「对话中也读不到可读需求」时, 才改道回需求分析;
    # 否则(文档已存在, 或对话里已描述过具体需求)直接放行建站。
    if skill_name == "agent_generate_site" and not has_requirement_doc and not has_conversation_requirement:
        logger.info("[工具] 状态路由 agent_generate_site→agent_requirement (无需求文档且无对话需求, status=%s)", project_status)
        skill_name = "agent_requirement"

    if confidence >= 0.8:
        logger.info("[工具] 技能=%s conf=%.0f%% → 直接路由", skill_name, confidence * 100)
        return ToolResult(
            skills=[_mk_candidate(skill_name, confidence, reason=f"意图: {level1}/{level2}")],
            fallback="explain")
    elif confidence >= 0.5:
        logger.info("[工具] 技能=%s conf=%.0f%% → 路由(中置信)", skill_name, confidence * 100)
        return ToolResult(
            skills=[_mk_candidate(skill_name, confidence, reason=f"意图: {level1}/{level2} (中置信)")],
            fallback="explain")
    else:
        logger.info("[工具] 低置信 conf=%.0f%% → 出多选项", confidence * 100)
        return ToolResult(
            skills=[_mk_candidate(skill_name, confidence, reason="低置信"),
                    _mk_candidate("explain", 0.5, reason="兜底")],
            fallback="explain")


def resolve_skill(level1: str, level2: str, candidate: str = "") -> str:
    """子任务 skill 归一(供多意图拆分复用):

    1. 候选 skill 若已真实注册 → 直接用(尊重 LLM/上游指定);
    2. 否则按 (level1, level2) 从 catalog 派生;
    3. 兜底 explain。
    """
    if candidate:
        from ..registry import SkillRegistry
        if SkillRegistry.get(candidate):
            return candidate
    mapped = skill_for(level1, level2)
    if mapped:
        from ..registry import SkillRegistry
        if SkillRegistry.get(mapped):
            return mapped
    return "explain"
