"""核心 AI 服务(仅内网)。暴露 /generate(SSE) / /cancel / /models / /skills / /tools / /registry。

启动即 bootstrap():导入 skills + tools 包,把全部 Skill/Tool 注册进 Registry(§5.8/§5.9)。
/ generate 流程(1-C):入队 queue:generate → Worker 消费 → run_skill 产出事件流 → 经进度频道
publish → 本端点订阅该频道并转成 SSE 帧透传给业务服务(§3.7)。Worker 在 lifespan 后台启动。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .config import settings
from .events import to_sse
from .providers import list_providers
from .queue import get_queue, worker_loop
from .registries import bootstrap
from .registry import SkillRegistry, ToolRegistry

# 引导注册(导入 skills + tools 包,完成全部注册)
_REGISTRY = bootstrap()

# Worker 后台任务句柄
_worker_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_task
    q = get_queue()
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
    model_id: str = "hy3"
    messages: list
    skill: str | None = None  # 可显式指定 Skill,否则由 Router 意图判定
    trace_id: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/models")
async def models():
    return list_providers()


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
async def generate(req: GenerateReq):
    """SSE 生成端点:入队 → 订阅进度频道 → 透传事件流(§3.7 / 1-C)。"""
    q = get_queue()
    trace_id = req.trace_id or uuid.uuid4().hex
    job = {
        "trace_id": trace_id,
        "model_id": req.model_id,
        "messages": req.messages,
        "skill": req.skill,
    }
    # 先建立订阅(避免丢首帧),再入队
    channel = await q.open_channel(trace_id)
    await q.enqueue(job)

    async def stream():
        async for event in q.subscribe(channel):
            yield to_sse(event)

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
