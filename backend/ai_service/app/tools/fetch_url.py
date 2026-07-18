"""Tool: fetch_url(网页抓取 · httpx + BeautifulSoup)。

成熟来源:httpx(成熟异步 HTTP 客户端) + beautifulsoup4(成熟 HTML 解析)。
用途:生成/改写网站时,抓取参考站点正文,注入上下文增强质量。
"""
from __future__ import annotations

import httpx

from ..config import settings
from ..registry import tool


@tool(
    name="fetch_url",
    scope="internal",
    risk="safe",
    description="抓取网页 URL,返回标题、正文纯文本(去 script/style)与关键链接,用于生成时参考真实站点。",
    schema={
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "抓取网页并返回标题/正文/链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "目标网页 URL"},
                    "timeout": {"type": "integer", "description": "超时秒数,默认 10"},
                },
                "required": ["url"],
            },
        },
    },
)
async def fetch_url(url: str, timeout: int = 10) -> dict:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {"ok": False, "error": "beautifulsoup4 未安装(pip install beautifulsoup4)"}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 SeedAI"}
        ) as c:
            r = await c.get(url)
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style"]):
            t.decompose()
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        text = " ".join(soup.get_text(separator=" ").split())[:8000]
        links = [a.get("href") for a in soup.find_all("a", href=True)][:20]
        return {
            "ok": True,
            "url": str(r.url),
            "status": r.status_code,
            "title": title,
            "text": text,
            "links": links,
        }
    except Exception as e:
        return {"ok": False, "error": f"抓取失败:{e}"}
