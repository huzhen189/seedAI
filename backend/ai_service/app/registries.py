"""Registry 引导与目录扫描(§5.8 / §5.9 M2+ 热插拔)。

- 导入 skills / tools 包即完成全部注册(各模块在导入时调用 register_skill / @tool)。
- bootstrap():对 tools/ skills/ 目录做兜底扫描,支持"直接丢文件进目录"即生效,
  无需修改 __init__(运营/用户贡献能力热插拔,§5.9 来源 B)。
- 单个贡献文件出错不阻断整体(沙箱式容错)。
"""
from __future__ import annotations

import importlib
import pkgutil


def _scan(subpkg: str) -> list[str]:
    """扫描 app/<subpkg>/ 下所有模块并导入,返回成功注册名列表。"""
    registered: list[str] = []
    pkg = importlib.import_module(f"app.{subpkg}")
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("__"):
            continue
        try:
            importlib.import_module(f"app.{subpkg}.{mod.name}")
            registered.append(mod.name)
        except Exception as e:  # 单个贡献文件错误不应阻断整体
            print(f"[registries] 扫描 app.{subpkg}.{mod.name} 出错: {e}")
    return registered


def bootstrap() -> dict:
    # 1) 显式包导入(已在 import 时注册所有内置 skills/tools)
    from . import skills, tools  # noqa: F401  (导入即注册)

    # 2) 目录兜底扫描(支持直接丢文件进 tools/ skills/)
    scanned_tools = _scan("tools")
    scanned_skills = _scan("skills")

    # 3) 汇总已注册清单
    from .registry import SkillRegistry, ToolRegistry

    return {
        "skills_registered": SkillRegistry.names(),
        "tools_registered": ToolRegistry.names(),
        "scanned_tools": scanned_tools,
        "scanned_skills": scanned_skills,
    }
