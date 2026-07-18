"""Tool: web_search(联网搜索 · Tavily / Serper / DuckDuckGo 三级回退)。

成熟来源:
  - 生产:Tavily API(面向 LLM 的成熟搜索 API)或 Serper(Google 结果 API)
  - 开发兜底:DuckDuckGo lite HTML(免 key,结果质量有限)
用途:生成时检索最新资料 / 竞品参考 / 技术文档。
"""
from __future__ import annotations

import httpx

from ..config import settings
from ..registry import tool


@tool(
    name="web_search",
    scope="internal",
    risk="safe",
    description="联网搜索,返回相关网页标题/链接/摘要。优先 Tavily,其次 Serper,均无 key 时回退 DuckDuckGo lite。",
    schema={
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索并返回相关结果列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "top_k": {"type": "integer", "description": "返回条数,默认取配置 web_search_top_k"},
                },
                "required": ["query"],
            },
        },
    },
)
async def web_search(query: str, top_k: int | None = None) -> dict:
    top_k = top_k or settings.web_search_top_k
    try:
        if settings.tavily_api_key:
            return await _tavily(query, top_k)
        if settings.serper_api_key:
            return await _serper(query, top_k)
        return await _ddg(query, top_k)
    except Exception as e:
        return {"ok": False, "error": f"web_search 失败:{e}"}


async def _tavily(query: str, top_k: int) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "max_results": top_k,
                "search_depth": "basic",
            },
        )
        r.raise_for_status()
        data = r.json()
    results = [
        {"title": i.get("title", ""), "url": i.get("url", ""), "snippet": i.get("content", "")}
        for i in data.get("results", [])
    ]
    return {"ok": True, "provider": "tavily", "results": results}


async def _serper(query: str, top_k: int) -> dict:
    async with httpx.AsyncClient(
        timeout=15, headers={"X-SERPER-KEY": settings.serper_api_key}
    ) as c:
        r = await c.post("https://google.serper.dev/search", json={"q": query, "num": top_k})
        r.raise_for_status()
        data = r.json()
    results = [
        {"title": i.get("title", ""), "url": i.get("link", ""), "snippet": i.get("snippet", "")}
        for i in data.get("organic", [])
    ]
    return {"ok": True, "provider": "serper", "results": results}


async def _ddg(query: str, top_k: int) -> dict:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {"ok": False, "error": "beautifulsoup4 未安装(pip install beautifulsoup4)"}
    try:
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": "Mozilla/5.0 SeedAI"}
        ) as c:
            # DuckDuckGo lite 为表单 POST
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
            r.raise_for_status()
    except Exception as e:
        return {"ok": False, "error": f"DuckDuckGo 请求失败:{e}"}
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for node in soup.select(".result__body")[:top_k]:
        a = node.select_one("a.result__a")
        snippet = node.select_one(".result__snippet")
        if a:
            results.append(
                {
                    "title": a.get_text(strip=True),
                    "url": a.get("href", ""),
                    "snippet": snippet.get_text(strip=True) if snippet else "",
                }
            )
    return {
        "ok": True,
        "provider": "duckduckgo-lite",
        "results": results,
        "note": "无搜索 API key,使用免密 DuckDuckGo lite(结果质量有限,生产建议配 Tavily/Serper)",
    }
