"""子任务/工具执行的上下文与槽位作用域隔离——只携带自身相关切片，防污染。

原则：子任务(一个 ActionItem)与工具执行时，只应接触**自身相关**的上下文
（自己的入参、本域槽位、最小标识），不得把整个 TurnContext（全轮消息、
其它子任务的槽位、全局 DST）全量塞入，否则会造成跨子任务污染与不可预测行为。

- ``relevant_slots``：按域前缀过滤槽位。
- ``build_subtask_scope`` / ``SubTaskScope``：构造子任务最小上下文投影。
- ``build_tool_scope``：构造工具 extra（显式、最小，绝不放入整个 TurnContext）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def relevant_slots(slots: dict | None, domain: str) -> dict:
    """仅保留属于 ``domain`` 的槽位（键以 ``domain.`` 开头），丢弃无关子任务的槽位。"""
    if not slots:
        return {}
    prefix = f"{domain}."
    return {k: v for k, v in slots.items() if k.startswith(prefix)}


def domain_of_skill(skill: str | None) -> str:
    """由 ``skill``(如 ``site_create``) 推导域前缀(``site``)。"""
    if not skill:
        return ""
    return skill.split("_")[0]


@dataclass
class SubTaskScope:
    """子任务执行时允许接触的最小上下文投影（不含全轮对话/全局 DST）。"""

    user_id: Any = None
    project_id: Any = None
    conversation_id: Any = None
    turn_id: Any = None
    action_arguments: dict = field(default_factory=dict)
    slots: dict = field(default_factory=dict)


def build_subtask_scope(context: Any, action: Any, domain: str | None = None) -> SubTaskScope:
    """从 TurnContext + ActionItem 构造子任务作用域（只取自身相关）。"""
    dom = domain or domain_of_skill(getattr(action, "skill", None))
    slots = relevant_slots(getattr(getattr(context, "sir_after_dst", None), "slots", None), dom)
    return SubTaskScope(
        user_id=getattr(getattr(context, "user", None), "user_id", None),
        project_id=getattr(context, "project_id", None),
        conversation_id=getattr(context, "conversation_id", None),
        turn_id=getattr(context, "turn_id", None),
        action_arguments=dict(getattr(action, "arguments", None) or {}),
        slots=slots,
    )


def build_tool_scope(tool_id: str, context: Any, action: Any = None) -> dict:
    """构造工具执行时的 ``extra``：仅含工具自身相关的少量数据，**绝不**放入整个 TurnContext。

    调用方必须显式传入工具所需字段；默认只给最小标识，杜绝把 ctx 全量丢进 ``extra``。
    """
    return {
        "tool_id": tool_id,
        "user_id": getattr(getattr(context, "user", None), "user_id", None),
        "project_id": getattr(context, "project_id", None),
        "conversation_id": getattr(context, "conversation_id", None),
        "trace_id": getattr(context, "trace_id", None),
    }
