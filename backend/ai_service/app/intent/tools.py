"""工具模块: 根据意图+置信度匹配可用技能, 按置信度排序。

输出 ToolResult {skills[], fallback}
"""

from __future__ import annotations

from dataclasses import dataclass, field

# (level1, level2) → skill_name
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
        return ToolResult(fallback="explain")

    # 状态路由: draft/planning → requirement_agent
    if skill_name == "builder_agent" and project_status in ("draft", "planning"):
        skill_name = "requirement_agent"

    # 根据置信度生成候选列表
    if confidence >= 0.8:
        return ToolResult(
            skills=[SkillCandidate(name=skill_name, confidence=confidence,
                                   reason=f"意图匹配: {level1}/{level2}")],
            fallback="explain",
        )
    elif confidence >= 0.5:
        # 中等置信度: 路由 + 标记低置信
        return ToolResult(
            skills=[SkillCandidate(name=skill_name, confidence=confidence,
                                   reason=f"意图匹配: {level1}/{level2} (置信度偏低)")],
            fallback="explain",
        )
    else:
        # 低置信度: 返回多候选(供汇总器出 options)
        return ToolResult(
            skills=[SkillCandidate(name=skill_name, confidence=confidence, reason="低置信度"),
                    SkillCandidate(name="explain", confidence=0.5, reason="兜底")],
            fallback="explain",
        )
