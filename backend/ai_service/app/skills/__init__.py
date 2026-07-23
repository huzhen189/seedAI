"""Skills 包:导入即注册(v1.0 重构)。

8 个 agent:
  Chat:  agent_chat, agent_search, agent_design, agent_doc
  Build: agent_requirement, agent_build, agent_review, agent_generate_site

旧文件保留兼容(逐步移除): explain, search_agent, design_agent, generate_doc,
  requirement_agent, generate_site, write_code, fix_agent, review_agent, builder_agent
"""

from . import (
    # v1.0 新 agent
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
    "design_agent",
    "explain",
    "fix_agent",
    "generate_doc",
    "generate_site",
    "rag_retrieve_skill",
    "requirement_agent",
    "review_agent",
    "search_agent",
    "write_code",
]
