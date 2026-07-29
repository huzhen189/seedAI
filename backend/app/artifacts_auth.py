"""本地产物静态资源鉴权(防软删/回收区项目直链越权读取)。

问题背景: P1 把生成产物落本地 artifacts/, 经 nginx `location /artifacts/` 或 vite
dev 中间件同源直出。但项目软删除(仅打 deleted_at)与回收区移动都不碰磁盘物理删除,
导致已隐藏/已删除的项目仍可被直链 `origin/artifacts/{uid}/{pid}/...` 读取, 造成泄露。

方案: nginx `auth_request /api/artifacts-auth;` 在每次静态请求前发一次子请求,
本端点返回 200(放行)/ 403(拒绝)。vite dev 中间件同步加一份 JS 兜底校验。

判定逻辑:
  1) 解析 path → {uid, pid}(支持 .trash 回收区前缀);
  2) 回收区内(.trash 开头)→ 一律 403(隐藏内容不经过静态直出);
  3) 项目已软删(deleted_at 非空)→ 403;
  4) 公开项目(is_public)→ 200(允许匿名预览);
  5) 私有项目 → 需有效 JWT 且 owner == 当前用户 → 200, 否则 401/403。

注意: 本端点为只读校验, 不签发/续期 token, 避免 auth_request 子请求产生 Cookie 副作用。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from sqlalchemy import select

from .db import get_db
from .models import Project
from .security import ACCESS_COOKIE, decode_token
from shared.artifacts import parse_rel_path

logger = logging.getLogger("business.artifacts_auth")

router = APIRouter(prefix="/api", tags=["artifacts-auth"])


def _extract_user_id(request: Request) -> int | None:
    """从 auth_request 子请求中尽量取出 user_id(Cookie / Bearer)。

    不抛异常: 取不到返回 None(视为匿名)。auth_request 子请求由 nginx 代发,
    浏览器 Cookie 会随子请求携带(HttpOnly, 同源)。
    """
    token = request.cookies.get(ACCESS_COOKIE)
    auth = request.headers.get("Authorization")
    if not token and auth and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        return None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None
        return int(payload.get("sub"))
    except Exception:
        return None


def _body_path(request: Request) -> str | None:
    """nginx auth_request 默认不转发 body, 故优先读 query 参数 `path`, 其次读 JSON body。"""
    try:
        p = request.query_params.get("path")
        if p:
            return p
    except Exception:
        pass
    try:
        import asyncio

        # auth_request 子请求一般无 body; 这里仅作兜底, 不阻塞。
        body = getattr(request, "_body", None)
        if body:
            import json

            data = json.loads(body)
            return data.get("path")
    except Exception:
        pass
    return None


@router.get("/artifacts-auth")
@router.post("/artifacts-auth")
async def artifacts_auth(request: Request):
    """静态资源读取鉴权子请求端点。nginx auth_request 调用, 返回 200/401/403。

    入参: query/path?= 或 body {path}; path 相对 /artifacts/ 之后的部分(如 uid/pid/v1/index.html)。
    """
    rel = _body_path(request)
    if not rel:
        # 也直接读 request.path_query 之外的原始 uri(由 nginx 通过 X-Original-URI 透传更稳)
        rel = request.headers.get("X-Original-URI") or request.headers.get("X-Original-URL")
        if rel:
            # 去掉前缀 /artifacts/ 与查询串
            rel = rel.split("?", 1)[0]
            if rel.startswith("/artifacts/"):
                rel = rel[len("/artifacts/"):]
            elif rel.startswith("/artifacts"):
                rel = rel[len("/artifacts"):]
    if not rel:
        logger.warning("[auth] /artifacts-auth 缺少 path, 拒绝")
        from fastapi import status
        from fastapi.responses import Response
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    parsed = parse_rel_path(rel)
    # 无法解析 → 视为非法路径, 拒(宁拒勿放, 但返回 400 让 try_files 走 404)
    if parsed is None:
        from fastapi import status
        from fastapi.responses import Response
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # 回收区或 .trash 前缀 → 一律拒绝(已删除内容不经过静态直出)
    if str(rel).replace("\\", "/").lstrip("/").startswith(".trash/"):
        from fastapi.responses import Response
        return Response(status_code=403)

    uid, pid = parsed["uid"], parsed["pid"]

    # 查项目归属与状态(需查库: deleted_at / is_public / user_id)
    from .db import SessionLocal
    try:
        async with SessionLocal() as session:
            proj = (await session.execute(
                select(Project).where(Project.id == pid)
            )).scalar_one_or_none()
            if proj is None:
                # 项目不存在(可能老 anon 数据/误构造) → 拒
                from fastapi.responses import Response
                return Response(status_code=403)
            # 软删 → 拒
            if proj.deleted_at is not None:
                from fastapi.responses import Response
                return Response(status_code=403)
            # 公开 → 放行
            if proj.is_public:
                from fastapi.responses import Response
                return Response(status_code=200)
            # 私有 → 需有效 JWT 且 owner
            owner = _extract_user_id(request)
            if owner is not None and owner == proj.user_id:
                from fastapi.responses import Response
                return Response(status_code=200)
            # 未登录或非 owner
            from fastapi import status
            from fastapi.responses import Response
            return Response(status_code=status.HTTP_401_UNAUTHORIZED if owner is None else 403)
    except Exception as e:  # noqa: BLE001 - 鉴权异常宁拒勿放
        logger.error("[auth] /artifacts-auth 异常(拒): %s", e)
        from fastapi.responses import Response
        return Response(status_code=403)
