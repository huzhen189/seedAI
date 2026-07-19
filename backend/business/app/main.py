"""业务服务入口(唯一对外)。装配鉴权 / 生成代理 / 管理监控。"""

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .admin import router as admin_router
from .auth import router as auth_router
from .config import settings
from .db import init_db
from .logging_config import setup_logging
from .metrics import record_request
from .analytics import record_api_latency
from .projects import router as projects_router
from .proxy import router as proxy_router
from .reconciler import start_reconciler


app = FastAPI(title="SeedAI Business API")

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
    await record_request(request.url.path, response.status_code, elapsed)
    await record_api_latency(request.url.path, elapsed)
    return response


@app.on_event("startup")
async def on_startup():
    await init_db()
    start_reconciler()


@app.get("/health")
async def health():
    return {"status": "ok"}


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
