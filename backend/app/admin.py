"""管理监控路由(§3.6 / §3 RBAC 三级)。

权限分层:
  - 只读后台(指标 / 用户列表):`require_admin`(super_admin 或 admin 均可进);
  - 控制面(启停 / 扩缩容)与用户 / 角色管理:`require_super_admin`(仅超管)。

管理页作为 Vue 内 `/admin` 路由,与用户前台共享登录态、彼此隔离(前端 §10)。
admin 进入后控制面板置灰 / 隐藏,仅 super_admin 可见可执行。
"""
import logging
from typing import List

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .metrics import snapshot
# v3 remap(2026-08-02): 旧的 Trace/UsageLog 模型在 v3 重构时已删除 → 按 v3 真实数据源替换:
#   - 生成/回放  → Turn(turns 表, 含 trace_id/status)
#   - 模型用量   → ModelCall(model_calls 表, 含 model/tokens/cost)
# 2026-08-02 补回: TraceEvent(阶段链路) / Feedback(用户评价) 在 v3 模型包中重建,
# 回放详情因此能还原 S0-S9 完整链路与用户评分, 不再降级为空。
from .models import Conversation, Feedback, Message, QcScore, TraceEvent, Turn, User, ModelCall

# 6 维度(与 backend/ai_service/app/qc.py 保持一致, 供雷达图轴序)
QC_DIMENSIONS = ["correctness", "completeness", "compliance", "efficiency", "readability", "safety"]
# v2.3.0 单裁判: 不再固定 3 裁判; 实际出现的 model 由报表运行时收集(qc_models)。
QC_JUDGES: List[str] = []
QC_DIM_LABELS = {
    "correctness": "正确性", "completeness": "完整性", "compliance": "合规性",
    "efficiency": "效率", "readability": "可读性", "safety": "安全性",
}
from .deployment import run_scale, run_start, run_stop
from .schemas import AdminUserResp, SetRoleReq, SetTierReq
from .security import (
    CurrentUser,
    require_admin,
    require_super_admin,
)

logger = logging.getLogger("business.admin")


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


@router.post("/analytics/track")
async def track_frontend(request: Request):
    """客户端上报前端访问 / 点击事件(STAT-3,不计鉴权,轻量上报)。

    支持两种上报体:
      - {"type": "page_view", "route": "/chat"}
      - {"type": "click", "label": "发送"}
    兼容 sendBeacon(application/json 或 text/plain)。"""
    from .analytics import record_frontend_access, record_frontend_click

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        try:
            import json as _json

            raw = (await request.body()).decode("utf-8", "ignore")
            if raw:
                body = _json.loads(raw)
        except Exception:
            return {"ack": False}
    try:
        t = body.get("type")
        if t == "page_view":
            # uid: 前端匿名访客 ID, 用于 UV 统计(R6)
            await record_frontend_access(body.get("route") or "unknown", body.get("uid"))
        elif t == "click":
            label = (body.get("label") or "").strip()
            if label:
                await record_frontend_click(label[:60])
        return {"ack": True}
    except Exception:
        return {"ack": False}


# ---------- 指标 SSE ----------
@router.get("/metrics")
async def metrics_stream(_=Depends(require_admin)):
    """实时指标:每 5s 推一帧快照(轮询兜底见前端)。"""

    async def publisher():
        while True:
            yield {"event": "metrics", "data": json.dumps(await snapshot())}
            await asyncio.sleep(5)

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
    if target.role == "super_admin" and req.role != "super_admin":
        raise HTTPException(status_code=400, detail="super_admin 不可被降级")
    if target.id == admin.id and req.role != "super_admin":
        raise HTTPException(status_code=400, detail="不能取消自己的超管角色")
    target.role = req.role
    await db.commit()
    await db.refresh(target)
    return target


