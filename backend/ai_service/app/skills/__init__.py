"""Skills 包:导入即注册(§5.8)。

每个模块在导入时调用 register_skill(...) 注册进 SkillRegistry。
新增 Skill:在此文件加一行 `from . import xxx`(或丢文件进目录由 registries.bootstrap 扫描)。
"""

from . import (
    builder_agent,
    design_agent,
    explain,
    fix_agent,
    generate_doc,
    generate_site,
    rag_retrieve_skill,
    requirement_agent,
    review_agent,
    search_agent,
    write_code,
)


__all__ = [
    "builder_agent",
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
