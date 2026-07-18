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
from .logging_config import setup_logging
from .providers import list_providers
from .queue import get_queue, worker_loop
from .registries import bootstrap
from .registry import SkillRegistry, ToolRegistry


# 初始化日志:控制台 + 本地按日期滚动文件(backend/ai_service/logs/ai_service.log)。
# 必须在 bootstrap/路由装配前调用,确保启动期与注册期日志也能落盘。
setup_logging("ai_service")

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
async def generate(req: GenerateReq, after: str | None = None):
    """SSE 生成端点:入队 → 订阅进度流 → 透传事件流(§3.7 / 1-C)。

    - trace_id 已存在(进度流已建立)→ **续接已有流**(回放 + 实时),不再重新入队;
      这是「离线继续 + 重连回放」的关键:Worker 独立运行,断线重连只是重新订阅同一流。
    - after 为断点(stream id):仅回放其后的增量;为 None 则从头全量回放。
    """
    q = get_queue()
    trace_id = req.trace_id or uuid.uuid4().hex
    resuming = await q.stream_exists(trace_id)
    if not resuming:
        # 新任务:建立通道(避免丢首帧)后入队,Worker 消费并 publish 到进度流
        await q.open_channel(trace_id)
        job = {
            "trace_id": trace_id,
            "model_id": req.model_id,
            "messages": req.messages,
            "skill": req.skill,
        }
        await q.enqueue(job)

    async def stream():
        async for event in q.subscribe(trace_id, after):
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


# 本地直跑入口:python backend/ai_service/app/main.py
# 锁定端口为 settings.ai_service_port(默认 7102),避免回退到 uvicorn 默认 8000。
# 生产/docker 由 Dockerfile 的 `uvicorn ... --port 7102` 启动,不走此分支。
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.ai_service_port)
