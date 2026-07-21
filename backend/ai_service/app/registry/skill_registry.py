"""Skill 注册表(SkillRegistry · §5.8)。

开闭原则:新增 Skill 只调 register_skill(...),Router 核心无需改动。
SkillEntry 字段严格对齐设计文档 §5.8:
  - name        : 技能名(如 generate_site)
  - intent_tags : 意图标签,辅助 Router 语义匹配(如 ["site","网页","页面"])
  - handler     : 处理函数 / 已编译 LangGraph app
  - is_graph    : 是否多 agent 状态图(True 时 handler 为 graph.compile())
  - description : 给 Router / 前端展示的说明
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class SkillEntry:
    name: str
    intent_tags: list[str]
    handler: Callable[..., Any]
    is_graph: bool = False
    description: str = ""
    display_name: str = ""   # 前端显示名
    avatar: str = ""          # 头像 emoji
    role: str = ""            # 角色标签

    def to_dict(self) -> dict:
        return {
            "id": self.name,
            "name": self.display_name or self.name,
            "avatar": self.avatar,
            "role": self.role,
            "description": self.description,
        }


class SkillRegistry:
    """全局 Skill 注册表(dict[name -> SkillEntry])。"""

    _entries: dict[str, SkillEntry] = {}

    # ---- 写 ----
    @classmethod
    def register(cls, entry: SkillEntry) -> None:
        cls._entries[entry.name] = entry

    # ---- 读 ----
    @classmethod
    def get(cls, name: str) -> SkillEntry | None:
        return cls._entries.get(name)

    @classmethod
    def all(cls) -> list[SkillEntry]:
        return list(cls._entries.values())

    @classmethod
    def names(cls) -> list[str]:
        return list(cls._entries.keys())

    @classmethod
    def list_agents(cls) -> list[dict]:
        """动态生成 Agent 列表(替代原 agents.py 手工维护)。"""
        return [e.to_dict() for e in cls._entries.values()]

    @classmethod
    def match(cls, text: str) -> SkillEntry | None:
        """轻量规则匹配:在 intent_tags 中做子串命中(生产可由 Router 小模型判定后按 name 取)。"""
        if not text:
            return None
        low = text.lower()
        for entry in cls._entries.values():
            for tag in entry.intent_tags:
                if tag and tag.lower() in low:
                    return entry
        return None


def register_skill(
    name: str,
    intent_tags: list[str],
    handler: Callable[..., Any],
    is_graph: bool = False,
    description: str = "",
    display_name: str = "",
    avatar: str = "",
    role: str = "",
) -> SkillEntry:
    """便捷注册函数。"""
    entry = SkillEntry(
        name=name,
        intent_tags=intent_tags,
        handler=handler,
        is_graph=is_graph,
        description=description,
        display_name=display_name,
        avatar=avatar,
        role=role,
    )
    SkillRegistry.register(entry)
    return entry
