"""Agent Core entry (P0 skeleton).

This is the inference service. In later phases it grows:
  - Memory Manager (buffer/vector/summary/slots)
  - Router (classifier/resolver/rules) + Guardrails
  - Runtime (planner/executor/orchestrator)
  - Model Gateway

P0 scope: healthz + /stream returns a minimal SSE hello so the shell runs and
business can later issue /api/chat/entry -> EventSource(agent/stream).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from shared.config import settings
from shared.db import init_db

logger = logging.getLogger("agent.main")

app = FastAPI(title="SeedAI Agent Core")


@app.on_event("startup")
async def _startup():
    await init_db()
    logger.info("Agent Core started; db schema ensured")


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"


@app.get("/stream")
async def stream(trace_id: str = "", q: str = "", token: str = ""):
    """P0 placeholder SSE endpoint.

    Returns a minimal structured stream so the frontend two-step connect can be
    wired end-to-end before the full pipeline exists. Phase P3/P4 replace this
    with the real Memory->Router->Runtime pipeline + stream_exists replay.
    """
    from fastapi.responses import StreamingResponse
    import json

    async def gen():
        ev = {"seq": 0, "hello": "agent-core-p0", "trace_id": trace_id, "q": q}
        yield f"event: plan\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        done = {"seq": 1, "status": "ok"}
        yield f"event: done\ndata: {json.dumps(done, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        app_dir="backend/agent",
        host="0.0.0.0",
        port=settings.ai_service_port,
        reload=False,
    )
