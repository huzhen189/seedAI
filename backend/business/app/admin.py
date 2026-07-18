"""管理监控路由(§3.6):仅 admin。实时指标 SSE + 手动控制面占位。"""

import asyncio
import json

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from .metrics import snapshot
from .security import require_admin


router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/metrics")
async def metrics_stream(_=Depends(require_admin)):
    """实时指标:每 2s 推一帧快照。"""

    async def publisher():
        while True:
            yield {"event": "metrics", "data": json.dumps(await snapshot())}
            await asyncio.sleep(2)

    return EventSourceResponse(publisher())


@router.post("/scale")
async def scale_service(name: str, replicas: int, _=Depends(require_admin)):
    """手动扩缩容占位(M0 仅返回意图;M1 接 DockerComposeOrchestrator/K8s)。"""
    return {"ack": True, "service": name, "target_replicas": replicas, "note": "M0 stub"}


@router.post("/stop")
async def stop_service(name: str, _=Depends(require_admin)):
    """手动停止占位。"""
    return {"ack": True, "service": name, "note": "M0 stub"}
