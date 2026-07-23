"""工具模块: 意图→技能映射 + 状态路由 + 置信度排序。

输出 ToolResult {skills[], fallback}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("ai_service.intent.tools")

INTENT_SKILL_MAP: dict[tuple[str, str], str] = {
    # Chat 方向 → Agent
    ("chat", "casual"): "agent_chat",
    ("chat", "explain"): "agent_chat",
    ("chat", "compare"): "agent_chat",
    ("chat", "translate"): "agent_chat",
    ("chat", "search"): "agent_search",
    ("chat", "design"): "agent_design",
    # Build 方向 → Agent
    ("build", "requirement"): "agent_requirement",
    ("build", "site"): "agent_build",
    ("build", "page"): "agent_build",
    ("build", "modify"): "agent_build",
    ("build", "game"): "agent_build",
    ("build", "fix"): "agent_review",
    ("build", "review"): "agent_review",
}


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
REQUIRES_DOC_SKILLS = frozenset({"generate_site", "builder_agent"})


def _mk_candidate(name: str, confidence: float, reason: str) -> SkillCandidate:
    return SkillCandidate(
        name=name, confidence=confidence, reason=reason,
        requires_doc=name in REQUIRES_DOC_SKILLS,
    )


def run_tools(level1: str, level2: str, confidence: float,
              industry: str = "other",
              project_status: str = "draft") -> ToolResult:
    """工具模块入口: 意图→技能映射 + 状态路由。"""
    skill_name = INTENT_SKILL_MAP.get((level1, level2))
    if not skill_name:
        logger.info("[工具] 无匹配技能 intent=%s/%s → 降级explain", level1, level2)
        return ToolResult(fallback="explain")

    # 状态路由(draft/planning 项目先走需求分析, 不直奔代码生成)
    if skill_name == "generate_site" and project_status in ("draft", "planning"):
        logger.info("[工具] 状态路由 generate_site→requirement_agent (status=%s)", project_status)
        skill_name = "requirement_agent"

    if confidence >= 0.8:
        logger.info("[工具] 技能=%s conf=%.0f%% → 直接路由", skill_name, confidence * 100)
        return ToolResult(
            skills=[_mk_candidate(skill_name, confidence,
                                  reason=f"意图: {level1}/{level2}")],
            fallback="explain")
    elif confidence >= 0.5:
        logger.info("[工具] 技能=%s conf=%.0f%% → 路由(中置信)", skill_name, confidence * 100)
        return ToolResult(
            skills=[_mk_candidate(skill_name, confidence,
                                  reason=f"意图: {level1}/{level2} (中置信)")],
            fallback="explain")
    else:
        logger.info("[工具] 低置信 conf=%.0f%% → 出多选项", confidence * 100)
        return ToolResult(
            skills=[_mk_candidate(skill_name, confidence, reason="低置信"),
                    _mk_candidate("explain", 0.5, reason="兜底")],
            fallback="explain")
