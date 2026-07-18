"""兼容层:generate_site 核心逻辑已迁移到 skills/generate_site.py(§5.8 SkillRegistry)。

本文件仅保留向后兼容导入,避免旧引用断裂。新代码请使用:
    from .skills.generate_site import generate_stream, app, build_graph
"""

from .skills.generate_site import app, build_graph, generate_stream


__all__ = ["generate_stream", "app", "build_graph"]
