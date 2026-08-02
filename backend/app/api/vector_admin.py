"""向量库可视化管理（统计系统 / 超管专用）。

功能边界（与用户诉求一致：可视化 + 受限写操作）：
  - 只读：集合列表 / 计数 / 浏览向量点(id·文本·元数据) / 语义检索预览。
  - 受限写：删点(ids 或 where) / 新建点(upsert) / 清空集合。
    * 全部 `require_super_admin` 守卫；
    * 写操作结构化日志留痕（app.vector_admin，含操作人/集合/条数/样本）；
    * 全部 fail-soft：Chroma 不可达 / 出错返回明确错误码，不崩溃。

复用 `app.ragstore` 统一后端（共享 HttpClient 与 _QwenEmbeddingFunction），
语义检索直接走 `retrieve()`，保证与线上集合 1024 维一致。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.ragstore import (
    add_points,
    clear_collection,
    delete_points,
    get_point,
    list_collections,
    peek,
    retrieve,
)
from app.security import CurrentUser, require_super_admin

logger = logging.getLogger("app.vector_admin")

router = APIRouter(prefix="/admin/vector", tags=["admin-vector"])


# ── 请求模型 ────────────────────────────────────────────────────────────────
class QueryReq(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=50)
    where: dict[str, Any] | None = None


class DeleteReq(BaseModel):
    ids: list[str] | None = Field(default=None, max_length=500)
    where: dict[str, Any] | None = None


class PointIn(BaseModel):
    id: str | None = Field(default=None, max_length=200)
    document: str = Field(min_length=1, max_length=20000)
    metadata: dict[str, Any] | None = None


class AddReq(BaseModel):
    points: list[PointIn] = Field(min_length=1, max_length=200)


class ClearReq(BaseModel):
    confirm: bool = False


# ── 审计留痕 ────────────────────────────────────────────────────────────────
def _audit(action: str, user: CurrentUser, *, collection: str, detail: dict[str, Any]) -> None:
    logger.info(
        "[vector_admin] action=%s user=%s(%d) collection=%s detail=%s",
        action,
        getattr(user, "account", "?"),
        getattr(user, "id", 0),
        collection,
        json.dumps(detail, ensure_ascii=False, default=str),
    )


def _parse_where(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("where 必须是 JSON 对象")
        return obj
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, {"code": "BAD_WHERE", "message": f"where 不是合法 JSON: {exc}"}) from exc


# ── 只读端点 ────────────────────────────────────────────────────────────────
@router.get("/collections")
async def api_list_collections(_: CurrentUser = Depends(require_super_admin)):
    """列出全部集合（名称 / 计数 / 元数据）。"""
    cols = await list_collections()
    return {"collections": cols, "total": len(cols)}


@router.get("/collections/{name}")
async def api_browse(
    name: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    where: str | None = Query(None),
    _: CurrentUser = Depends(require_super_admin),
):
    """分页浏览某集合的向量点（不含原始向量）。"""
    filt = _parse_where(where)
    rows = await peek(name, limit=limit, offset=offset, where=filt)
    return {"collection": name, "limit": limit, "offset": offset, "count": len(rows), "points": rows}


@router.get("/collections/{name}/{point_id}")
async def api_point_detail(
    name: str,
    point_id: str,
    with_embedding: bool = Query(False),
    _: CurrentUser = Depends(require_super_admin),
):
    """单条向量点详情；with_embedding=true 附带原始向量（1024 维，体积大）。"""
    p = await get_point(name, point_id, with_embedding=with_embedding)
    if p is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "向量点不存在或集合不可用"})
    return {"collection": name, "point": p}


@router.post("/collections/{name}/query")
async def api_query(name: str, body: QueryReq, _: CurrentUser = Depends(require_super_admin)):
    """语义检索预览：用线上 Qwen 嵌入函数检索，返回带距离(cosine)的命中。"""
    hits = await retrieve(name, body.query, top_k=body.top_k, where=body.where)
    return {
        "collection": name,
        "query": body.query,
        "hits": [
            {"id": h.id, "text": h.text, "metadata": h.metadata, "distance": h.distance}
            for h in hits
        ],
    }


# ── 受限写端点（超管 + 留痕） ────────────────────────────────────────────────
@router.delete("/collections/{name}/points")
async def api_delete_points(
    name: str,
    body: DeleteReq,
    user: CurrentUser = Depends(require_super_admin),
):
    """删除向量点：ids 或 where 至少给一个。返回删除条数。"""
    if not body.ids and not body.where:
        raise HTTPException(400, {"code": "EMPTY", "message": "ids 与 where 至少给一个"})
    try:
        n = await delete_points(name, ids=body.ids, where=body.where)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, {"code": "DELETE_FAILED", "message": str(exc)[:200]}) from exc
    _audit("delete_points", user, collection=name, detail={
        "ids_sample": (body.ids or [])[:20],
        "where": body.where,
        "deleted": n,
    })
    return {"ok": True, "deleted": n}


@router.post("/collections/{name}/points")
async def api_add_points(
    name: str,
    body: AddReq,
    user: CurrentUser = Depends(require_super_admin),
):
    """新增/更新向量点（upsert 幂等）。id 缺省时自动生成 manual_{i}。"""
    ids = [p.id or f"manual_{i}" for i, p in enumerate(body.points)]
    docs = [p.document for p in body.points]
    metas = [p.metadata or {} for p in body.points]
    try:
        n = await add_points(name, ids=ids, documents=docs, metadatas=metas)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, {"code": "ADD_FAILED", "message": str(exc)[:200]}) from exc
    _audit("add_points", user, collection=name, detail={"count": n, "ids_sample": ids[:20]})
    return {"ok": True, "added": n, "ids": ids}


@router.delete("/collections/{name}/clear")
async def api_clear_collection(
    name: str,
    body: ClearReq,
    user: CurrentUser = Depends(require_super_admin),
):
    """清空整个集合（保留集合本身）。需 confirm=true。"""
    if not body.confirm:
        raise HTTPException(400, {"code": "NEED_CONFIRM", "message": "需 confirm=true 方可清空"})
    try:
        n = await clear_collection(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, {"code": "CLEAR_FAILED", "message": str(exc)[:200]}) from exc
    _audit("clear_collection", user, collection=name, detail={"removed": n})
    return {"ok": True, "removed": n}