@router.post("/users/{user_id}/tier", response_model=AdminUserResp)
async def set_user_tier(
    user_id: int,
    req: SetTierReq,
    _=Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """变更用户套餐等级(仅超管;v3 以 tier 枚举 free/pro/max 取代旧 plan 字段)。"""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    target.tier = req.tier
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
    """全量重置系统(超管)。

    架构说明(v2.0.0 单进程):旧的 `app.db.reset_db` 在 M11c 重构后已废弃——
    新架构重置=手工建空库 + 改 .env + 启动 create_all(不建超管),不存在配套的
    运行时 "DROP+重建" 脚本。因此此处**只清 Redis**(统计/缓存/队列态,这部分
    确实可由代码安全回收),MySQL 表数据清理由超管在数据库侧按需求手工执行。

    前端调用前应先清理本地数据(localStorage/sessionStorage/IndexedDB)。
    """
    if confirm != "yes":
        raise HTTPException(400, detail="请在 query 中传 confirm=yes 以确认")
    try:
        from .cache import get_redis

        r = await get_redis()
        # 扫描并删除本服务命名空间下的所有键(ai:/an:/cache:/stats:/queue: 等)。
        # 用 SCAN 游标迭代,避免 KEYS 在大键空间阻塞。
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await r.scan(cursor, count=500)
            if keys:
                await r.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        return {
            "success": True,
            "message": "已清空全部 Redis 键(统计/缓存/队列)。MySQL 表数据请于数据库侧按需求手工清空,随后手动重启单进程后端(7101)刷新页面重新登录。",
            "tables_dropped": 0,
            "redis_cleared": True,
            "redis_keys_deleted": deleted,
        }
    except Exception as e:
        logger.exception("reset 失败")
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
    user_id: int | None = Query(None, description="按 user_id 过滤"),
    project_id: int | None = Query(None, description="按 project_id 过滤"),
    conversation_id: int | None = Query(None, description="按 conversation_id 过滤"),
    trace_id: str | None = Query(None, description="按 trace_id(或 turn_id)精确/前缀过滤"),
):
    """生成/回放会话列表(倒序),供管理后台回放入口。

    v3 remap: 旧 Trace 模型 → Turn(turns 表), 保留 trace_id 关联 QC 与用户消息。
    Turn 无 model_id/total_tokens/started/finished 字段, 相关列降级为 None。
    补全回放字段: project_id(经 conversation 反查) / conversation_id / turn_id(== trace_id) / 时间列。
    """
    stmt = select(Turn)
    if user_id is not None:
        stmt = stmt.where(Turn.user_id == user_id)
    if conversation_id is not None:
        stmt = stmt.where(Turn.conversation_id == conversation_id)
    if trace_id:
        # trace_id 与 turn_id 同源(= 26 位), 支持精确或前缀匹配。
        stmt = stmt.where(Turn.trace_id.like(f"{trace_id}%"))
    stmt = stmt.order_by(Turn.id.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    # project_id 反查: Turn.conversation_id → Conversation.project_id(批量查, 避免 N 查询)。
    proj_by_conv: dict[int, int] = {}
    conv_ids = list({t.conversation_id for t in rows if t.conversation_id is not None})
    if conv_ids:
        crows = (
            (await db.execute(select(Conversation.id, Conversation.project_id).where(Conversation.id.in_(conv_ids))))
            .tuples()
            .all()
        )
        proj_by_conv = {c.id: c.project_id for c in crows}
    if project_id is not None:
        rows = [t for t in rows if proj_by_conv.get(t.conversation_id) == project_id]

    # 关联 QC 整体分(供列表快速预览)
    qc_rows = (await db.execute(select(QcScore))).scalars().all()
    qc_by_trace: dict[str, float] = {q.trace_id: q.overall for q in qc_rows}
    # 关联用户评价(1-10 分),让列表一眼看出哪些轮次被用户打过分。
    fb_rows = (await db.execute(select(Feedback))).scalars().all()
    fb_by_trace: dict[str, int] = {f.trace_id: f.rating for f in fb_rows}
    # 查每条 turn 对应的第一条用户消息(供列表预览,限20字)。
    # 必须按 trace_id 关联,而非 conversation_id —— 否则同一会话多条 trace 全部显示第一条(历史 bug)。
    from sqlalchemy import text as sa_text
    user_inputs = {}
    trace_ids = list(set(t.trace_id for t in rows if t.trace_id))
    if trace_ids:
        try:
            placeholders = ",".join([f":t{i}" for i in range(len(trace_ids))])
            params = {f"t{i}": tid for i, tid in enumerate(trace_ids)}
            mrows = (await db.execute(
                sa_text(
                    f"SELECT m.trace_id, m.content FROM messages m "
                    f"JOIN (SELECT trace_id, MIN(id) AS min_id FROM messages "
                    f"WHERE trace_id IN ({placeholders}) AND role='user' "
                    f"GROUP BY trace_id) sub "
                    f"ON m.trace_id = sub.trace_id AND m.id = sub.min_id"
                ), params,
            )).fetchall()
            for tid, content in mrows:
                user_inputs[tid] = (content or "")[:20] + ("..." if len(content or "") > 20 else "")
        except Exception:
            pass

    return [
        {
            "id": t.id,
            "trace_id": t.trace_id,
            "turn_id": t.trace_id,
            "user_id": t.user_id,
            "conversation_id": t.conversation_id,
            "project_id": proj_by_conv.get(t.conversation_id),
            "user_input": user_inputs.get(t.trace_id, "") if t.trace_id else "",
            "model_id": None,
            "status": t.status,
            "total_tokens": None,
            "qc_overall": qc_by_trace.get(t.trace_id),
            "feedback_rating": fb_by_trace.get(t.trace_id) if t.trace_id else None,
            "started_at": t.created_at.isoformat() if t.created_at else None,
            "finished_at": None,
        }
        for t in rows
    ]


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    _=Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """单条生成会话 + 内容 + 完整处理链路(供前端回放)。

    数据源:
      - trace     → Turn(turns)
      - messages  → Message(按 conversation_id + trace_id)
      - events    → TraceEvent(trace_events): turn_start / S0..S9 阶段 IO / turn_end
      - feedback  → Feedback(feedbacks): 用户 1-10 分 + 六维细分 + 评语
      - qc        → QcScore(qc_scores)
    events 由 app.core.audit.DbAuditSink 在每轮 Turn 收尾时批量写入。
    """
    t = (
        await db.execute(select(Turn).where(Turn.trace_id == trace_id))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="trace not found")
    # 关联 QC 结果 + 实际对话内容(供复盘详情)
    qc = (
        await db.execute(select(QcScore).where(QcScore.trace_id == trace_id))
    ).scalar_one_or_none()
    msgs = []
    if t.conversation_id is not None:
        mrows = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == t.conversation_id,
                       Message.trace_id == trace_id)
                .order_by(Message.id.asc())
            )
        ).scalars().all()
        for m in mrows:
            content = m.content or ""
            # 结构化(建站产物)内容仅截摘要, 避免详情过大
            if content.startswith("{") and len(content) > 600:
                try:
                    obj = json.loads(content)
                    if isinstance(obj, dict) and obj.get("type") in ("site", "code"):
                        content = f"[结构化产物:{obj.get('title', '')}]"
                except Exception:
                    content = content[:600] + "…"
            msgs.append({
                "role": m.role,
                "model_id": m.model_slot,
                "content": content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            })
    # ── 完整链路事件(S0-S9 每节点 IN/OUT/changed) ────────────────────────────
    ev_rows = (
        await db.execute(
            select(TraceEvent)
            .where(TraceEvent.trace_id == trace_id)
            .order_by(TraceEvent.seq.asc())
            .limit(500)
        )
    ).scalars().all()
    events = []
    for e in ev_rows:
        try:
            payload = json.loads(e.payload) if e.payload else {}
        except Exception:
            payload = {"_raw": (e.payload or "")[:2000]}
        events.append({
            "seq": e.seq,
            "event_type": e.event_type,
            "stage": e.stage,
            "payload": payload,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })
    # ── 用户评价 ────────────────────────────────────────────────────────────
    fb = (
        await db.execute(select(Feedback).where(Feedback.trace_id == trace_id))
    ).scalar_one_or_none()
    # 回放补全字段: project_id(经 conversation 反查) + 结束时间(取消息/事件最新 created_at)。
    project_id = None
    if t.conversation_id is not None:
        conv = await db.get(Conversation, t.conversation_id)
        project_id = conv.project_id if conv else None
    cands = [m["created_at"] for m in msgs if m.get("created_at")] + [
        e["created_at"] for e in events if e.get("created_at")
    ]
    finished_at = max(cands) if cands else (t.created_at.isoformat() if t.created_at else None)
    return {
        "trace": {
            "id": t.id,
            "trace_id": t.trace_id,
            "turn_id": t.trace_id,
            "user_id": t.user_id,
            "conversation_id": t.conversation_id,
            "project_id": project_id,
            "model_id": None,
            "status": t.status,
            "total_tokens": None,
            "started_at": t.created_at.isoformat() if t.created_at else None,
            "finished_at": finished_at,
        },
        "qc": (
            {
                "overall": qc.overall,
                "result": qc.result,
                "needs_review": qc.needs_review,
                "safety_risk": qc.safety_risk,
                "partial": qc.partial,
                "created_at": qc.created_at.isoformat() if qc.created_at else None,
            }
            if qc is not None else None
        ),
        "feedback": (
            {
                "rating": fb.rating,
                "comment": fb.comment,
                "dimensions": fb.dimensions,
                "created_at": fb.created_at.isoformat() if fb.created_at else None,
            }
            if fb is not None else None
        ),
        "messages": msgs,
        "events": events,
    }


