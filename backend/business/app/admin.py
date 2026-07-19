"""管理监控路由(§3.6 / §3 RBAC 三级)。

权限分层:
  - 只读后台(指标 / 用户列表):`require_admin`(super_admin 或 admin 均可进);
  - 控制面(启停 / 扩缩容)与用户 / 角色管理:`require_super_admin`(仅超管)。

管理页作为 Vue 内 `/admin` 路由,与用户前台共享登录态、彼此隔离(前端 §10)。
admin 进入后控制面板置灰 / 隐藏,仅 super_admin 可见可执行。
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .metrics import snapshot
from .models import User
from .orchestrator import run_scale, run_start, run_stop
from .schemas import AdminUserResp, SetPlanReq, SetRoleReq
from .security import (
    CurrentUser,
    ROLE_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_USER,
    require_admin,
    require_super_admin,
)


router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/metrics")
async def metrics_stream(_=Depends(require_admin)):
    """实时指标:每 2s 推一帧快照(轮询兜底见前端)。"""

    async def publisher():
        while True:
            yield {"event": "metrics", "data": json.dumps(await snapshot())}
            await asyncio.sleep(2)

    from sse_starlette.sse import EventSourceResponse

    return EventSourceResponse(publisher())


@router.get("/users", response_model=list[AdminUserResp])
async def list_users(
    _=Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(200, ge=1, le=500),
):
    """用户列表(仅超管)。按注册时间倒序。"""
    rows = (
        (await db.execute(select(User).order_by(User.id.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return rows


@router.post("/users/{user_id}/role", response_model=AdminUserResp)
async def set_user_role(
    user_id: int,
    req: SetRoleReq,
    admin: CurrentUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """变更用户角色(仅超管)。

    安全约束:
      - 不允许把任何 super_admin 降级(防锁死控制台);
      - 不允许把目标改成与调用者冲突的越权角色(本接口已要求 super_admin,故只拦自降)。
    """
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    # 任何 super_admin 都不允许被降级(含调用者自己),避免控制台被锁死。
    if target.role == ROLE_SUPER_ADMIN and req.role != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="super_admin 不可被降级")
    if target.id == admin.id and req.role != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="不能取消自己的超管角色")
    target.role = req.role
    await db.commit()
    await db.refresh(target)
    return target


@router.post("/users/{user_id}/plan", response_model=AdminUserResp)
async def set_user_plan(
    user_id: int,
    req: SetPlanReq,
    _=Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """变更用户套餐(仅超管;收费预留,当前仅改 plan 字段)。"""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    target.plan = req.plan
    await db.commit()
    await db.refresh(target)
    return target


@router.post("/scale")
async def scale_service(
    name: str,
    replicas: int,
    _=Depends(require_super_admin),
):
    """手动扩缩容(⑥-b):真实调用 `docker compose up -d --scale`,返回执行日志。"""
    result = await run_scale(name, replicas)
    return {"ack": True, "service": name, "target_replicas": replicas, **result}


@router.post("/stop")
async def stop_service(name: str, _=Depends(require_super_admin)):
    """手动停止(⑥-b):真实调用 `docker compose stop`,返回执行日志。"""
    result = await run_stop(name)
    return {"ack": True, "service": name, **result}


@router.post("/start")
async def start_service(name: str, _=Depends(require_super_admin)):
    """手动启动(⑥-b 补充):真实调用 `docker compose start`,返回执行日志。"""
    result = await run_start(name)
    return {"ack": True, "service": name, **result}


# 保留角色常量导出(供其他模块引用,避免散落字符串)
__all__ = [
    "router",
    "ROLE_USER",
    "ROLE_ADMIN",
    "ROLE_SUPER_ADMIN",
]
