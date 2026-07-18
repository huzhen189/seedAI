"""Tool: browser_screenshot(无头浏览器截图 · Playwright · 预览/响应式校验)。

成熟来源:Playwright(微软成熟浏览器自动化,跨 Chromium/Firefox/WebKit)。
risk=dangerous:会启动浏览器进程,占用资源;经 ToolRegistry risk 字段控制(§5.9),
运营/用户贡献工具需沙箱 + 权限白名单。MVP 仅内部 agent 在需要时调用。
用途:对生成产物 URL/HTML 截图,做响应式与视觉校验(node:enter reviewer 阶段)。
"""
from __future__ import annotations

from pathlib import Path

from ..config import settings
from ..registry import tool


@tool(
    name="browser_screenshot",
    scope="internal",
    risk="dangerous",
    description="用无头 Chromium 对 URL 或 HTML 截图,用于预览/响应式校验。需安装 playwright + chromium。",
    schema={
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "对网页或 HTML 字符串截图,返回本地图片路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "URL 或 HTML 字符串"},
                    "source_type": {
                        "type": "string",
                        "description": "url 或 html",
                        "enum": ["url", "html"],
                    },
                    "width": {"type": "integer", "description": "视口宽,默认 1280"},
                    "height": {"type": "integer", "description": "视口高,默认 800"},
                    "wait_ms": {"type": "integer", "description": "截图前等待毫秒,默认 500"},
                },
                "required": ["source"],
            },
        },
    },
)
async def browser_screenshot(
    source: str,
    source_type: str = "url",
    width: int = 1280,
    height: int = 800,
    wait_ms: int = 500,
) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "ok": False,
            "error": "playwright 未安装(pip install playwright && playwright install chromium)",
        }
    out = Path(settings.artifact_dir) / "shots"
    out.mkdir(parents=True, exist_ok=True)
    shot_path = out / f"shot_{abs(hash(source)) % 10 ** 8}.png"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": height})
            if source_type == "html":
                await page.set_content(source)
            else:
                await page.goto(source, wait_until="networkidle")
            await page.wait_for_timeout(wait_ms)
            await page.screenshot(path=str(shot_path))
            await browser.close()
        return {"ok": True, "path": str(shot_path.resolve())}
    except Exception as e:
        return {"ok": False, "error": f"截图失败:{e}"}
