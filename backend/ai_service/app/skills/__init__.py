"""Skills 包：导入即注册（v1.0 重构落地）。

8 个 agent（chat/build 各 4）：
  Chat:  agent_chat, agent_search, agent_design, agent_doc
  Build: agent_requirement, agent_build, agent_review, agent_generate_site

旧 11 个 skill 文件已于 v1.0 删除（见 docs/agent-skill-reorg-plan.md / docs/v1.0-版本更新日志.md）。
"""

from . import (
    agent_chat,
    agent_search,
    agent_design,
    agent_doc,
    agent_requirement,
    agent_build,
    agent_review,
    agent_generate_site,
)


__all__ = [
    "agent_chat",
    "agent_search",
    "agent_design",
    "agent_doc",
    "agent_requirement",
    "agent_build",
    "agent_review",
    "agent_generate_site",
]
