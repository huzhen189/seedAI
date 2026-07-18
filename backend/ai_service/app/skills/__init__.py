"""Skills 包:导入即注册(§5.8)。

每个模块在导入时调用 register_skill(...) 注册进 SkillRegistry。
新增 Skill:在此文件加一行 `from . import xxx`(或丢文件进目录由 registries.bootstrap 扫描)。
"""

from . import (
    explain,
    generate_doc,
    generate_site,
    rag_retrieve_skill,
    write_code,
)


__all__ = [
    "generate_site",
    "write_code",
    "generate_doc",
    "explain",
    "rag_retrieve_skill",
]
