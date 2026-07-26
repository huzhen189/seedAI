"""Tool: file_write / file_read(本地产物读写 · 标准库 pathlib)。

成熟来源:Python 标准库 pathlib(零依赖、跨平台)。
用途:generate_site 产出单文件 HTML 落盘 artifacts 目录,随后由 cos_upload 投递。
"""

from __future__ import annotations
import logging

from pathlib import Path

from ..config import settings
from ..registry import tool
logger = logging.getLogger("ai_service.tools.file_io")


@tool(
    name="file_write",
    scope="internal",
    risk="safe",
    description="将产物(如单文件 HTML)写入本地 artifacts 目录,返回绝对路径。供 COS 投递前落盘。",
    schema={
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "将文本产物写入本地 artifacts 目录,返回绝对路径与字节数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对 artifact_dir 的文件路径,如 'user_1/site_2/v1/index.html'",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的文件内容(通常是完整 HTML 字符串)",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
)
def file_write(path: str, content: str) -> dict:
    root = Path(settings.artifact_dir).resolve()
    fp = (root / path).resolve()
    # S9 修复: 阻止路径穿越(如 '../../etc/passwd'),所有写入必须落在 artifact_dir 内
    if fp != root and root not in fp.parents:
        return {"ok": False, "error": "path_traversal", "path": str(path)}
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(fp), "bytes": len(content.encode("utf-8"))}


@tool(
    name="file_read",
    scope="internal",
    risk="safe",
    description="读取本地 artifacts 目录下的产物文件内容。",
    schema={
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "读取本地产物文件,返回文本内容与大小。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对 artifact_dir 的文件路径"}
                },
                "required": ["path"],
            },
        },
    },
)
def file_read(path: str) -> dict:
    root = Path(settings.artifact_dir).resolve()
    fp = (root / path).resolve()
    # S9 修复: 阻止路径穿越, 读取也必须限定在 artifact_dir 内
    if fp != root and root not in fp.parents:
        return {"ok": False, "error": "path_traversal", "path": str(path)}
    if not fp.exists():
        return {"ok": False, "error": "not_found", "path": str(fp)}
    text = fp.read_text(encoding="utf-8")
    return {
        "ok": True,
        "path": str(fp),
        "content": text,
        "bytes": len(text.encode("utf-8")),
    }
