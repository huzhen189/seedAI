"""§4 角色重构 · DevAgent(开发工程师)。

封装 agent_build / agent_generate_site / agent_doc,产出 CodeArtifact(强 Schema 交接物)。
上游交付物 = PRD + DesignSpec。
"""

from __future__ import annotations

import logging

from .base import RoleAgent

logger = logging.getLogger("ai_service.roles.dev")


class DevAgent(RoleAgent):
    role = "dev"
    owned_skills = ["agent_build", "agent_generate_site", "agent_doc"]

    def system_prompt_fragment(self) -> str:
        return (
            "你当前以「开发工程师」身份工作,职责是基于上游 PRD 与 DesignSpec 实现可运行的网站代码"
            "(CodeArtifact),并保证交付预览 URL。请严格遵循上游规范(品牌/配色/布局/功能优先级),"
            "不要擅自更改业务需求。"
        )
