"""§4 角色重构 · DesignAgent(设计顾问)。

封装 agent_design,产出 DesignSpec(强 Schema 交接物)。上游交付物 = PRD。
"""

from __future__ import annotations

import logging

from .base import RoleAgent

logger = logging.getLogger("ai_service.roles.design")


class DesignAgent(RoleAgent):
    role = "design"
    owned_skills = ["agent_design"]

    def system_prompt_fragment(self) -> str:
        return (
            "你当前以「设计顾问」身份工作,职责是基于上游 PRD 产出设计规范(DesignSpec):"
            "配色方案、字体气质、布局结构、动效建议。请严格依据上游 PRD 的品牌定位与风格,"
            "不要重新定义业务目标。"
        )
