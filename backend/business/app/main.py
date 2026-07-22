"""业务服务入口(唯一对外)。装配鉴权 / 生成代理 / 管理监控。"""

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .admin import router as admin_router
from .auth import router as auth_router
from .config import settings
from .cache import get_redis
from .db import engine, init_db
from .logging_config import setup_logging
from .metrics import record_request
from .analytics import record_api_latency, record_api_call
from .projects import router as projects_router
from .proxy import router as proxy_router
from .reconciler import start_reconciler


app = FastAPI(title="SeedAI Business API")

logger = logging.getLogger("business.main")

# 初始化日志:控制台 + 本地按日期滚动文件(backend/business/logs/business.log)。
# 必须在路由装配前调用,确保启动期日志也能落盘。
setup_logging("business")

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
    # 统计: 请求量 / 延迟 / 状态码分段(供管理后台「系统分析」)
    await record_request(path, response.status_code, elapsed)
    await record_api_latency(path, elapsed)
    await record_api_call(path, response.status_code)
    # 访问日志: /health 等探活请求降为 DEBUG 避免刷屏, 其余 INFO
    if path == "/health":
        logger.debug("[req] %s %s %d %.1fms", request.method, path, response.status_code, elapsed)
    else:
        logger.info("[req] %s %s %d %.1fms", request.method, path, response.status_code, elapsed)
    # 5xx 服务端错误额外告警, 便于快速定位故障
    if response.status_code >= 500:
        logger.error("[req] 服务端错误 %s %s %d %.1fms", request.method, path, response.status_code, elapsed)
    return response


@app.on_event("startup")
async def on_startup():
    await init_db()
    start_reconciler()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    """存活探针(liveness):仅检查进程是否可响应,不依赖任何外部资源。

    用于 k8s/docker 的 livenessProbe —— 只要进程没死就返回 200,OOM/死锁时
    由编排层重启容器。刻意**不**碰 MySQL/Redis,避免存活探针因外部抖动误杀。
    """
    return {"status": "ok", "service": "business", "ts": int(time.time())}


@app.get("/ready")
async def ready():
    """就绪探针(readiness):检查核心外部依赖是否可用(MySQL + Redis)。

    用于 k8s/docker 的 readinessProbe —— 依赖未就绪时返回 503,编排层暂不转发
    流量但**不**杀容器,等依赖恢复后自动重新接入。本地直跑时同样可用
    `curl localhost:7101/ready` 验证链路连通性。
    """
    checks: dict = {}
    # 1) MySQL: 复用已有异步引擎跑一条轻量 SELECT 1
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["mysql"] = "ok"
    except Exception as e:  # 捕获所有异常(连接/超时/鉴权),不让探针自身 500
        checks["mysql"] = f"fail: {type(e).__name__}: {e}"
    # 2) Redis: 复用 cache 池 ping
    try:
        r = await get_redis()
        pong = await r.ping()
        checks["redis"] = "ok" if pong else "fail: ping=false"
    except Exception as e:
        checks["redis"] = f"fail: {type(e).__name__}: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "service": "business",
        "ts": int(time.time()),
        "checks": checks,
    }, 200 if all_ok else 503


# 路由装配
app.include_router(auth_router)
app.include_router(proxy_router)
app.include_router(projects_router)
app.include_router(admin_router)


# 本地直跑入口:python backend/business/app/main.py
# 锁定端口为 settings.business_api_port(默认 7101),避免回退到 uvicorn 默认 8000。
# 生产/docker 由 Dockerfile 的 `uvicorn ... --port 7101` 启动,不走此分支。
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.business_api_port)
