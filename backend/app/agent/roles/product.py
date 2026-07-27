"""§4 角色重构 · ProductAgent(产品分析师)。

封装 agent_requirement,产出 PRD(强 Schema 交接物)。无上游交付物(处于 SOP 首位)。
"""

from __future__ import annotations

import logging

from .base import RoleAgent

logger = logging.getLogger("ai_service.roles.product")


class ProductAgent(RoleAgent):
    role = "product"
    owned_skills = ["agent_requirement"]

    def system_prompt_fragment(self) -> str:
        return (
            "你当前以「产品分析师」身份工作,职责是把用户模糊的想法拆解、补全、专业化,"
            "产出一份详尽专业的产品需求文档(PRD)。你处于 SOP 首位,没有上游交付物,"
            "请基于用户对话独立产出 PRD。"
        )
