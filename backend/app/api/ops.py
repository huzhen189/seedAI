"""运维可观测性端点（§15.2 可观测性）。

- GET /readyz          是否接收新 Turn（mysql+redis 正常且非全熔断）。
- GET /ops/status      受 admin 鉴权的依赖/熔断/SLO 快照（含关键积压）。
- GET /metrics         Prometheus 文本格式基础指标。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from app.config import settings
from app.cache import get_redis
from app.db import engine
from app.security import CurrentUser, require_admin
from app.security_alerting import AlertManager
from app.security_circuit import CircuitState, get_registry
from app.security_slo import evaluate, summarize

logger = logging.getLogger("app.api.ops")

# 跨请求保持告警生命周期状态（持续 5min 触发 / 5min 解除）。
_alert_manager = AlertManager()

router = APIRouter(tags=["ops"])

_START_TIME = time.monotonic()


async def _mysql_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - 依赖健康探测
        logger.warning("mysql 健康检查失败: %s", exc)
        return False


async def _redis_ok() -> bool:
    try:
        r = await get_redis()
        return bool(await r.ping())
    except Exception as exc:  # pragma: no cover
        logger.warning("redis 健康检查失败: %s", exc)
        return False


async def _chroma_ok() -> bool:
    try:
        from chromadb import HttpClient as ChromaHttpClient

        parsed = urlparse(settings.chroma_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        client = ChromaHttpClient(
            host=parsed.hostname,
            port=parsed.port or (443 if parsed.scheme == "https" else 8000),
            ssl=parsed.scheme == "https",
        )
        client.heartbeat()
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("chroma 健康检查失败: %s", exc)
        return False


@router.get("/readyz")
async def readyz() -> Response:
    """是否接收新 Turn：硬依赖 mysql+redis 正常，且非全部 provider 熔断。"""
    checks: dict[str, bool] = {}
    checks["mysql"] = await _mysql_ok()
    checks["redis"] = await _redis_ok()
    breakers = get_registry().snapshot_all()
    providers = [b["name"] for b in breakers] or ["qwen", "deepseek"]
    all_open = bool(breakers) and all(b["state"] == CircuitState.OPEN.value for b in breakers)
    checks["circuit_breakers"] = not all_open
    ready = all(checks.values())
    payload = {
        "status": "ready" if ready else "not_ready",
        "ts": int(time.time()),
        "checks": checks,
    }
    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        status_code=200 if ready else 503,
    )


@router.get("/ops/status")
async def ops_status(_admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    """受 admin 鉴权的运维快照：依赖、熔断、积压、SLO。"""
    mysql_ok = await _mysql_ok()
    redis_ok = await _redis_ok()
    chroma_ok = await _chroma_ok()
    breakers = get_registry().snapshot_all()

    # 关键积压：Redis 持久队列长度（若启用）。
    redis_backlog = None
    try:
        r = await get_redis()
        # ai: 前缀下的 list/zset 数量作为粗略积压指标（不展开大 key）。
        redis_backlog = await r.dbsize()
    except Exception:  # pragma: no cover
        redis_backlog = None

    return {
        "service": "app",
        "env": settings.env,
        "ts": int(time.time()),
        "uptime_seconds": round(time.monotonic() - _START_TIME, 1),
        "dependencies": {
            "mysql": "ok" if mysql_ok else "fail",
            "redis": "ok" if redis_ok else "fail",
            "chroma": "ok" if chroma_ok else "fail",
        },
        "circuit_breakers": breakers,
        "redis_backlog_keys": redis_backlog,
        "slo": {"note": "详见 M10c SLO 评估端点 /ops/slo（本轮后续接入）"},
    }


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus 文本格式基础指标。"""
    mysql_ok = await _mysql_ok()
    redis_ok = await _redis_ok()
    lines: list[str] = []
    lines.append("# HELP seedai_up 服务存活(1)。")
    lines.append("# TYPE seedai_up gauge")
    lines.append("seedai_up 1")
    lines.append("# HELP seedai_uptime_seconds 进程启动后秒数。")
    lines.append("# TYPE seedai_uptime_seconds gauge")
    lines.append(f"seedai_uptime_seconds {round(time.monotonic() - _START_TIME, 1)}")
    for dep, ok in (("mysql", mysql_ok), ("redis", redis_ok)):
        lines.append(f'# TYPE seedai_dependency_up{{dep="{dep}"}} gauge')
        lines.append(f'seedai_dependency_up{{dep="{dep}"}} {1 if ok else 0}')
    state_code = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 1, CircuitState.OPEN: 2}
    for b in get_registry().snapshot_all():
        code = state_code.get(CircuitState(b["state"]), 0)
        lines.append(f'# TYPE seedai_circuit_breaker_state{{provider="{b["name"]}"}} gauge')
        lines.append(f'seedai_circuit_breaker_state{{provider="{b["name"]}"}} {code}')
        lines.append(
            f'# TYPE seedai_circuit_breaker_failures{{provider="{b["name"]}"}} gauge'
        )
        lines.append(
            f'seedai_circuit_breaker_failures{{provider="{b["name"]}"}} {b["total_failures"]}'
        )
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


async def _build_slo_snapshot() -> dict[str, float | None]:
    """组装 SLO 测量快照；可测量的取真实值，不可测量的标 unknown(None)。

    当前仅对 API 延迟做尽力而为的探测；回放成功率/W0 伪成功/部署不破坏等
    需要 Outbox/Deployment 对账数据，暂无实时测量路径时返回 unknown，避免误报。
    """
    snapshot: dict[str, float | None] = {}
    try:
        from app import analytics

        snap = await analytics.analytics_snapshot()
        api = snap.get("api_latency", {})
        p99 = api.get("p99")
        snapshot["api_p99_ms"] = float(p99) if p99 is not None else None
    except Exception as exc:  # pragma: no cover
        logger.warning("SLO 快照组装失败(api 延迟): %s", exc)
        snapshot["api_p99_ms"] = None
    # 其余 SLO 暂无实时测量路径 → unknown（不误报）。
    snapshot.setdefault("sse_first_event_p99_ms", None)
    snapshot.setdefault("replay_success_rate", None)
    snapshot.setdefault("deploy_no_break", None)
    snapshot.setdefault("w0_pseudo_success", None)
    return snapshot


@router.get("/ops/slo")
async def ops_slo(_admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    """受 admin 鉴权的 SLO 评估与告警状态（§15.2）。"""
    snapshot = await _build_slo_snapshot()
    results = evaluate(snapshot)
    events = _alert_manager.update(results)
    return {
        "ts": int(time.time()),
        "summary": summarize(results),
        "results": [r.__dict__ for r in results],
        "active_alerts": _alert_manager.active_alerts(),
        "recent_events": [e.to_dict() for e in events],
    }


__all__ = ["router"]
