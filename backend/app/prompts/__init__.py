"""集中管理的提示词注册表。

此前提示词散落在各模块硬编码（intent.py / chat/service.py），难调优、难版本化、难 A/B。
现统一收敛到此包，按用途分模块，便于版本追踪与统一调优。
"""
from .chat_system import CHAT_SYSTEM_PROMPT, CHAT_TEMPERATURE
from .intent_escalation import INTENT_ESCALATION_PROMPT

__all__ = [
    "CHAT_SYSTEM_PROMPT",
    "CHAT_TEMPERATURE",
    "INTENT_ESCALATION_PROMPT",
]
