"""Tool: image_generate(文生图 · OpenAI 兼容图像 API)。

成熟来源:OpenAI 兼容 images API(可接 DALL·E / 通义万相 / 自建扩散服务等)。
scope=user_exposed:用户可在 UI 显式触发"生成配图"。
说明:图像 API 非必须能力;未配置 image_api_key/image_api_base 时返回清晰状态,
不阻塞其它能力(§5.9 来源 A 内置工具,mature 库可平滑接入)。
"""
from __future__ import annotations

import httpx

from ..config import settings
from ..registry import tool


@tool(
    name="image_generate",
    scope="user_exposed",
    risk="safe",
    description="文生图:调用 OpenAI 兼容图像 API 生成图片,返回图片 URL 或 base64。未配置时返回清晰状态。",
    schema={
        "type": "function",
        "function": {
            "name": "image_generate",
            "description": "根据提示词生成图片。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图像描述提示词"},
                    "size": {"type": "string", "description": "尺寸,如 1024x1024", "enum": ["1024x1024", "1792x1024", "1024x1792"]},
                    "quality": {"type": "string", "description": "质量", "enum": ["standard", "hd"]},
                },
                "required": ["prompt"],
            },
        },
    },
)
async def image_generate(prompt: str, size: str = "1024x1024", quality: str = "standard") -> dict:
    if not (settings.image_api_key and settings.image_api_base):
        return {
            "ok": False,
            "configured": False,
            "error": "未配置图像生成(image_api_key/image_api_base),需在 .env 配置后启用(§5.9 tool:image_generate)",
        }
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{settings.image_api_base.rstrip('/')}/images/generations",
                headers={
                    "Authorization": f"Bearer {settings.image_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.image_model,
                    "prompt": prompt,
                    "size": size,
                    "quality": quality,
                    "n": 1,
                },
            )
            r.raise_for_status()
            data = r.json()
        item = (data.get("data") or [{}])[0]
        return {"ok": True, "url": item.get("url"), "b64": item.get("b64_json")}
    except Exception as e:
        return {"ok": False, "error": f"图像生成失败:{e}"}