@router.get("/quality")
async def quality(_=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """AI 质量聚合指标(③-a / 文档 §3.12 6+1 维度精简版)。"""
    # ── 用户反馈评分(v3: 走 Redis P_FEEDBACK, 由 analytics.record_feedback 写入) ──
    from .analytics import P_FEEDBACK
    from .cache import get_redis
    try:
        r = await get_redis()
        fb_count = int((await r.hget(P_FEEDBACK, "count")) or 0)
        fb_rating_sum = int((await r.hget(P_FEEDBACK, "rating_sum")) or 0)
        fb_rating_cnt = int((await r.hget(P_FEEDBACK, "rating_count")) or 0)
        avg = round(fb_rating_sum / fb_rating_cnt, 2) if fb_rating_cnt else None
    except Exception:
        fb_count = 0
        avg = None

    # ── 模型用量(v3: model_calls 表, 含 model/tokens/cost/error) ──
    mc_rows = (await db.execute(select(ModelCall))).scalars().all()
    model_usage: dict[str, dict] = {}
    for m in mc_rows:
        key = m.model or "unknown"
        agg = model_usage.setdefault(key, {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "errors": 0, "cost_usd": 0.0,
        })
        agg["calls"] += 1
        agg["prompt_tokens"] += int(m.prompt_tokens or 0)
        agg["completion_tokens"] += int(m.completion_tokens or 0)
        agg["errors"] += 1 if m.error_code else 0
        try:
            agg["cost_usd"] = round(float(agg["cost_usd"]) + float(m.cost_usd or 0), 6)
        except Exception:
            pass

    # ── 生成成功率(v3: turns 表, completed/total) ──
    turns = (await db.execute(select(Turn))).scalars().all()
    total = len(turns)
    done = sum(1 for t in turns if t.status == "completed")

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

    # ── QC 单裁判聚合(v2.3.0) ──
    # 新 schema: QcScore.result = {"scores": {dim: int(0-100)}, "overall": float(0-10), ...}
    # 维度键与 backend/app/llm/extract.py 的 qc.scores 严格对齐(correctness/completeness/...)。
    # 旧 schema(result.dimensions[d].mean / result.judges) 已废弃, 不再读取。
    qc_rows = (await db.execute(select(QcScore))).scalars().all()
    qc_count = len(qc_rows)
    qc_overall_dim: dict[str, list] = {d: [] for d in QC_DIMENSIONS}
    qc_overall_list: list[float] = []
    for q in qc_rows:
        res = q.result or {}
        if not isinstance(res, dict):
            continue
        if res.get("overall") is not None:
            qc_overall_list.append(float(res.get("overall", 0)))
        scores = res.get("scores") or {}
        if isinstance(scores, dict):
            for d in QC_DIMENSIONS:
                v = scores.get(d)
                if isinstance(v, (int, float)) and v > 0:
                    qc_overall_dim[d].append(float(v))
    qc_overall_dim_avg = {
        d: round(sum(v) / len(v), 2) if v else 0.0 for d, v in qc_overall_dim.items()
    }
    qc_overall_avg = round(sum(qc_overall_list) / len(qc_overall_list), 2) if qc_overall_list else None
    # 需复核占比
    qc_review_list = [1 for q in qc_rows if q.needs_review]
    qc_review_rate = round(len(qc_review_list) / max(qc_count, 1), 3)

    return {
        "feedback_count": fb_count,
        "avg_rating": avg,
        "rating_distribution": {},
        "model_usage": model_usage,
        # v3: 无 TraceEvent 事件流, reviewer 通过率改由 QC needs_review 反推
        "reviewer_pass_rate": round((qc_count - len(qc_review_list)) / max(qc_count, 1), 3),
        "reviewer_total": qc_count,
        "generation_total": total,
        "generation_success_rate": round(done / max(total, 1), 3),
        "unsupported_count": unsupported_total,
        "unsupported_samples": samples,
        # QC 单裁判聚合(v2.3.0)
        "qc_count": qc_count,
        "qc_overall_avg": qc_overall_avg,
        "qc_overall_dim_avg": qc_overall_dim_avg,   # 整体每维均值(雷达图基线序列)
        "qc_model_avg": {},                          # 预留: 单裁判下无多模型序列
        "qc_review_rate": qc_review_rate,           # 需人工复核占比
        "qc_dimensions": QC_DIMENSIONS,
        "qc_dim_labels": QC_DIM_LABELS,
        "qc_judges": [],                             # 预留: 实际出现的 QC 模型(前端雷达图序列名)
    }


# router 导出(角色常量已在 v3 改为 security 的字符串 role 判定,此处不再导出 ROLE_*)
__all__ = [
    "router",
]
