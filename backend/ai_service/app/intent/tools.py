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
    ("build", "site"): "agent_generate_site",
    ("build", "page"): "agent_build",
    ("build", "modify"): "agent_build",
    ("build", "game"): "agent_build",
    ("build", "doc"): "agent_doc",
    ("build", "fix"): "agent_review",
    ("build", "review"): "agent_review",
    # Manage 方向 → Agent
    ("manage", "delete"): "agent_delete",
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


# 这些技能执行前必须先有「需求文档」(新架构 Agent 名)
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
    """工具模块入口: 意图→技能映射 + 状态路由。"""
    skill_name = INTENT_SKILL_MAP.get((level1, level2))
    if not skill_name:
        logger.info("[工具] 无匹配技能 intent=%s/%s → 降级explain", level1, level2)
        return ToolResult(fallback="explain")

    # 状态路由: 完整建站(agent_generate_site)要求先有需求文档。
    # 关键修复(RC3): 仅当项目「没有需求文档」且「对话中也读不到可读需求」时,
    # 才改道回需求分析; 否则(文档已存在, 或对话里已描述过具体需求如
    # "首页天气+附近美食+地图定位"), 直接放行建站, 避免用户说"按我刚刚的要求生成网站"
    # 被无限打回重做需求(死亡路由)。
    if skill_name == "agent_generate_site" and not has_requirement_doc and not has_conversation_requirement:
        logger.info("[工具] 状态路由 agent_generate_site→agent_requirement (无需求文档且无对话需求, status=%s)", project_status)
        skill_name = "agent_requirement"

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
