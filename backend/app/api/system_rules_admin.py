"""系统规则管理端 API（双轨：MySQL 真相 + 向量索引）。

权限：超管专用（与用户/角色管理、控制面同级）。写操作结构化日志留痕。
功能：列表（展示向量摘要）/ 详情（MySQL 原文）/ 新增 / 修改 / 删除 / 重建向量索引。

规则双轨设计见 services.system_rules 与 db.seed_system_rules：
  - MySQL(system_rules) 为唯一真相源，存 content 全文；
  - 向量集合仅存 summary+keywords 索引串，命中后回查 MySQL 取原文；
  - 本路由的写操作在更新 MySQL 后联动同步单条向量点（fail-soft）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.security import CurrentUser, require_super_admin
from app.services import system_rules as svc

logger = logging.getLogger("app.api.system_rules_admin")

router = APIRouter(prefix="/admin/system-rules", tags=["admin-system-rules"])

# rule_key 字符集：字母数字 + _ : . -，长度 ≤ 64（与数据库约束对齐）。
_KEY_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")


# ── 请求模型 ────────────────────────────────────────────────────────────────
class RuleCreate(BaseModel):
    rule_key: str = Field(min_length=1, max_length=64)
    scope: str
    scope_ref: str | None = Field(default=None, max_length=64)
    rule_type: str
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)
    keywords: str = Field(default="", max_length=500)
    priority: int = Field(default=50, ge=0, le=100)
    is_active: bool = True


class RuleUpdate(BaseModel):
    # 全部可选；用 exclude_unset 区分「未提供」与「显式置空」（如 scope_ref=null 清空）。
    scope: str | None = None
    scope_ref: str | None = None
    rule_type: str | None = None
    title: str | None = Field(default=None, max_length=200)
    content: str | None = None
    summary: str | None = Field(default=None, max_length=500)
    keywords: str | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    is_active: bool | None = None


# ── 校验 + 审计留痕 ──────────────────────────────────────────────────────────
def _validate(rule_key: str, scope: str, rule_type: str, scope_ref: str | None) -> None:
    if not _KEY_RE.match(rule_key):
        raise HTTPException(400, {
            "code": "BAD_KEY",
            "message": "rule_key 须为字母/数字/_/./-/:，长度 1-64",
        })
    if scope not in svc.SCOPE_VALUES:
        raise HTTPException(400, {
            "code": "BAD_SCOPE",
            "message": f"scope 须为 {sorted(svc.SCOPE_VALUES)}",
        })
    if rule_type not in svc.RULE_TYPE_VALUES:
        raise HTTPException(400, {
            "code": "BAD_TYPE",
            "message": f"rule_type 须为 {sorted(svc.RULE_TYPE_VALUES)}",
        })
    if scope != "global" and not scope_ref:
        raise HTTPException(400, {
            "code": "NEED_REF",
            "message": "非 global 作用域必须提供 scope_ref（域名/用户 id/项目 id）",
        })


def _audit(action: str, user: CurrentUser, rule_key: str) -> None:
    logger.info(
        "[system_rules_admin] action=%s user=%s(%d) rule_key=%s",
        action, getattr(user, "account", "?"), getattr(user, "id", 0), rule_key,
    )


# ── 只读：列表 / 详情 ────────────────────────────────────────────────────────
@router.get("")
async def api_list_rules(
    scope: str | None = Query(None),
    rule_type: str | None = Query(None),
    is_active: bool | None = Query(None),
    q: str | None = Query(None, alias="q"),
    _: CurrentUser = Depends(require_super_admin),
):
    """列出系统规则。列表展示向量摘要(summary)，详情再取 MySQL 原文(content)。"""
    return await svc.list_rules(scope=scope, rule_type=rule_type, is_active=is_active, keyword=q)


@router.get("/{rule_key:path}")
async def api_get_rule(rule_key: str, _: CurrentUser = Depends(require_super_admin)):
    """单条规则详情（含 content 全文）。"""
    r = await svc.get_rule(rule_key)
    if r is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "规则不存在"})
    return r


# ── 写：新增 / 修改 / 删除 / 重建 ────────────────────────────────────────────
@router.post("")
async def api_create_rule(body: RuleCreate, user: CurrentUser = Depends(require_super_admin)):
    _validate(body.rule_key, body.scope, body.rule_type, body.scope_ref)
    try:
        r = await svc.create_rule(body.model_dump())
    except ValueError as e:
        raise HTTPException(409, {"code": "DUPLICATE", "message": str(e)}) from e
    _audit("create", user, body.rule_key)
    return r


@router.put("/{rule_key:path}")
async def api_update_rule(
    rule_key: str, body: RuleUpdate, user: CurrentUser = Depends(require_super_admin),
):
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, {"code": "EMPTY", "message": "未提供任何更新字段"})
    # 若更新了 scope/rule_type，按新值做枚举校验（scope_ref 联动）。
    new_scope = data.get("scope", None)
    new_ref = data.get("scope_ref", None)
    new_type = data.get("rule_type", None)
    if new_scope is not None and new_scope not in svc.SCOPE_VALUES:
        raise HTTPException(400, {"code": "BAD_SCOPE", "message": f"scope 须为 {sorted(svc.SCOPE_VALUES)}"})
    if new_type is not None and new_type not in svc.RULE_TYPE_VALUES:
        raise HTTPException(400, {"code": "BAD_TYPE", "message": f"rule_type 须为 {sorted(svc.RULE_TYPE_VALUES)}"})
    if (new_scope is not None and new_scope != "global") and not new_ref and new_ref is not None:
        # 仅当显式把 scope 改为非 global 且显式置空 scope_ref 时才拦；未动 scope_ref 则不拦。
        raise HTTPException(400, {"code": "NEED_REF", "message": "非 global 作用域必须提供 scope_ref"})
    r = await svc.update_rule(rule_key, data)
    if r is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "规则不存在"})
    _audit("update", user, rule_key)
    return r


@router.delete("/{rule_key:path}")
async def api_delete_rule(rule_key: str, user: CurrentUser = Depends(require_super_admin)):
    ok = await svc.delete_rule(rule_key)
    if not ok:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "规则不存在"})
    _audit("delete", user, rule_key)
    return {"ok": True}


@router.post("/reindex")
async def api_reindex(user: CurrentUser = Depends(require_super_admin)):
    """用全部活跃 MySQL 行重建向量索引（clear + 批量 upsert）。fix 向量/MySQL 漂移用。"""
    n = await svc.reindex_rules()
    _audit("reindex", user, "")
    return {"ok": True, "reindexed": n}
