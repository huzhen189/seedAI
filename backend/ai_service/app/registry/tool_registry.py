"""Tool 注册表(ToolRegistry · §5.9)。

开闭原则:新增 Tool 用 @tool 装饰器或 register_tool(...),Skill 内部 agent 经
function calling 即可见可用,Router / 核心无需改动。

ToolEntry 字段严格对齐设计文档 §5.9:
  - name   : 工具名(如 rag_retrieve)
  - schema : JSON Schema(OpenAI function-calling 格式),暴露给模型
  - func   : 实际执行函数(async 或 sync)
  - scope  : internal(仅内部 agent 调用) / user_exposed(用户可见可触发)
  - risk   : safe / dangerous(危险工具需权限校验 / 沙箱隔离)
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolEntry:
    name: str
    schema: dict  # OpenAI function-calling 格式: {"type":"function","function":{...}}
    func: Callable[..., Any]
    scope: str = "internal"  # internal | user_exposed
    risk: str = "safe"  # safe | dangerous
    description: str = ""


class ToolRegistry:
    """全局 Tool 注册表(dict[name -> ToolEntry])。"""

    _entries: dict[str, ToolEntry] = {}

    # ---- 写 ----
    @classmethod
    def register(cls, entry: ToolEntry) -> None:
        cls._entries[entry.name] = entry

    # ---- 读 ----
    @classmethod
    def get(cls, name: str) -> ToolEntry | None:
        return cls._entries.get(name)

    @classmethod
    def all(cls) -> list[ToolEntry]:
        return list(cls._entries.values())

    @classmethod
    def names(cls) -> list[str]:
        return list(cls._entries.keys())

    @classmethod
    def as_function_calling_specs(cls, include: str = "all") -> list[dict]:
        """导出供 LLM function calling 的 schema 列表。

        include:
          - "all"           : 全部
          - "user_exposed"  : 仅用户可见(scope=user_exposed)
          - "internal"      : 仅内部 agent 调用
        """
        out = []
        for e in cls._entries.values():
            if include == "all" or e.scope == include:
                out.append(e.schema)
        return out


def register_tool(
    name: str,
    schema: dict,
    func: Callable[..., Any],
    scope: str = "internal",
    risk: str = "safe",
    description: str = "",
) -> ToolEntry:
    entry = ToolEntry(
        name=name,
        schema=schema,
        func=func,
        scope=scope,
        risk=risk,
        description=description or (func.__doc__ or "").strip(),
    )
    ToolRegistry.register(entry)
    return entry


def _infer_schema(func: Callable, description: str) -> dict:
    """从函数签名 + 类型注解推断 OpenAI function-calling schema(缺省兜底,推荐显式传 schema)。"""
    sig = inspect.signature(func)
    props: dict[str, dict] = {}
    required: list[str] = []
    for pname, p in sig.parameters.items():
        if pname in ("ctx", "kwargs"):
            continue
        ann = p.annotation
        jtype = "string"
        if ann in (int, "int"):
            jtype = "integer"
        elif ann in (float, "float"):
            jtype = "number"
        elif ann in (bool, "bool"):
            jtype = "boolean"
        props[pname] = {"type": jtype, "description": ""}
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description or (func.__doc__ or "").strip(),
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


def tool(
    name: str | None = None,
    *,
    schema: dict | None = None,
    scope: str = "internal",
    risk: str = "safe",
    description: str = "",
):
    """装饰器:@tool(name="rag_retrieve", schema=..., scope=..., risk=...)。

    在模块导入时即把函数注册进 ToolRegistry(§5.9「内置工具」来源 A)。
    支持 sync / async 函数;schema 缺省时按签名推断。
    """

    def deco(func: Callable) -> Callable:
        tname = name or func.__name__
        tschema = schema or _infer_schema(func, description)
        register_tool(
            name=tname,
            schema=tschema,
            func=func,
            scope=scope,
            risk=risk,
            description=description or (func.__doc__ or "").strip(),
        )
        return func

    return deco
