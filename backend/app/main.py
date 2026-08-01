"""统一单进程应用入口(业务层 + 十阶段推理链路合并)。

合并说明(用户指令: ai_service 与 business 合并为一个项目, 全量重写):
- 业务层: 鉴权 / 限流 / 用量计量 / 项目管理 / 统计分析 / 管理后台 / SSE 对话入口。
- 推理层: 十阶段 Pipeline(S0 网关 -> S9 归档) 在进程内编排, /api/chat 订阅同一
  gen:stream:<tid> 频道回放, 不依赖任何旧 proxy/agent/queue/cascade/roles 链路。

对外端口统一为 settings.app_port(默认 7101)。前端无需改造即可命中同域 SSE。
"""

import asyncio
import logging
import mimetypes
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import text

# 本地组件库根目录(相对此文件: backend/app/main.py -> backend/shared/vendor)。
# 生成站点引用 /vendor/libs/... 作为域名根绝对路径, 本路由让单进程后端直接静态托管该目录 ——
# 无论部署在 docker /opt/seedai 还是 home /home/huzhen/seedai, 都能用 __file__ 解析真实路径,
# 与 /api 反代解耦, 根绝对路径稳定可用。
VENDOR_DIR = Path(__file__).resolve().parent.parent / "shared" / "vendor"
# 安全边界: 只允许 /vendor 命中 VENDOR_DIR 内的真实文件, 防路径穿越。
_VENDOR_ABSPATH = VENDOR_DIR.resolve()

from .api import (
    admin_analytics_router,
    ops_router,
    preview_router,
    turns_router,
    workspace_router,
)
from .auth import router as auth_router
from app.config import settings
from .cache import get_redis
from .db import engine, init_db
from .logging_config import setup_logging
from .metrics import record_request
from .analytics import record_api_latency, record_api_call
from .reconciler import start_reconciler
from .services.recovery import reconcile_orphan_turns


logger = logging.getLogger("app.main")

setup_logging("app")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) 新库 schema 初始化(只建缺失表, 不迁移不重置)
    await init_db()
    # 2) 写失败对账器(Redis 侧, 与十阶段链路解耦)
    start_reconciler()
    # 3) 孤儿 Turn 对账: 进程被强杀会留下 status='running' 的 Turn, 在途 Pipeline 已死。
    #    翻 failed, 否则前端快照永远显示运行中。
    try:
        await reconcile_orphan_turns()
    except Exception as e:  # noqa: BLE001
        logger.error("[startup] 孤儿 Turn 对账失败(不阻断启动): %s", e)
    logger.info("统一应用启动完成(十阶段链路 v3.0)")
    yield


app = FastAPI(title=settings.app_title, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = (time.time() - start) * 1000
    path = request.url.path
    await record_request(path, response.status_code, elapsed)
    await record_api_latency(path, elapsed)
    await record_api_call(path, response.status_code)
    if path == "/health":
        logger.debug("[req] %s %s %d %.1fms", request.method, path, response.status_code, elapsed)
    else:
        logger.info("[req] %s %s %d %.1fms", request.method, path, response.status_code, elapsed)
    if response.status_code >= 500:
        logger.error("[req] 服务端错误 %s %s %d %.1fms", request.method, path, response.status_code, elapsed)
    return response


# 抑制 Windows ProactorEventLoop 上的 ConnectionResetError traceback
def _exception_handler(loop, context):
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError):
        logger.warning("连接被远程关闭(已忽略): %s", context.get("message", ""))
    else:
        loop.default_exception_handler(context)


asyncio.get_event_loop().set_exception_handler(_exception_handler)


# ---------- 健康检查 ----------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "app", "ts": int(time.time())}


@app.get("/ready")
async def ready():
    checks: dict = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["mysql"] = "ok"
    except Exception as e:
        checks["mysql"] = f"fail: {type(e).__name__}: {e}"
    try:
        r = await get_redis()
        pong = await r.ping()
        checks["redis"] = "ok" if pong else "fail: ping=false"
    except Exception as e:
        checks["redis"] = f"fail: {type(e).__name__}: {e}"
    all_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "service": "app",
        "ts": int(time.time()),
        "checks": checks,
    }, 200 if all_ok else 503


# ---------- 本地组件库静态托管(/vendor/) ----------
# 生成站点一律以 /vendor/libs/<name>/<file> 根绝对路径引用预置库(见 shared.vendor.LIBS_REFERENCE),
# 本路由让后端直接托管 backend/shared/vendor, 确保「域名/vendor/libs/...」在任何部署形态下都可用,
# 不依赖 nginx alias 的真实主机路径。nginx / 前端容器只需把 /vendor/ 透传反代到 7101 即可。
@app.get("/vendor/{path:path}")
async def serve_vendor(path: str):
    if not path or ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="invalid vendor path")
    target = (_VENDOR_ABSPATH / path).resolve()
    # 路径穿越防护: 必须仍在 VENDOR_DIR 内
    if target != _VENDOR_ABSPATH and _VENDOR_ABSPATH not in target.parents:
        raise HTTPException(status_code=403, detail="forbidden")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(str(target), media_type=media, headers={"Cache-Control": "public, max-age=86400"})


# ---------- 路由装配 ----------
# 十阶段链路唯一入口(turns_router)取代旧 proxy/projects/admin 三件套。
app.include_router(auth_router)
app.include_router(turns_router)
app.include_router(workspace_router)
app.include_router(admin_analytics_router)
# 签名预览(REQ-PREVIEW-001): 取代 v2 的 nginx auth_request + 同源静态直出方案。
app.include_router(preview_router)
# 运维可观测性(M10b): /readyz /ops/status /metrics
app.include_router(ops_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
