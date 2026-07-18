"""Tool: cos_upload(腾讯云 COS 预览投递 · cos-python-sdk-v5 · §10)。

成熟来源:腾讯云官方 SDK cos-python-sdk-v5(稳定、文档完善)。
用途:generate_site 产出落盘后,由本工具上传到预览桶,返回线上直链 preview_url
      (前端 iframe 直接加载,无需本地 host 代理,§3.11)。
"""
from __future__ import annotations

from pathlib import Path

from ..config import settings
from ..registry import tool


@tool(
    name="cos_upload",
    scope="internal",
    risk="safe",
    description="将本地产物文件上传到腾讯云 COS 预览桶(§10),返回线上直链 preview_url。",
    schema={
        "type": "function",
        "function": {
            "name": "cos_upload",
            "description": "上传本地文件到 COS 并返回直链。",
            "parameters": {
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "本地文件绝对路径(通常由 file_write 产出)"},
                    "cos_key": {
                        "type": "string",
                        "description": "COS 对象键,如 'previews/{user_id}/{site_id}/{version}/index.html'",
                    },
                },
                "required": ["local_path", "cos_key"],
            },
        },
    },
)
def cos_upload(local_path: str, cos_key: str) -> dict:
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError:
        return {"ok": False, "error": "cos-python-sdk-v5 未安装(pip install cos-python-sdk-v5)"}
    if not (settings.cos_secret_id and settings.cos_secret_key):
        return {"ok": False, "error": "未配置 COS 密钥(.env: cos_secret_id / cos_secret_key)"}
    fp = Path(local_path)
    if not fp.exists():
        return {"ok": False, "error": f"local_path 不存在: {local_path}"}
    config = CosConfig(
        Region=settings.cos_region,
        SecretId=settings.cos_secret_id,
        SecretKey=settings.cos_secret_key,
    )
    client = CosS3Client(config)
    client.upload_file(Bucket=settings.cos_bucket, Key=cos_key, LocalFilePath=str(fp))
    url = f"{settings.cos_preview_domain.rstrip('/')}/{cos_key.lstrip('/')}"
    return {"ok": True, "url": url, "bucket": settings.cos_bucket, "key": cos_key}
