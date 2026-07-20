"""核心 AI 服务(仅内网)。暴露 /generate(SSE) / /cancel / /models / /skills / /tools / /registry。

启动即 bootstrap():导入 skills + tools 包,把全部 Skill/Tool 注册进 Registry(§5.8/§5.9)。
/ generate 流程(1-C):入队 queue:generate → Worker 消费 → run_skill 产出事件流 → 经进度频道
publish → 本端点订阅该频道并转成 SSE 帧透传给业务服务(§3.7)。Worker 在 lifespan 后台启动。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .config import settings
from .events import to_sse
from .logging_config import setup_logging
from .providers import list_providers
from .queue import get_queue, worker_loop
from .registries import bootstrap
from .registry import SkillRegistry, ToolRegistry


# 初始化日志:控制台 + 本地按日期滚动文件(backend/ai_service/logs/ai_service.log)。
# 必须在 bootstrap/路由装配前调用,确保启动期与注册期日志也能落盘。
setup_logging("ai_service")

logger = logging.getLogger("ai_service.main")

# P0 崩溃恢复: 抑制 Windows ProactorEventLoop 上 ConnectionResetError traceback
# (远程 LLM API 断连会在传输层清理时报这个错, 不影响服务运行, 降为 WARNING)
def _exception_handler(loop, context):
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError):
        logger.warning("连接被远程关闭(已忽略): %s", context.get("message", ""))
    else:
        loop.default_exception_handler(context)

asyncio.get_event_loop().set_exception_handler(_exception_handler)

# 引导注册(导入 skills + tools 包,完成全部注册)
_REGISTRY = bootstrap()

# Worker 后台任务句柄
_worker_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_task
    get_queue()  # 确保队列单例初始化(副作用)
    # 启动 Worker 池(消费 queue:generate,发布进度)
    _worker_task = __import__("asyncio").ensure_future(
        worker_loop(concurrency=settings.worker_concurrency)
    )
    yield
    if _worker_task is not None:
        _worker_task.cancel()


app = FastAPI(title="SeedAI AI Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # M0 内网,生产收紧
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateReq(BaseModel):
    model_id: str = "deepseek"
    messages: list
    skill: str | None = None
    trace_id: str | None = None
    conversation_id: int | None = None
    context_hint: str = ""            # 前端WebLLM上下文检测
    conversation_summary: str = ""    # Redis对话摘要
    project_status: str = "draft"     # 项目状态
    requirement_doc: dict | None = None  # 需求文档


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/models")
async def models():
    return list_providers()


@app.get("/agents")
async def list_agents():
    from .agents import AGENTS
    return AGENTS


@app.get("/skills")
async def list_skills():
    return [
        {
            "name": e.name,
            "intent_tags": e.intent_tags,
            "is_graph": e.is_graph,
            "description": e.description,
        }
        for e in SkillRegistry.all()
    ]


@app.get("/tools")
async def list_tools():
    return [
        {
            "name": e.name,
            "scope": e.scope,
            "risk": e.risk,
            "description": e.description,
        }
        for e in ToolRegistry.all()
    ]


@app.get("/registry")
async def registry_summary():
    return _REGISTRY


@app.post("/generate")
async def generate(req: GenerateReq, after: str | None = None):
    """SSE 生成端点:入队 → 订阅进度流 → 透传事件流(§3.7 / 1-C)。"""
    q = get_queue()
    trace_id = req.trace_id or uuid.uuid4().hex
    user_input = ""
    for m in req.messages:
        if m.get("role") == "user":
            user_input = (m.get("content", "") or "")[:80]
            break
    logger.info(
        "[1/3] 接收入参 trace=%s model=%s conv=%s msgs=%d skill=%s "
        "ctx_hint=%.40s summary=%.40s project=%s doc=%s input=%.80s",
        trace_id, req.model_id, req.conversation_id, len(req.messages),
        req.skill or "auto", req.context_hint[:40] if req.context_hint else "-",
        req.conversation_summary[:40] if req.conversation_summary else "-",
        req.project_status, "有" if req.requirement_doc else "无", user_input,
    )
    resuming = await q.stream_exists(trace_id)
    if not resuming:
        await q.open_channel(trace_id)
        job = {
            "trace_id": trace_id,
            "model_id": req.model_id,
            "messages": req.messages,
            "skill": req.skill,
            "conversation_id": req.conversation_id,
            "context_hint": req.context_hint,
            "conversation_summary": req.conversation_summary,
            "project_status": req.project_status,
            "requirement_doc": req.requirement_doc,
        }
        await q.enqueue(job)
        logger.info("[2/3] 新任务入队 trace=%s queue=%s", trace_id, type(q).__name__)
    else:
        logger.info("[2/3] 续接已有流 trace=%s after=%s 全量回放", trace_id, after or "无")
    event_count = 0
    async def stream():
        nonlocal event_count
        async for event in q.subscribe(trace_id, after):
            event_count += 1
            yield to_sse(event)
        logger.info("[3/3] SSE流结束 trace=%s 共推送%d个事件", trace_id, event_count)
    return EventSourceResponse(stream())


@app.post("/cancel")
async def cancel(req: Request):
    """级联取消(C1):标记 cancel:<trace_id>,Worker 在下个 token 前中断。"""
    body = await req.json()
    trace_id = body.get("trace_id")
    if trace_id:
        await get_queue().set_cancel(trace_id)
        return {"ok": True, "trace_id": trace_id}
    return {"ok": False, "error": "missing trace_id"}


@app.post("/retry-upload")
async def retry_upload(req: Request):
    """业务端触发: 对本地暂存的产物重新上传 COS, 返回线上 URL。"""
    import os
    from pathlib import Path

    body = await req.json()
    trace_id = body.get("trace_id")
    if not trace_id:
        return {"ok": False, "error": "missing trace_id"}
    art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
    idx = art_dir / "anon" / trace_id / "index.html"
    if not idx.exists():
        return {"ok": False, "error": f"本地文件不存在: {idx}"}
    try:
        from .tools.cos_upload import cos_upload
        cos_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/anon/{trace_id}/index.html"
        res = cos_upload(str(idx), cos_key)
        if res.get("ok"):
            return {"ok": True, "url": res["url"]}
        return {"ok": False, "error": res.get("error", "COS 上传失败")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# 本地直跑入口:python backend/ai_service/app/main.py
# 锁定端口为 settings.ai_service_port(默认 7102),避免回退到 uvicorn 默认 8000。
# 生产/docker 由 Dockerfile 的 `uvicorn ... --port 7102` 启动,不走此分支。
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.ai_service_port)
