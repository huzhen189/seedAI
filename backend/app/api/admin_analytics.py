"""管理后台遥测吸入端点。

前端 composables/track.ts 与 usePerf.ts 通过 sendBeacon / fetch 上报埋点与性能数据,
路径为 /admin/analytics/track 与 /admin/analytics/perf。这些是尽力而为的遥测, 不要求鉴权,
仅接收并返回 204, 避免前端控制台出现 404/401 噪音。后续若要做真实聚合, 在此接 analytics.py。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

router = APIRouter(prefix="/admin", tags=["admin-analytics"])

logger = __import__("logging").getLogger("app.api.admin_analytics")


@router.post("/analytics/track")
async def track(request: Request) -> Response:
    # 不消费 body(telemetry 尽力而为), 直接确认。如需落盘可在此解析 JSON 后写入 analytics。
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/analytics/perf")
async def perf(request: Request) -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)
