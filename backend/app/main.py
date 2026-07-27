"""统一单进程应用入口(业务层 + 推理层合并)。

合并说明(用户指令: ai_service 与 business 合并为一个项目, 全量重写):
- 业务层: 鉴权 / 限流 / 用量计量 / 项目管理 / 统计分析 / 管理后台 / SSE 对话入口
  (proxy.py 现在直接把任务投递给同进程 Worker 队列, 不再经 httpx 转发)。
- 推理层: 意图混合级联 / skills / tools / Worker 池 / 队列 / QC / Chroma / providers,
  全部在 lifespan 中启动, /api/chat 订阅同一 gen:stream:<tid> 频道回放。

对外端口统一为 settings.app_port(默认 7101)。前端无需改造即可命中同域 SSE。
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

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
from .agent.events import to_sse
from .agent.providers import list_providers
from .agent.core.queue import get_queue, worker_loop
from .agent.registry import SkillRegistry, ToolRegistry

# 引导注册: 触发 @register_skill / @tool 装饰器(原 ai_service 启动时 import 两个包)。
# 必须在路由装配前完成, 否则 /skills、/tools、Worker 路由都拿不到注册项。
import app.agent.skills  # noqa: F401
import app.agent.tools   # noqa: F401


logger = logging.getLogger("app.main")

setup_logging("app")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) 业务层 schema 初始化 + 超管种子
    await init_db()
    # 2) 推理层: 确保队列单例、重建 Chroma 集合
    #    注: 意图向量索引(ensure_intent_index) 已移至 scripts/reset_all.py 在重置阶段构建,
    #        启动时不再每次重嵌 82 句, 仅做轻量检查, 缺失则告警提示运行重置脚本。
    get_queue()
    try:
        from .agent.knowledge.chroma import ensure_collections
        ensure_collections()
    except Exception as e:
        logger.warning("lifespan: ensure_collections 失败(可忽略): %s", e)
    try:
        from .agent.intent.vector_store import check_intent_index
        if not check_intent_index():
            logger.warning(
                "[startup] 意图向量索引(intents)缺失/为空, 语义召回将降级为离线 bigram。"
                "请运行 scripts/reset_all.py 重建索引(已移至重置阶段, 不再每次重启重建)。"
            )
        else:
            logger.info("[startup] 意图向量索引检测就绪(集合=intents, 跳过重建)")
    except Exception as e:
        logger.warning("lifespan: 检查意图索引失败(可忽略): %s", e)
    # 3) 启动 Worker 池(消费 queue:generate, 发布进度)
    try:
        loop = asyncio.get_running_loop()
        worker_task = loop.create_task(worker_loop(concurrency=settings.worker_concurrency))
        logger.info("[startup] Worker 池已提交到事件循环 (concurrency=%s)", settings.worker_concurrency)
    except Exception as e:
        logger.error("[startup] Worker 池启动失败: %s", e)
    # 4) 业务层对账器
    start_reconciler()
    logger.info("统一应用启动完成(单进程 v2.0)")
    yield
    worker_task.cancel()


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



# ---------- 推理层只读端点(原 ai_service 暴露, 现同进程) ----------
@app.get("/models")
async def models():
    return list_providers()


@app.get("/skills")
async def list_skills():
    return [
        {"name": e.name, "intent_tags": e.intent_tags, "is_graph": e.is_graph, "description": e.description}
        for e in SkillRegistry.all()
    ]


@app.get("/tools")
async def list_tools():
    return [
        {"name": e.name, "scope": e.scope, "risk": e.risk, "description": e.description}
        for e in ToolRegistry.all()
    ]


@app.get("/registry")
async def registry_summary():
    return {"skills": SkillRegistry.names(), "tools": ToolRegistry.names()}


@app.get("/agents")
async def list_agents():
    """Agent 注册表(公开, 供前端头像/名称展示)。单进程直读 SkillRegistry。"""
    return SkillRegistry.list_agents()


@app.post("/cancel")
async def cancel(req: Request):
    """级联取消(C1): 标记 cancel:<trace_id>, Worker 在下个 token/阶段前中断。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    trace_id = body.get("trace_id")
    if trace_id:
        await get_queue().set_cancel(trace_id)
        logger.info("[cancel] 标记取消 trace=%s", trace_id)
        return {"ok": True, "trace_id": trace_id}
    logger.warning("[cancel] 缺少 trace_id, 忽略")
    return {"ok": False, "error": "missing trace_id"}


@app.post("/retry-upload")
async def retry_upload(req: Request):
    """业务端触发: 对本地暂存的产物重新上传 COS, 返回线上 URL。"""
    import os
    from pathlib import Path

    try:
        body = await req.json()
    except Exception:
        body = {}
    trace_id = body.get("trace_id")
    if not trace_id:
        return {"ok": False, "error": "missing trace_id"}
    art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
    idx = art_dir / "anon" / trace_id / "index.html"
    if not idx.exists():
        return {"ok": False, "error": f"本地文件不存在: {idx}"}
    try:
        from .agent.tools.cos_upload import cos_upload
        cos_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/anon/{trace_id}/index.html"
        res = cos_upload(str(idx), cos_key)
        if res.get("ok"):
            logger.info("[retry-upload] 上传成功 trace=%s url=%s", trace_id, res["url"])
            return {"ok": True, "url": res["url"]}
        return {"ok": False, "error": res.get("error", "COS 上传失败")}
    except Exception as e:
        logger.error("[retry-upload] 异常 trace=%s: %s", trace_id, e)
        return {"ok": False, "error": str(e)}


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


# ---------- 路由装配(业务层) ----------
app.include_router(auth_router)
app.include_router(proxy_router)
app.include_router(projects_router)
app.include_router(admin_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
