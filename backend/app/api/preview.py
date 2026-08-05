"""签名预览端点(REQ-PREVIEW-001 / SEC-PREVIEW-001)。

规范约束:
  - 预览产物必须由「不携带平台凭证」的独立 Origin 提供(preview_origin);
  - 访问凭据是短期 HMAC 签名 URL, 过期后必须重新签发, 不得当永久字段存库/缓存;
  - 响应受严格 CSP 约束(frame-ancestors 限定平台 Origin, base-uri/object-src/form-action 全禁);
  - 签名绑定 project / artifact / version / purge_generation / owner / exp ——
    purge 递增 generation 后, 所有历史签名立即失效(防「已清除项目被旧链接复活」)。

与 v2 遗留 `app/artifacts_auth.py` 的区别:
  旧方案依赖 nginx auth_request 子请求 + 平台 Cookie 判定, 产物与平台同源, 且引用了
  v3 已删除的 `deleted_at`/`is_public` 列 —— 既不安全也已失效, M9a 起由本模块取代。

签名格式(紧凑, 不用 JWT 以免与登录 token 共用密钥/解析器):
    token = b64url(payload_json) + "." + b64url(hmac_sha256(key, payload_b64))
  key 由 jwt_secret 经固定 salt 派生, 与登录签名域隔离。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import Artifact, Project
from app.security import CurrentUser, get_current_user


logger = logging.getLogger("app.api.preview")

router = APIRouter(tags=["preview"])

# 签名域分离: 预览令牌绝不能与登录 access/refresh 令牌互换。
_SIGNING_SALT = b"seedai:preview-grant:v1"
# 可预览的 Artifact 状态(building/failed/deleted 一律不签发)。
_PREVIEWABLE = ("verified", "preview_ready")
# 项目处于 trashed/purging 时禁止签发与访问。
_SERVABLE_PROJECT_STATUS = ("draft", "active")


# ---------------------------------------------------------------- 签名


def _signing_key() -> bytes:
    return hashlib.sha256(_SIGNING_SALT + settings.jwt_secret.encode("utf-8")).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_preview_token(payload: dict[str, Any]) -> str:
    """签发预览令牌。payload 键名保持单字符以压缩 URL 长度。"""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body_b64 = _b64e(body)
    sig = hmac.new(_signing_key(), body_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{body_b64}.{_b64e(sig)}"


def verify_preview_token(token: str) -> dict[str, Any]:
    """校验并解出 payload。签名错/格式错抛 403, 过期抛 410(语义: 需重新签发)。"""
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(status_code=403, detail={"code": "PREVIEW_TOKEN_MALFORMED"})
    body_b64, sig_b64 = parts
    expected = hmac.new(_signing_key(), body_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64d(sig_b64)
    except Exception:
        raise HTTPException(status_code=403, detail={"code": "PREVIEW_TOKEN_MALFORMED"}) from None
    # 恒定时间比较, 防签名侧信道爆破。
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail={"code": "PREVIEW_TOKEN_INVALID"})
    try:
        payload = json.loads(_b64d(body_b64))
    except Exception:
        raise HTTPException(status_code=403, detail={"code": "PREVIEW_TOKEN_MALFORMED"}) from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=403, detail={"code": "PREVIEW_TOKEN_MALFORMED"})
    if int(payload.get("e", 0)) <= int(time.time()):
        raise HTTPException(status_code=410, detail={"code": "PREVIEW_TOKEN_EXPIRED"})
    return payload


# ---------------------------------------------------------------- 安全响应头


def _frame_ancestors() -> str:
    origins = [o for o in settings.cors_origin_list if o and o != "*"]
    return " ".join(origins) if origins else "'self'"


def _preview_headers() -> dict[str, str]:
    """产物响应的安全头。产物是平台生成的静态站点, 允许内联样式/脚本与本域资源,
    但禁止发起跨域数据外带(connect-src 'none')、禁止表单提交、禁止改 base-uri。"""
    csp = "; ".join(
        [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            "connect-src 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            f"frame-ancestors {_frame_ancestors()}",
        ]
    )
    return {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Cross-Origin-Opener-Policy": "same-origin",
        # 短期签名 URL 不得被中间缓存留存。
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        # 预览环境不授予任何高权限特性。
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }


# ---------------------------------------------------------------- 路径解析


def _artifact_root() -> Path:
    return Path(settings.artifact_dir).resolve()


def _resolve_within(base_dir: Path, rel: str) -> Path:
    """把请求路径安全地解析到 base_dir 内; 越界/穿越一律 403。"""
    cleaned = (rel or "").replace("\\", "/").strip("/")
    if not cleaned:
        cleaned = "index.html"
    if any(seg in ("..", "") for seg in cleaned.split("/")):
        raise HTTPException(status_code=403, detail={"code": "PREVIEW_PATH_FORBIDDEN"})
    target = (base_dir / cleaned).resolve()
    if target != base_dir and base_dir not in target.parents:
        raise HTTPException(status_code=403, detail={"code": "PREVIEW_PATH_FORBIDDEN"})
    return target


def _base_dir_of(artifact: Artifact) -> Path:
    """产物目录 = artifact_dir / dirname(preview_path)。preview_path 存的是相对路径。"""
    if not artifact.preview_path:
        raise HTTPException(status_code=404, detail={"code": "ARTIFACT_NOT_PREVIEWABLE"})
    rel = Path(str(artifact.preview_path).replace("\\", "/"))
    if rel.is_absolute():
        raise HTTPException(status_code=500, detail={"code": "ARTIFACT_PATH_INVALID"})
    root = _artifact_root()
    base = (root / rel).resolve().parent
    if root not in base.parents and base != root:
        raise HTTPException(status_code=500, detail={"code": "ARTIFACT_PATH_INVALID"})
    return base


# ---------------------------------------------------------------- 契约


class PreviewGrantRequest(BaseModel):
    """不传 artifact_id 时默认签发项目 head(最新可预览版本)。"""

    artifact_id: int | None = Field(default=None, ge=1)
    entry: str = Field(default="index.html", max_length=256)


def _grant_url(request: Request, token: str, entry: str) -> str:
    origin = settings.preview_origin
    path = f"/preview/{token}/{entry.lstrip('/')}"
    if origin:
        return f"{origin}{path}"
    # 本地开发: 无独立 Origin 时退回同源绝对 URL, 前端仍按签名 URL 处理。
    return f"{str(request.base_url).rstrip('/')}{path}"


# ---------------------------------------------------------------- 端点


@router.get("/api/projects/{project_id}/artifacts")
async def list_artifacts(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """列出项目的 Artifact 版本(倒序)。前端版本切换与预览签发都依赖它。"""
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    rows = (
        await session.execute(
            select(Artifact)
            .where(Artifact.project_id == project_id, Artifact.status != "deleted")
            .order_by(Artifact.version.desc())
            .limit(200)
        )
    ).scalars().all()
    return [
        {
            "id": a.id,
            "project_id": a.project_id,
            "version": a.version,
            "status": a.status,
            "previewable": a.status in _PREVIEWABLE and bool(a.preview_path),
            "capability_manifest": a.capability_manifest,
            "manifest_digest": a.manifest_digest,
            "trace_id": a.trace_id,
            "is_head": a.id == project.head_artifact_id,
            "is_published": a.id == project.published_artifact_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


@router.post("/api/projects/{project_id}/preview-grant")
async def create_preview_grant(
    project_id: int,
    payload: PreviewGrantRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """签发短期预览 URL。前端必须在过期前重新调用本端点, 不得缓存为永久字段。"""
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    if project.status not in _SERVABLE_PROJECT_STATUS:
        raise HTTPException(
            status_code=409,
            detail={"code": "PROJECT_NOT_PREVIEWABLE", "status": project.status},
        )

    artifact_id = payload.artifact_id or project.head_artifact_id
    if artifact_id is None:
        raise HTTPException(status_code=404, detail={"code": "NO_ARTIFACT"})
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail={"code": "ARTIFACT_NOT_FOUND"})
    if artifact.status not in _PREVIEWABLE or not artifact.preview_path:
        raise HTTPException(
            status_code=409,
            detail={"code": "ARTIFACT_NOT_PREVIEWABLE", "status": artifact.status},
        )

    ttl = settings.preview_grant_ttl
    expires_at = int(time.time()) + ttl
    token = sign_preview_token(
        {
            "p": project_id,
            "a": artifact.id,
            "v": artifact.version,
            "u": project.user_id,
            "g": project.purge_generation,
            "e": expires_at,
        }
    )
    entry = payload.entry.strip() or "index.html"
    logger.info(
        "[preview] grant pid=%s aid=%s v=%s ttl=%ss uid=%s",
        project_id,
        artifact.id,
        artifact.version,
        ttl,
        user.id,
    )
    return {
        "url": _grant_url(request, token, entry),
        "artifact_id": artifact.id,
        "version": artifact.version,
        "expires_at": expires_at,
        "expires_in": ttl,
        # 独立 Origin 未配置时告知前端: 当前为同源降级(仅本地开发允许)。
        "isolated_origin": bool(settings.preview_origin),
    }


@router.get("/preview/{token}/{path:path}")
async def serve_preview(
    token: str,
    path: str,
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    """无凭证提供产物文件。本端点刻意不读取任何 Cookie / Authorization ——
    唯一凭据是 URL 里的短期签名, 因此可安全地部署在独立 Origin 上。"""
    payload = verify_preview_token(token)

    project = await session.get(Project, int(payload.get("p", 0)))
    if project is None or project.status not in _SERVABLE_PROJECT_STATUS:
        raise HTTPException(status_code=404, detail={"code": "PREVIEW_NOT_FOUND"})
    # purge 会递增 generation: 历史签名立刻作废, 避免已清除内容被旧链接读出。
    if int(payload.get("g", -1)) != project.purge_generation:
        raise HTTPException(status_code=410, detail={"code": "PREVIEW_REVOKED"})
    if int(payload.get("u", -1)) != project.user_id:
        raise HTTPException(status_code=403, detail={"code": "PREVIEW_OWNER_MISMATCH"})

    artifact = await session.get(Artifact, int(payload.get("a", 0)))
    if artifact is None or artifact.project_id != project.id or artifact.status == "deleted":
        raise HTTPException(status_code=404, detail={"code": "PREVIEW_NOT_FOUND"})

    target = _resolve_within(_base_dir_of(artifact), path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail={"code": "PREVIEW_FILE_NOT_FOUND"})

    media = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(str(target), media_type=media, headers=_preview_headers())


@router.get("/preview/{token}", include_in_schema=False)
async def serve_preview_root(
    token: str,
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    """无路径时等价于 index.html(便于直接粘贴签名根 URL)。"""
    return await serve_preview(token=token, path="index.html", session=session)


@router.get("/api/projects/{project_id}/artifacts/{artifact_id}/files")
async def list_artifact_files(
    project_id: int,
    artifact_id: int,
    with_content: bool = False,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """列出(可选含内容)已生成站点的源码文件, 供前端「代码」视图展示。

    安全: 严格校验 owner 与 previewable, 文件遍历限定在产物目录内(同 _resolve_within 的
    越界防护), 不暴露产物目录外的任何路径。
    """
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail={"code": "ARTIFACT_NOT_FOUND"})
    if artifact.status not in _PREVIEWABLE or not artifact.preview_path:
        raise HTTPException(
            status_code=409,
            detail={"code": "ARTIFACT_NOT_PREVIEWABLE", "status": artifact.status},
        )

    base = _base_dir_of(artifact)
    files: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if rel.lower().endswith((".html", ".htm", ".css", ".js", ".json", ".svg", ".txt", ".md")):
            entry: dict[str, Any] = {"name": rel, "size": path.stat().st_size}
            if with_content:
                try:
                    entry["content"] = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    entry["content"] = ""
            files.append(entry)
    return {"files": files}


@router.get("/api/projects/{project_id}/artifacts/{artifact_id}/files/{path:path}")
async def get_artifact_file(
    project_id: int,
    artifact_id: int,
    path: str,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """按相对路径返回单个源码文件内容(供「代码」视图按需加载大文件)。"""
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail={"code": "ARTIFACT_NOT_FOUND"})
    if artifact.status not in _PREVIEWABLE or not artifact.preview_path:
        raise HTTPException(
            status_code=409,
            detail={"code": "ARTIFACT_NOT_PREVIEWABLE", "status": artifact.status},
        )
    target = _resolve_within(_base_dir_of(artifact), path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail={"code": "PREVIEW_FILE_NOT_FOUND"})
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        content = ""
    return {"name": path, "content": content, "size": target.stat().st_size}


@router.get("/api/preview/health", include_in_schema=False)
async def preview_health() -> dict[str, Any]:
    """暴露预览隔离配置, 供 SEC-PREVIEW-001 冒烟与运维巡检读取(不含密钥)。"""
    return {
        "status": "ok",
        "isolated_origin": bool(settings.preview_origin),
        "preview_origin": settings.preview_origin or None,
        "grant_ttl": settings.preview_grant_ttl,
        "frame_ancestors": _frame_ancestors(),
        "ts": int(time.time()),
    }


__all__ = ["router", "sign_preview_token", "verify_preview_token"]
