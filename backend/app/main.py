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

from .api import admin_analytics_router, turns_router, workspace_router
from .auth import router as auth_router
from app.config import settings
from .cache import get_redis
from .db import engine, init_db
from .logging_config import setup_logging
from .metrics import record_request
from .analytics import record_api_latency, record_api_call
from .artifacts_auth import router as artifacts_auth_router
from .reconciler import start_reconciler
from .services.recovery import reconcile_orphan_turns
from .agent.providers import list_providers
from .agent.registry import SkillRegistry, ToolRegistry

# 引导注册: 触发 @register_skill / @tool 装饰器(原 ai_service 启动时 import 两个包)。
# 必须在路由装配前完成, 否则 /skills、/tools、Worker 路由都拿不到注册项。
import app.agent.skills  # noqa: F401
import app.agent.tools   # noqa: F401


logger = logging.getLogger("app.main")

setup_logging("app")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) 新库 schema 初始化(只建缺失表, 不迁移不重置)
    await init_db()
    # 2) 知识层: 重建 Chroma 集合
    #    注: 意图向量索引(ensure_intent_index) 已移至 scripts/reset_all.py 在重置阶段构建,
    #        启动时不再每次重嵌 82 句, 仅做轻量检查, 缺失则告警提示运行重置脚本。
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
    # 3) 写失败对账器(Redis 侧, 与十阶段链路解耦)
    start_reconciler()
    # 4) 孤儿 Turn 对账: 进程被强杀会留下 status='running' 的 Turn, 在途 Pipeline 已死。
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


# 兼容前端 /api 前缀约定(vite 仅代理 /api 与 /admin, 根路径不经代理无法被浏览器访问)。
# 浏览器经代理必须走 /api 前缀, 故为推理层只读端点补 /api 别名, 避免 404。
@app.get("/api/models")
async def models_api():
    return list_providers()


@app.get("/api/skills")
async def skills_api():
    return [
        {"name": e.name, "intent_tags": e.intent_tags, "is_graph": e.is_graph, "description": e.description}
        for e in SkillRegistry.all()
    ]


@app.get("/api/tools")
async def tools_api():
    return [
        {"name": e.name, "scope": e.scope, "risk": e.risk, "description": e.description}
        for e in ToolRegistry.all()
    ]


@app.get("/api/agents")
async def agents_api():
    return SkillRegistry.list_agents()


@app.get("/registry")
async def registry_summary():
    return {"skills": SkillRegistry.names(), "tools": ToolRegistry.names()}


@app.get("/agents")
async def list_agents():
    """Agent 注册表(公开, 供前端头像/名称展示)。单进程直读 SkillRegistry。"""
    return SkillRegistry.list_agents()


@app.post("/retry-upload")
async def retry_upload(req: Request):
    """业务端触发: 对本地暂存的产物重新上传 COS, 返回线上 URL。

    P1: 本地路径改用 {uid}/{pid}/v{ver}/index.html(与 generate_site 同树);
    trace_id 仍作为兼容回退(无 uid/pid/ver 时使用 anon/<trace>)。
    """
    import os
    from pathlib import Path

    try:
        body = await req.json()
    except Exception:
        body = {}
    trace_id = body.get("trace_id")
    uid = body.get("user_id")
    pid = body.get("project_id")
    ver = body.get("version")
    from shared.artifacts import site_dir, rel_path_for

    if uid is not None and pid is not None:
        src = site_dir(uid, pid, ver) / "index.html"
    elif trace_id:
        src = Path(os.getenv("ARTIFACT_DIR", "./artifacts")) / "anon" / trace_id / "index.html"
    else:
        return {"ok": False, "error": "missing trace_id or (user_id+project_id)"}
    if not src.exists():
        return {"ok": False, "error": f"本地文件不存在: {src}"}
    try:
        from .agent.tools.cos_upload import cos_upload
        from shared.artifacts import cos_key_for

        # 优先按 {uid}/{pid}/v{ver} 求 COS key; 兼容回退走 anon/<trace>
        if uid is not None and pid is not None:
            cos_key = cos_key_for(uid, pid, ver, "index.html")
        else:
            cos_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/anon/{trace_id}/index.html"
        res = cos_upload(str(src), cos_key)
        if res.get("ok"):
            logger.info("[retry-upload] 上传成功 %s url=%s", cos_key, res["url"])
            return {"ok": True, "url": res["url"]}
        return {"ok": False, "error": res.get("error", "COS 上传失败")}
    except Exception as e:
        logger.error("[retry-upload] 异常 %s: %s", src, e)
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
app.include_router(artifacts_auth_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.app_port)
