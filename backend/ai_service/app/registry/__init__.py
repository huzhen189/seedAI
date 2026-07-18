"""Registry 包:SkillRegistry + ToolRegistry。

见设计文档 §5.8(Skill 引入与注册)/ §5.9(Tool 来源与注册)。
"""
from .skill_registry import SkillEntry, SkillRegistry, register_skill
from .tool_registry import ToolEntry, ToolRegistry, register_tool, tool

__all__ = [
    "SkillEntry",
    "SkillRegistry",
    "register_skill",
    "ToolEntry",
    "ToolRegistry",
    "register_tool",
    "tool",
]
