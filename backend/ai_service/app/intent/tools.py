"""工具模块: 意图→技能映射 + 状态路由 + 置信度排序。

输出 ToolResult {skills[], fallback}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("ai_service.intent.tools")

INTENT_SKILL_MAP: dict[tuple[str, str], str] = {
    ("learn", "explain"): "explain",
    ("learn", "debug"): "explain",
    ("learn", "compare"): "explain",
    ("learn", "casual"): "explain",
    ("code", "snippet"): "write_code",
    ("code", "component"): "write_code",
    ("code", "fix"): "fix_agent",
    ("code", "refactor"): "review_agent",
    ("build", "page"): "builder_agent",
    ("build", "site"): "builder_agent",
    ("build", "modify"): "builder_agent",
    ("build", "game"): "builder_agent",
    ("build", "building"): "builder_agent",
    ("build", "requirement"): "requirement_agent",
    ("learn", "design"): "design_agent",
    ("learn", "search"): "search_agent",
    ("doc", "readme"): "generate_doc",
    ("doc", "tutorial"): "generate_doc",
    ("doc", "plan"): "generate_doc",
    ("translate", "text"): "explain",
    ("translate", "code_lang"): "write_code",
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


def run_tools(level1: str, level2: str, confidence: float,
              industry: str = "other",
              project_status: str = "draft") -> ToolResult:
    """工具模块入口: 意图→技能映射 + 状态路由。"""
    skill_name = INTENT_SKILL_MAP.get((level1, level2))
    if not skill_name:
        logger.info("[工具] 无匹配技能 intent=%s/%s → 降级explain", level1, level2)
        return ToolResult(fallback="explain")

    # 状态路由
    if skill_name == "builder_agent" and project_status in ("draft", "planning"):
        logger.info("[工具] 状态路由 builder→requirement (status=%s)", project_status)
        skill_name = "requirement_agent"

    if confidence >= 0.8:
        logger.info("[工具] 技能=%s conf=%.0f%% → 直接路由", skill_name, confidence * 100)
        return ToolResult(
            skills=[SkillCandidate(name=skill_name, confidence=confidence,
                                   reason=f"意图: {level1}/{level2}")],
            fallback="explain")
    elif confidence >= 0.5:
        logger.info("[工具] 技能=%s conf=%.0f%% → 路由(中置信)", skill_name, confidence * 100)
        return ToolResult(
            skills=[SkillCandidate(name=skill_name, confidence=confidence,
                                   reason=f"意图: {level1}/{level2} (中置信)")],
            fallback="explain")
    else:
        logger.info("[工具] 低置信 conf=%.0f%% → 出多选项", confidence * 100)
        return ToolResult(
            skills=[SkillCandidate(name=skill_name, confidence=confidence, reason="低置信"),
                    SkillCandidate(name="explain", confidence=0.5, reason="兜底")],
            fallback="explain")
