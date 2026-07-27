"""§4 角色重构 · QAAgent(质量评审)。

封装 agent_review(+scoring),产出 ReviewReport(强 Schema 交接物)。
上游交付物 = CodeArtifact(+PRD 验收标准)。
"""

from __future__ import annotations

import logging

from .base import RoleAgent

logger = logging.getLogger("ai_service.roles.qa")


class QAAgent(RoleAgent):
    role = "qa"
    owned_skills = ["agent_review"]

    def system_prompt_fragment(self) -> str:
        return (
            "你当前以「质量评审」身份工作,职责是基于上游 PRD 验收标准与 CodeArtifact,"
            "对生成站点做 7 维质量评审(ReviewReport),指出缺陷并给综合评分。"
            "请客观严格,对照上游验收标准逐条核对。"
        )
