"""生成代理:业务服务作为唯一对外入口。

- 前端永不直接触达 AI 服务。
- 鉴权门禁:GET /api/chat 需登录(从 HttpOnly Cookie 取 JWT);未登录时下发
  SSE error 事件(code=AUTH_REQUIRED, message="Missing authentication"),而非 JSON 401,
  以便前端 EventSource 识别并主动弹出登录框(文档 §3.7 / §5 / §2.1)。
- 这里负责把前端的 GET 请求翻译成 AI 服务的 POST /generate,并把 SSE 帧
  透明透传回去;同时转发取消信号到 AI 的 POST /cancel。
- 鉴权 / 限流 / 用量计量在此拦截(已登录用户按真实 user_id 计量)。
- **落库(M1)**:/api/chat 入参新增 conversation_id;SSE 流结束(或中断)时,
  把首条用户消息 + AI 完整回复双写进 Message,并更新 Conversation.updated_at,
  首条时自动生成会话标题。落库失败不阻塞已完成的流。

SSE 透传策略:上游 AI 返回标准 SSE 文本帧(event:/data: 行 + 空行)。
我们用 httpx aiter_lines 逐行解析,重建标准 SSE 帧原样吐出(保证
think/token/node/preview/done/error/aborted/degraded 一字不差地转发,兼容
前端 EventSource),同时收集 token 帧内容用于落库。
"""

import json
import logging
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .metrics import consume_daily_quota, record_model_usage
from .models import Conversation, Message, User
from .security import ACCESS_COOKIE, CurrentUser, decode_token


router = APIRouter(prefix="/api", tags=["generate"])

logger = logging.getLogger("proxy")

_bearer = HTTPBearer(auto_error=False)


async def _resolve_user(request: Request) -> CurrentUser | None:
    """手动解析登录态(避免直接调用带 Depends 的 get_current_user)。

    返回 None 表示未登录 / token 无效;前端据此收到 SSE auth error 事件。
    """
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        try:
            creds = await _bearer(request)
            if creds is not None:
                token = creds.credentials
        except Exception:
            token = None
    if not token:
        return None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None
        return CurrentUser(int(payload["sub"]), payload.get("role", "user"))
    except Exception:
        return None


def _error_frame(code: str, message: str) -> str:
    """构造一条标准 SSE error 帧文本(供生成器 yield)。"""
    return (
        "event: error\n"
        f"data: {json.dumps({'code': code, 'message': message}, ensure_ascii=False)}\n\n"
    )


def _sse_error_frame(code: str, message: str) -> StreamingResponse:
    """通用 SSE error 帧响应(HTTP 200 + text/event-stream)。

    浏览器 EventSource 读不到非 2xx 状态码,任何业务/上游错误都必须
    以 SSE error 帧下发,前端才能识别并给出明确提示(而非笼统“连接中断”)。
    """

    async def gen():
        yield _error_frame(code, message)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse_auth_error() -> StreamingResponse:
    """鉴权失败时返回一条 SSE error 事件(而非 JSON 401)。

    浏览器 EventSource 读不到 HTTP 401 状态码,只能解析 SSE 帧;
    故用标准 SSE 帧下发 AUTH_REQUIRED,前端即可主动弹出登录框。
    """
    return _sse_error_frame("AUTH_REQUIRED", "Missing authentication")


