"""Tool: html_validate(单文件 HTML 静态校验 · 标准库 html.parser)。

成熟来源:Python 标准库 html.parser(零依赖、容错解析)。
用途:Reviewer 节点(§5.3)做静态分析 —— 校验标签平衡、关键结构、是否含 script/style。
说明:MVP 用标准库做轻量校验;后续可平滑升级为 html5lib / tidy(更强容错),接口不变。
"""
from __future__ import annotations

from html.parser import HTMLParser

from ..registry import tool


class _HTMLValidator(HTMLParser):
    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img",
        "input", "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.issues: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.issues.append(f"未闭合标签: <{self.stack[-1]}>")
                self.stack.pop()
            if self.stack:
                self.stack.pop()
        else:
            self.issues.append(f"多余闭合标签: </{tag}>")


@tool(
    name="html_validate",
    scope="internal",
    risk="safe",
    description="轻量静态校验单文件 HTML:检查标签平衡、关键结构(<html>/<head>/<body>)、是否含 script/style。",
    schema={
        "type": "function",
        "function": {
            "name": "html_validate",
            "description": "校验 HTML 字符串,返回问题列表与结构标志。",
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {"type": "string", "description": "待校验的完整 HTML 字符串"}
                },
                "required": ["html"],
            },
        },
    },
)
def html_validate(html: str) -> dict:
    v = _HTMLValidator()
    try:
        v.feed(html)
    except Exception as e:
        return {"ok": False, "error": str(e), "issues": [str(e)]}
    for t in v.stack:
        v.issues.append(f"未闭合标签: <{t}>")
    low = html.lower()
    has_html = "<html" in low
    has_head = "<head" in low
    has_body = "<body" in low
    if not has_html:
        v.issues.append("缺少 <html> 根标签")
    if not has_body:
        v.issues.append("缺少 <body>")
    return {
        "ok": len(v.issues) == 0,
        "issues": v.issues,
        "has_html": has_html,
        "has_head": has_head,
        "has_body": has_body,
        "has_script": "<script" in low,
        "has_style": "<style" in low,
        "length": len(html),
    }
