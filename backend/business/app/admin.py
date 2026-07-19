"""管理监控路由(§3.6 / §3 RBAC 三级)。

权限分层:
  - 只读后台(指标 / 用户列表):`require_admin`(super_admin 或 admin 均可进);
  - 控制面(启停 / 扩缩容)与用户 / 角色管理:`require_super_admin`(仅超管)。

管理页作为 Vue 内 `/admin` 路由,与用户前台共享登录态、彼此隔离(前端 §10)。
admin 进入后控制面板置灰 / 隐藏,仅 super_admin 可见可执行。
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .db import reset_db as do_reset_db, schedule_biz_restart
from .metrics import snapshot
from .models import Feedback, Trace, TraceEvent, UsageLog, User
from .orchestrator import run_scale, run_start, run_stop
from .schemas import AdminUserResp, SetPlanReq, SetRoleReq
from .security import (
    ROLE_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_USER,
    CurrentUser,
    require_admin,
    require_super_admin,
)


router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/health")
async def health():
    """三库连通性健康检查(MySQL + Redis;Chroma 由 AI 服务托管,此处不检查)。"""
    from .metrics import _db_status

    return await _db_status()


    from .metrics import snapshot as metrics_snapshot


@router.get("/analytics")
async def analytics(_=Depends(require_admin)):
    """全量分析看板:意图命中率/Skill成效/API延迟/前端性能/生成阶段耗时。"""
    from .analytics import analytics_snapshot
    return await analytics_snapshot()


@router.post("/analytics/perf")
async def report_frontend_perf(request: Request):
    """客户端上报前端性能(不计鉴权,轻量上报)。"""
    from .analytics import record_frontend_perf
    try:
        body = await request.json()
        for metric in ("page_load", "ttfb", "dom_ready"):
            val = body.get(metric)
            if isinstance(val, (int, float)) and val > 0:
                await record_frontend_perf(metric, float(val))
        return {"ack": True}
    except Exception:
        return {"ack": False}


# ---------- 指标 SSE ----------
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


@router.post("/reset")
async def reset_system(confirm: str = Query(""), _=Depends(require_super_admin)):
    """全量重置系统(超管): 清空数据库+Redis → 重建表 → 种子用户。

    前端调用前应先清理本地数据(localStorage/sessionStorage/IndexedDB)。
    返回后需手动重启两个后端服务。
    """
    if confirm != "yes":
        raise HTTPException(400, detail="请在 query 中传 confirm=yes 以确认")
    try:
        result = await do_reset_db()
        # 数据已清理, 调度业务服务自动重启(7102 由用户手动重启)
        schedule_biz_restart()
        return result
    except Exception as e:
        logger.exception("reset_db 失败")
        return {"success": False, "error": str(e)}


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


# ---------- 对话追踪 / 回放 / 质量(③-a · 文档 §3.13) ----------
@router.get("/traces")
async def list_traces(
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
):
    """Trace 列表(倒序),供管理后台回放入口。"""
    rows = (
        (await db.execute(select(Trace).order_by(Trace.id.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return [
        {
            "id": t.id,
            "trace_id": t.trace_id,
            "user_id": t.user_id,
            "model_id": t.model_id,
            "status": t.status,
            "total_tokens": t.total_tokens,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        }
        for t in rows
    ]


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """单条 Trace + 事件序列(供前端回放重建时间线)。"""
    t = (
        await db.execute(select(Trace).where(Trace.trace_id == trace_id))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="trace not found")
    events = (
        (
            await db.execute(
                select(TraceEvent)
                .where(TraceEvent.trace_id == trace_id)
                .order_by(TraceEvent.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "trace": {
            "id": t.id,
            "trace_id": t.trace_id,
            "user_id": t.user_id,
            "model_id": t.model_id,
            "status": t.status,
            "total_tokens": t.total_tokens,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        },
        "events": [
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "stage": e.stage,
                "payload": json.loads(e.payload) if e.payload else None,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


@router.get("/quality")
async def quality(_=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """AI 质量聚合指标(③-a / 文档 §3.12 6+1 维度精简版)。"""
    fbs = (await db.execute(select(Feedback))).scalars().all()
    ratings = [f.rating for f in fbs]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    dist: dict[int, int] = {}
    for r in ratings:
        dist[r] = dist.get(r, 0) + 1

    usages = (await db.execute(select(UsageLog))).scalars().all()
    model_usage: dict[str, int] = {}
    for u in usages:
        key = u.model or "unknown"
        model_usage[key] = model_usage.get(key, 0) + 1

    rev_events = (
        (
            await db.execute(
                select(TraceEvent).where(
                    TraceEvent.event_type == "think", TraceEvent.stage == "reviewer"
                )
            )
        )
        .scalars()
        .all()
    )
    passed = 0
    for e in rev_events:
        try:
            p = json.loads(e.payload) if e.payload else {}
            if p.get("passed") is True:
                passed += 1
        except Exception:
            pass

    traces = (await db.execute(select(Trace))).scalars().all()
    total = len(traces)
    done = sum(1 for t in traces if t.status == "done")

    from .cache import get_redis
    try:
        r = await get_redis()
        unsupported_total = int((await r.get("stats:unsupported:total")) or 0)
        samples_raw = await r.lrange("stats:unsupported_samples", 0, 19)
        samples = []
        for s in samples_raw:
            try:
                samples.append(json.loads(s))
            except Exception:
                pass
    except Exception:
        unsupported_total = 0
        samples = []

    return {
        "feedback_count": len(ratings),
        "avg_rating": avg,
        "rating_distribution": dist,
        "model_usage": model_usage,
        "reviewer_pass_rate": round(passed / max(len(rev_events), 1), 3),
        "reviewer_total": len(rev_events),
        "generation_total": total,
        "generation_success_rate": round(done / max(total, 1), 3),
        "unsupported_count": unsupported_total,
        "unsupported_samples": samples,
    }


# 保留角色常量导出(供其他模块引用,避免散落字符串)
__all__ = [
    "router",
    "ROLE_USER",
    "ROLE_ADMIN",
    "ROLE_SUPER_ADMIN",
]