def _map_upstream_error(status: int, body: bytes) -> tuple[str, str]:
    """把上游 HTTP 错误状态码/响应体映射成(错误码, 中文提示)。"""
    if status == 429:
        return "RATE_LIMITED", "请求过于频繁，请稍后再试"
    message = "AI 服务暂时不可用，请稍后重试"
    try:
        data = json.loads(body)
        if isinstance(data, dict) and data.get("detail"):
            message = str(data["detail"])
    except Exception:
        pass
    return "UPSTREAM_ERROR", message


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
    db: AsyncSession = Depends(get_db),
    model: str = Query("hy3", description="模型 id,默认主模型 hy3"),
    conversation_id: int = Query(..., description="会话 id,必填(前端先建会话)"),
    trace_id: str | None = Query(None, description="前端生成的链路 id,用于取消/续传"),
    after: str | None = Query(None, description="断点续传:仅回放该 stream id 之后的增量(留空=全量回放)"),
):
    """登录后 SSE 对话端点(文档 §3.7 / §5 / §2.1 / §15.3)。

    前端: GET /api/chat?model=<id>&conversation_id=<cid>&messages=<JSON>&trace_id=<id>
          (需携带登录 Cookie)
    业务: 校验 JWT → 翻译成 POST {ai}/generate,逐帧透传 SSE → 流结束落库。
    鉴权失败:返回 SSE error 事件(code=AUTH_REQUIRED),而非 JSON 401,
    以便前端 EventSource 识别并主动弹出登录框。
    """
    # 手动解析登录态;未登录则下发 SSE auth error(浏览器读不到 401 状态码)。
    user = await _resolve_user(request)
    if user is None:
        return _sse_auth_error()

    messages = _parse_messages(request)
    tid = trace_id or uuid.uuid4().hex

    # 取首条用户消息文本(用于落库 + 自动标题)
    user_text = ""
    for m in messages:
        if m.get("role") == "user":
            user_text = m.get("content", "") or ""
            break

    # 每日配额检查(①-b):基于 user_id,free 默认 50 次/天,超额返回 RATE_LIMITED 帧
    plan = (
        await db.execute(select(User.plan).where(User.id == user.id))
    ).scalar_one_or_none() or "free"
    allowed, _ = await consume_daily_quota(user.id, plan)
    if not allowed:
        return _sse_error_frame(
            "RATE_LIMITED",
            f"今日生成次数已用尽（{settings.free_daily_quota} 次/天），请明日再来或升级套餐",
        )

    # 已登录用户按真实 user_id 计量
    await record_model_usage(user.id, model)

    payload = {"model_id": model, "messages": messages, "trace_id": tid}
    # 续传:把 after 作为查询参数转发给 AI /generate(断点续接同一进度流)
    gen_url = f"{settings.ai_service_url}/generate"
    if after:
        from urllib.parse import urlencode

        gen_url += "?" + urlencode({"after": after})

    async def publisher():
        # read 不超时(生成可能持续数分钟),connect 给 10s
        timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
        assistant_parts: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                try:
                    async with client.stream(
                        "POST",
                        gen_url,
                        json=payload,
                    ) as resp:
                        if resp.status_code >= 400:
                            # 上游报错:翻译成 SSE error 帧(而非裸字节),
                            # 浏览器 EventSource 才能识别并提示明确文案。
                            err_text = await resp.aread()
                            code, message = _map_upstream_error(resp.status_code, err_text)
                            yield _error_frame(code, message)
                            return
                        event = None
                        data_parts: list[str] = []
                        async for raw_line in resp.aiter_lines():
                            if raw_line == "":
                                if event is not None or data_parts:
                                    data = "\n".join(data_parts)
                                    if event == "token":
                                        assistant_parts.append(data)
                                    # 重建标准 SSE 帧透传(兼容前端 EventSource)
                                    frame = ""
                                    if event:
                                        frame += f"event: {event}\n"
                                    frame += f"data: {data}\n\n"
                                    yield frame.encode("utf-8")
                                event, data_parts = None, []
                                continue
                            if raw_line.startswith("event:"):
                                event = raw_line[6:].strip()
                            elif raw_line.startswith("data:"):
                                data_parts.append(raw_line[5:].strip())
                                # 其他行(id:/retry:/注释)忽略透传
                except httpx.HTTPError as e:
                    # 上游连接/读取异常(AI 服务宕机、Redis 未起导致
                    # /generate 入队失败而 5xx 等):下发 SSE error 帧,
                    # 避免连接裸断让前端只能笼统报“连接中断”。
                    logger.warning("upstream request failed: %s", e)
                    yield _error_frame("UPSTREAM_ERROR", "AI 服务暂时不可用，请稍后重试")
                    return
        finally:
            # 流结束(正常/中断)均尝试落库;失败仅记录,不阻塞已返回的流
            try:
                await _persist_conversation(
                    db, user, conversation_id, model, user_text, "".join(assistant_parts), tid
                )
            except Exception as e:  # 落库失败不影响已完成生成
                logger.warning("persist conversation failed: %s", e)

    return StreamingResponse(
        publisher(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": tid,  # 便于前端在没自带 trace_id 时也能取消
        },
    )


async def _persist_conversation(
    db: AsyncSession,
    user: CurrentUser,
    conversation_id: int,
    model: str,
    user_text: str,
    assistant_text: str,
    trace_id: str,
) -> None:
    """SSE 结束后落库:用户消息 + AI 回复;更新会话标题/updated_at。

    幂等(重连 / 续传):同一 trace_id 只插一条 user 消息;assistant 消息按 trace_id
    upsert(首落 insert、续传 update),避免刷新 / 重连导致重复行。
    """
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        return  # 会话不存在或不属于该用户,跳过落库

    # user 消息:同 trace_id 已存在则跳过(重连不会重复插入)
    user_msg = (
        await db.execute(
            select(Message).where(
                Message.trace_id == trace_id, Message.role == "user"
            )
        )
    ).scalar_one_or_none()
    if user_msg is None:
        db.add(
            Message(
                conversation_id=conv.id,
                role="user",
                content=user_text,
                model_id=model,
                trace_id=trace_id,
            )
        )

    # assistant 消息:首落 insert,重连续传 update(覆盖为最新完整内容)
    if assistant_text:
        asst_msg = (
            await db.execute(
                select(Message).where(
                    Message.trace_id == trace_id, Message.role == "assistant"
                )
            )
        ).scalar_one_or_none()
        if asst_msg is None:
            db.add(
                Message(
                    conversation_id=conv.id,
                    role="assistant",
                    content=assistant_text,
                    model_id=model,
                    trace_id=trace_id,
                )
            )
        else:
            asst_msg.content = assistant_text
            asst_msg.model_id = model

    if not conv.title and user_text:
        conv.title = user_text[:20]
    conv.updated_at = datetime.utcnow()
    await db.commit()


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
        r = await client.post(f"{settings.ai_service_url}/cancel", json={"trace_id": trace_id})
        return r.json()
