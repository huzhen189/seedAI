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
    ("build", "page"): "generate_site",
    ("build", "site"): "generate_site",
    ("build", "modify"): "generate_site",
    ("build", "game"): "generate_site",
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
