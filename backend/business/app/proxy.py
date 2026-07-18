"""生成代理:业务服务作为唯一对外入口。

- 前端永不直接触达 AI 服务。
- M0 匿名:GET /api/chat 不需要登录即可对话(对应文档 §3.7 / §5)。
- 这里负责把前端的 GET 请求翻译成 AI 服务的 POST /generate,并把 SSE 帧
  透明透传回去;同时转发取消信号到 AI 的 POST /cancel。
- 鉴权 / 限流 / 用量计量在此拦截(匿名场景仅做匿名计量,登录可扩展)。

SSE 透传策略:上游 AI 返回的是标准 SSE 文本帧(event:/data: 行 + 空行)。
我们用 httpx 以原始字节流读取,再以 StreamingResponse 原样吐出,保证
所有事件类型(token / think / node / preview / done / error / aborted /
degraded)一字不差地转发,不做任何重排。
"""
import json
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .config import settings
from .metrics import record_model_usage

router = APIRouter(prefix="/api", tags=["generate"])

# 匿名用户的计量 id(-1 表示未登录,与登录用户 id 区分)
_ANON_USER_ID = -1


def _parse_messages(request: Request) -> list:
    """从 query 解析消息列表。

    支持三种来源(优先级从高到低):
      1. messages=urlencode(JSON 数组)   —— 多轮对话标准格式
      2. q=单条用户消息                    —— 单轮便捷入口
      3. message=单条用户消息              —— 兼容别名
    都不存在则 400。
    """
    raw = request.query_params.get("messages")
    if raw:
        try:
            msgs = json.loads(raw)
            if isinstance(msgs, list) and len(msgs) > 0:
                return msgs
        except Exception:
            pass  # 解析失败回退到单条

    single = request.query_params.get("q") or request.query_params.get("message")
    if single:
        return [{"role": "user", "content": single}]

    raise HTTPException(status_code=400, detail="missing 'messages' or 'q' query param")


@router.get("/models")
async def list_models():
    """透传 AI 服务的模型列表(匿名可读,供前端模型选择器使用)。"""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{settings.ai_service_url}/models")
        r.raise_for_status()
        return r.json()


@router.get("/chat")
async def chat(
    request: Request,
    model: str = Query("hy3", description="模型 id,默认主模型 hy3"),
    trace_id: str | None = Query(None, description="前端生成的链路 id,用于取消"),
):
    """匿名 SSE 对话端点(文档 §3.7 / §5)。

    前端: GET /api/chat?model=<id>&messages=<JSON>&trace_id=<id>
    业务: 翻译成 POST {ai}/generate,逐帧透传 SSE。
    """
    messages = _parse_messages(request)
    tid = trace_id or uuid.uuid4().hex

    # 匿名计量(登录后可替换为真实 user id)
    await record_model_usage(_ANON_USER_ID, model)

    payload = {"model_id": model, "messages": messages, "trace_id": tid}

    # 流式透传:原始字节,不重排
    async def publisher():
        # read 不超时(生成可能持续数分钟),connect 给 10s
        timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{settings.ai_service_url}/generate",
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    # 上游报错:把错误体作为单行文本透出给前端
                    err_text = await resp.aread()
                    yield err_text
                    return
                async for chunk in resp.aiter_raw():
                    yield chunk

    return StreamingResponse(
        publisher(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": tid,  # 便于前端在没自带 trace_id 时也能取消
        },
    )


@router.post("/cancel")
async def cancel(request: Request):
    """级联取消(C1):转发到 AI 服务的 /cancel。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    trace_id = body.get("trace_id") or (await request.body()).decode("utf-8", "ignore")
    if not trace_id:
        raise HTTPException(status_code=400, detail="missing trace_id")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{settings.ai_service_url}/cancel", json={"trace_id": trace_id}
        )
        return r.json()
