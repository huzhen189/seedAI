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

import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .analytics import record_error, record_intent_result, record_model_detail, record_skill_outcome, record_user_active, record_intent_decision
from .analytics import record_agent_usage, record_context_detection, record_requirement_doc, record_project_status_transition
from .analytics import record_qc, record_feedback
from .cache import cache_get, cache_set, ck_delete, ck_get, ck_set, enqueue_write_error, get_redis
from .config import settings
from .db import get_db
from .metrics import consume_daily_quota, record_model_usage, record_unsupported
from .models import Artifact, Conversation, Message, Project, Trace, User
from .repos.business_repos import conv_repo, message_repo
from .repos.trace_repos import feedback_repo, qc_score_repo, trace_repo
from .schemas import FeedbackReq
from .security import ACCESS_COOKIE, CurrentUser, _set_access_cookie, create_access_token, decode_token, get_current_user
from .tracing import append_trace_event, create_trace, finish_trace, log_usage

import html as _html
import re


# ---------- 内容安全 ----------
_SENSITIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"<script[^>]*>.*?</script>", re.I | re.S),
    re.compile(r"javascript\s*:", re.I),
    re.compile(r"on\w+\s*=", re.I),
]
_INPUT_MAX_LEN = 8000


def _sanitize_input(text: str) -> str:
    if len(text) > _INPUT_MAX_LEN:
        text = text[:_INPUT_MAX_LEN]
    for pat in _SENSITIVE_PATTERNS:
        text = pat.sub("[已过滤]", text)
    return text


def _sanitize_html(html_str: str) -> str:
    for pat in _SENSITIVE_PATTERNS:
        html_str = pat.sub("[已过滤]", html_str)
    return html_str


def _parse_project_forbid(text: str | None) -> list[str]:
    """从项目 system_prompt 抽取结构化禁用意图/词(Tier 2, 与 ai_service 侧约定一致)。

    约定: system_prompt 内以独立行 `--forbid: deploy, payment` 声明, 逗号/空白分隔。
    只取结构化片段, 绝不解析自由文本(防误拦/漏拦)。无声明返回空列表。
    """
    if not text:
        return []
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.lower().startswith("--forbid:"):
            continue
        body = s[len("--forbid:"):].strip()
        for tok in body.replace(",", " ").split():
            tok = tok.strip().strip("\"'")
            if tok:
                out.append(tok.lower())
    return out


# 模型 id -> 供应商(用于用量账本成本归集;与 providers.py 的适配器命名保持一致)
_PROVIDER_BY_MODEL = {
    "hy3": "tokenhub",
    "qwen": "aliyun",
    "deepseek": "deepseek",
}


router = APIRouter(prefix="/api", tags=["generate"])

logger = logging.getLogger("proxy")

_bearer = HTTPBearer(auto_error=False)


async def _resolve_user(request: Request, response: Response | None = None) -> CurrentUser | None:
    """手动解析登录态(避免直接调用带 Depends 的 get_current_user)。

    返回 None 表示未登录 / token 无效;前端据此得到 SSE auth error 事件。
    若 response 传入且 token 剩余 <10min, 自动续期 Cookie(滑动过期)。
    """
    token = request.cookies.get(ACCESS_COOKIE)
    # 仅记录 cookie 名(不记录值), 避免泄露 token 明文; 仅用于排查鉴权链路
    cookie_keys = list(request.cookies.keys())
    logger.info("[auth] cookie 内 token: %s | cookie 名=%s",
                "FOUND" if token else "NONE", cookie_keys)
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
        user = CurrentUser(int(payload["sub"]), payload.get("role", "user"))
        # 滑动过期
        if response is not None:
            exp = payload.get("exp", 0)
            now_ts = datetime.now(timezone.utc).timestamp()
            if exp - now_ts < 600:
                new_token = create_access_token(user.id, user.role)
                _set_access_cookie(response, new_token)
        return user
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


def _strip_trail(content: str) -> str | None:
    """去除思考过程(trail JSON) — 历史消息上下文不需要。返回 None 表示整条消息应丢弃。"""
    if not content:
        return None
    # trail: {"type":"trail","events":[...]}  → 丢弃整条
    # text:  {"type":"text","data":"..."}      → 取 data
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            if obj.get("type") == "trail":
                return None  # 丢弃思考过程消息
            if "data" in obj:
                return str(obj["data"])
    except (json.JSONDecodeError, TypeError):
        pass
    return content  # 纯文本原样返回


async def _build_messages_from_db(db: AsyncSession, conversation_id: int, request: Request) -> list:
    """从 Redis → MySQL 取最近 5 条消息 + 当前 q。

    - Redis 优先(10 min TTL)，命中后刷新 TTL(滑动过期)
    - Redis miss → MySQL 按 id 降序取 5 条，回填 Redis
    - 支持 cursor_id 分页(message_id 作为排序/分页起始位置)
    - user_id 作用域: conversation 已绑定 project→user，无需额外过滤
    """
    cursor_id = request.query_params.get("cursor_id")
    try:
        cursor_id = int(cursor_id) if cursor_id else None
    except (ValueError, TypeError):
        cursor_id = None

    redis_key = f"chat:msgs:{conversation_id}:{cursor_id or 'latest'}"
    r = await get_redis()

    # ── 1) Redis ──
    try:
        cached = await r.get(redis_key)
        if cached:
            messages = json.loads(cached)
            # 过滤旧缓存中残留的 trail 消息
            messages = [m for m in messages if _strip_trail(m.get("content", "")) is not None]
            await r.expire(redis_key, 600)
            logger.info("[chat] Redis命中 conv=%d cursor=%s cnt=%d TTL已刷新",
                       conversation_id, cursor_id or 'latest', len(messages))
            return _append_q(messages, request, from_cache=True)
    except Exception as e:
        logger.warning("[chat] Redis读失败 conv=%d err=%s", conversation_id, e)

    # ── 2) MySQL ──
    messages: list = []
    if conversation_id:
        try:
            stmt = select(Message).where(Message.conversation_id == conversation_id)
            if cursor_id:
                stmt = stmt.where(Message.id < cursor_id)
            stmt = stmt.order_by(desc(Message.id)).limit(5)
            result = await db.execute(stmt)
            db_msgs = list(result.scalars().all())
            db_msgs.reverse()  # 恢复时间线升序
            for m in db_msgs:
                content = m.content or ""
                content = _strip_trail(content)
                if content is None:
                    continue  # trail 消息直接丢弃
                if len(content) > 2000:
                    content = content[:2000] + "...(已截断)"
                messages.append({"role": m.role, "content": content})
            logger.info("[chat] MySQL回源 conv=%d cursor=%s db_total_fetched=%d kept=%d",
                       conversation_id, cursor_id or 'latest', len(messages))
        except Exception as e:
            logger.warning("[chat] MySQL查询失败 conv=%d err=%s", conversation_id, e)

    # ── 3) 回填 Redis ──
    if messages:
        try:
            await r.set(redis_key, json.dumps(messages, ensure_ascii=False), ex=600)
            logger.info("[chat] Redis回填 conv=%d cursor=%s cnt=%d TTL=600s",
                       conversation_id, cursor_id or 'latest', len(messages))
        except Exception as e:
            logger.warning("[chat] Redis回填失败 conv=%d err=%s", conversation_id, e)

    # ── 4) 追加当前输入 ──
    return _append_q(messages, request)


def _append_q(messages: list, request: Request, *, from_cache: bool = False) -> list:
    """追加当前用户输入(非 cache 路径才需要, cache 已含历史)。"""
    if from_cache:
        return messages
    q = request.query_params.get("q")
    if q:
        if not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != q:
            messages.append({"role": "user", "content": q})
            logger.info("[chat] 追加当前用户输入 q=%.60s", q)
    if not messages:
        raise HTTPException(status_code=400, detail="missing 'q' query param and no history")
    logger.info("[chat] 最终消息数=%d", len(messages))
    return messages


# ── 路由定义 ──


@router.get("/models")
async def list_models():
    """透传 AI 服务的模型列表(匿名可读, 供前端模型选择器使用)。Redis 缓存 300s。"""
    cache_key = "cache:models"
    cached = await cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{settings.ai_service_url}/models")
        r.raise_for_status()
        data = r.json()
    # 回填缓存(300s TTL, 模型列表极少变动)
    await cache_set(cache_key, json.dumps(data, ensure_ascii=False), ttl=1500)
    return data


@router.get("/agents")
async def list_agents():
    """透传 AI 服务的 Agent 注册表(匿名可读, 供前端头像/名称展示)。Redis 缓存 600s。"""
    cache_key = "cache:agents"
    cached = await cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{settings.ai_service_url}/agents")
        r.raise_for_status()
        data = r.json()
    await cache_set(cache_key, json.dumps(data, ensure_ascii=False), ttl=600)
    return data


# ---- 对话摘要(Redis 滑动窗口, v0.9.0: TTL 1d + 过期 MySQL 回退) ----
async def get_summary(conversation_id: int) -> str:
    """读取对话摘要; Redis 过期则从 MySQL 重压(v0.9.0 新增)。"""
    try:
        r = await get_redis()
        val = await r.get(f"summary:{conversation_id}")
        if val:
            return val.decode() if isinstance(val, bytes) else str(val)
    except Exception:
        pass
    # --- Redis MISS: 从 MySQL 回退重压 ---
    try:
        from .db import SessionLocal
        from sqlalchemy import select, text as sa_text
        async with SessionLocal() as db:
            rows = (await db.execute(
                sa_text(
                    "SELECT role, content FROM messages WHERE conversation_id=:cid "
                    "ORDER BY id DESC LIMIT 30"
                ),
                {"cid": conversation_id},
            )).fetchall()
            if not rows:
                return ""
            # 拼接最近消息(最多取最近10轮user+asst)
            parts = []
            count = 0
            for role, content in reversed(rows):
                if count >= 20:
                    break
                parts.append(f"[{role}]: {content[:300]}")
                count += 1
            raw = "\n".join(parts)
            # 调 LLM 重压为 ≤200 字摘要
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                    json={"model": "deepseek-chat",
                          "messages": [{"role":"user","content":
                              f"把对话压缩成 ≤200字 摘要(主题/决策/进度)。\n{raw}"}],
                          "max_tokens": 200, "temperature": 0.3},
                )
                new_summary = resp.json()["choices"][0]["message"]["content"].strip()
            # 写回 Redis
            r2 = await get_redis()
            await r2.setex(f"summary:{conversation_id}", 86400, new_summary[:1000])
            logger.info("[chat] 摘要过期回退重压 conv=%s len=%d", conversation_id, len(new_summary))
            return new_summary
    except Exception as e:
        logger.debug("[chat] 摘要过期回退失败: %s", e)
        return ""


async def save_summary(conversation_id: int, text: str) -> None:
    """写入对话摘要, 1天过期(v0.9.0: 从7天缩短)"""
    try:
        r = await get_redis()
        await r.setex(f"summary:{conversation_id}", 86400, text[:1000])
    except Exception:
        pass


async def maybe_compress_summary(conversation_id: int, model: str, latest_user: str, latest_assistant: str) -> None:
    """每6条消息压缩一次摘要: 旧摘要 + 最新一轮 → LLM → 存 Redis"""
    try:
        r = await get_redis()
        # Redis 计数器: 每轮递增
        cnt = await r.incr(f"summary_cnt:{conversation_id}")
        await r.expire(f"summary_cnt:{conversation_id}", 86400)
        if cnt % 6 != 1:  # 每6条才压缩一次(第1/7/13...条)
            return
        old_summary = await get_summary(conversation_id)
        logger.info("[chat] 触发摘要压缩 conv=%s round=%s", conversation_id, cnt)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                compress_prompt = (
                    "把对话压缩成 ≤200字 摘要(主题/决策/进度)。\n"
                    f"旧摘要: {old_summary or '(无)'}\n"
                    f"用户: {latest_user[:300]}\nAI: {latest_assistant[:500]}\n新摘要: "
                )
                resp = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                    json={"model": "deepseek-chat", "messages": [{"role":"user","content":compress_prompt}],
                          "max_tokens": 200, "temperature": 0.3},
                )
                data = resp.json()
                new_summary = data["choices"][0]["message"]["content"].strip()
                await save_summary(conversation_id, new_summary)
                logger.info("[chat] 对话摘要已更新 conv=%s len=%d", conversation_id, len(new_summary))
        except Exception as e:
            logger.debug("[chat] 摘要压缩失败: %s", e)
    except Exception:
        pass


async def _sync_checkpoint_to_mysql(conversation_id: int, stage: str,
                                     ck_data: dict, progress_pct: int) -> None:
    """后台异步: 将 Redis checkpoint 同步到 MySQL(不阻塞 SSE)"""
    try:
        from .db import SessionLocal as _S
        async with _S() as s:
            conv = await conv_repo.get_by_id(s, conversation_id)
            if conv:
                await conv_repo.update(s, conv,
                    status="paused", checkpoint_stage=stage,
                    checkpoint_data=json.dumps(ck_data, ensure_ascii=False),
                    progress_pct=progress_pct)
    except Exception as e:
        logger.warning("[chat] checkpoint MySQL 同步失败 conv=%s: %s", conversation_id, e)


async def _do_persist(user_id: int, conversation_id: int, tid: str, model: str,
                      terminal_status: str, user_text: str, assistant_text: str,
                      preview_url: str | None = None,
                      qc_result: dict | None = None) -> None:
    """后台异步落库(独立 session, 3 次重试, 全失败入 Redis 错误队列兜底)"""
    from .db import SessionLocal as _S
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            logger.info("[chat] [8/8] 后台落库 trace=%s attempt=%d", tid, attempt + 1)
            async with _S() as s:
                await finish_trace(s, tid, terminal_status, max(0, len(assistant_text) // 4))
                await _persist_conversation(s, user_id, conversation_id, model, user_text, assistant_text, tid, preview_url)
                # 后置 QC 三裁判结果落库(幂等 upsert by trace_id)
                if qc_result is not None:
                    try:
                        await qc_score_repo.upsert(
                            s, tid, model, conversation_id, qc_result)
                        logger.info("[chat] QC 已落库 trace=%s overall=%s",
                                    tid, qc_result.get("overall"))
                        # 同步写入统计系统(满足"新增功能必接统计"约定): 计数/均分/复核率/每维
                        await record_qc(qc_result)
                    except Exception as qc_e:  # noqa: BLE001
                        logger.warning("[chat] QC 落库失败(跳过) trace=%s: %s", tid, qc_e)
                # 流结束(done/aborted/error)清理 Redis checkpoint; paused 保留供恢复
                if terminal_status != "paused":
                    await ck_delete(conversation_id)
                logger.info("[chat] 后台落库完成 trace=%s", tid)
                # 落库成功后: 触发对话摘要压缩(异步, 失败不影响主流程)
                asyncio.create_task(maybe_compress_summary(conversation_id, model, user_text, assistant_text))
                return  # 成功, 直接返回
        except Exception as e:
            last_err = e
            logger.warning("[chat] 后台落库失败 trace=%s attempt=%d: %s", tid, attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s 指数退避
    # 3 次全部失败: 入 Redis 错误队列兜底
    logger.error("[chat] 后台落库最终失败(3次) trace=%s: %s", tid, last_err)
    try:
        await enqueue_write_error({
            "type": "persist_chat",
            "trace_id": tid,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "model": model,
            "terminal_status": terminal_status,
            "user_text": user_text,
            "assistant_text": assistant_text,
            "preview_url": preview_url,
            "failed_at": datetime.utcnow().isoformat(),
        })
    except Exception:
        logger.critical("[chat] 错误队列写入也失败 trace=%s — 数据丢失!", tid)


@router.get("/chat")
async def chat(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    model: str = Query("deepseek", description="模型 id"),
    conversation_id: int = Query(..., description="会话 id,必填(前端先建会话)"),
    trace_id: str | None = Query(None, description="前端生成的链路 id,用于取消/续传"),
    after: str | None = Query(None, description="断点续传:仅回放该 stream id 之后的增量(留空=全量回放)"),
    resume: bool = Query(False, description="从断点恢复(设 true 则注入 checkpoint_data)"),
    correct: bool = Query(False, description="更正模式(基于上次结果微调)"),
):
    """登录后 SSE 对话端点(文档 §3.7 / §5 / §2.1 / §15.3)。

    前端: GET /api/chat?model=<id>&conversation_id=<cid>&messages=<JSON>&trace_id=<id>
          (需携带登录 Cookie)
    业务: 校验 JWT → 翻译成 POST {ai}/generate,逐帧透传 SSE → 流结束落库。
    鉴权失败:返回 SSE error 事件(code=AUTH_REQUIRED),而非 JSON 401,
    以便前端 EventSource 识别并主动弹出登录框。
    """
    # --- 1) 鉴权 ---
    # Cookie → URL token(SSE 兜底) → Bearer
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        token = request.query_params.get("token")
    if not token:
        try:
            creds = await _bearer(request)
            if creds is not None:
                token = creds.credentials
        except Exception:
            token = None
    user: CurrentUser | None = None
    if token:
        try:
            payload = decode_token(token)
            if payload.get("type") == "access":
                user = CurrentUser(int(payload["sub"]), payload.get("role", "user"))
        except Exception:
            pass
    if user is None:
        logger.info("[chat] 鉴权失败 — 未登录或 token 无效")
        return _sse_auth_error()
    logger.info("[chat] [1/8] 鉴权通过 user=%s role=%s", user.id, user.role)

    # --- 2) 从 DB 取最近 20 条消息 + 当前 q ---
    messages = await _build_messages_from_db(db, conversation_id, request)
    # 清洗用户输入(防 XSS/注入) + 打 _msg_id 给 AI 侧向量索引
    for i, m in enumerate(messages):
        m["_msg_id"] = conversation_id * 1000 + i + 1
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            m["content"] = _sanitize_input(m["content"])
    tid = trace_id or uuid.uuid4().hex
    # 取当前用户消息: 优先 q 参数, 其次遍历取最后一条 user
    user_text = request.query_params.get("q") or ""
    if not user_text:
        for m in messages:
            if m.get("role") == "user":
                user_text = m.get("content", "") or ""  # 遍历到尾, 最后一条覆盖
    logger.info("[chat] [2/8] 解析消息 trace=%s conv=%s model=%s msgs=%d input=%.80s",
                tid, conversation_id, model, len(messages), user_text)
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = m.get("content", "")
        logger.info("[chat]   消息[%d] role=%s len=%d content=%.500s", i, role, len(content), content)

    # --- 3) 配额检查 ---
    plan = (
        await db.execute(select(User.plan).where(User.id == user.id))
    ).scalar_one_or_none() or "free"
    allowed, remaining = await consume_daily_quota(user.id, plan)
    if not allowed:
        logger.warning("[chat] 配额用尽 user=%s plan=%s", user.id, plan)
        await record_error("rate_limited")
        return _sse_error_frame(
            "RATE_LIMITED",
            f"今日生成次数已用尽（{settings.free_daily_quota} 次/天），请明日再来或升级套餐",
        )
    logger.info("[chat] [3/8] 配额检查通过 plan=%s 剩余=%s", plan, remaining)

    # --- 4) 计量 + Trace ---
    await record_model_usage(user.id, model)
    await create_trace(db, user.id, conversation_id, tid, model)
    logger.info("[chat] [4/8] 计量已记录 + trace=%s 已创建", tid)

    payload = {"model_id": model, "messages": messages, "trace_id": tid,
               "conversation_id": conversation_id, "user_id": user.id, "project_id": None}
    # 前端二次确认回传(安全 confirm 通过后带 confirmed=1 重发, Worker 据此跳过拦截)
    confirmed = request.query_params.get("confirmed")
    if confirmed in ("1", "true", "True"):
        payload["confirmed"] = True
        logger.info("[chat] 二次确认已通过, 跳过安全拦截")
    # 多意图编排: 前端回传已确认的中风险子任务 id(逗号分隔)
    confirmed_subtasks = request.query_params.get("confirmed_subtasks")
    if confirmed_subtasks:
        payload["confirmed_subtasks"] = [s.strip() for s in confirmed_subtasks.split(",") if s.strip()]
        logger.info("[chat] 已确认中风险子任务: %s", payload["confirmed_subtasks"])
    # 前端上下文检测 + Redis 对话摘要
    ctx = request.query_params.get("context_hint")
    if ctx:
        payload["context_hint"] = ctx
        logger.info("[chat] 上下文检测 frontend_context=%.80s", ctx)
        await record_context_detection("webllm")
    else:
        await record_context_detection("chroma")
    summary = await get_summary(conversation_id)
    if summary:
        payload["conversation_summary"] = summary
    gen_url = f"{settings.ai_service_url}/generate"
    # 项目上下文: 状态+需求文档+系统prompt+硬约束
    # 注意: 必须先取 Conversation 得到 project_id(此前此处直接引用 conv 而未定义,
    # 整段被 try/except 吞掉, 导致 project_status/requirement_doc 从未下发 —— 已修正)
    try:
        conv = await db.get(Conversation, conversation_id)
        project_id = conv.project_id if conv else None
        if project_id:
            proj = await db.get(Project, project_id)
            if proj:
                payload["project_status"] = proj.status or "draft"
                if proj.requirement_doc:
                    try:
                        payload["requirement_doc"] = json.loads(proj.requirement_doc)
                    except Exception:
                        pass
                # 项目系统 prompt(Tier 1): 注入 skill 执行上下文
                payload["project_system_prompt"] = proj.system_prompt or ""
                # 项目硬约束(Tier 2): 从 system_prompt 的 --forbid: 行抽取结构化词
                payload["project_constraints"] = _parse_project_forbid(proj.system_prompt)
    except Exception as e:
        logger.warning("[chat] 项目上下文获取失败 conv=%s: %s", conversation_id, e)
    # 断点续跑: 方案确认后→锁死 generate_site, 防止重新进需求分析
    use_skill_override = False
    if resume:
        ck_redis = await ck_get(conversation_id)
        if ck_redis and ck_redis.get("stage") == "await_confirm":
            payload["skill"] = "generate_site"
            use_skill_override = True
    if after:
        from urllib.parse import urlencode
        gen_url += "?" + urlencode({"after": after})

    # 断点续跑(§7): 注入 checkpoint_data + resume_mode。Redis 优先, MySQL 兜底。
    if resume:
        ck_data = None
        ck_stage = "?"
        # ① 先查 Redis(热路径, <1ms)
        ck_redis = await ck_get(conversation_id)
        if ck_redis and ck_redis.get("status") == "paused":
            ck_data = ck_redis.get("data")
            ck_stage = ck_redis.get("stage", "?")
            logger.info("[chat] 断点恢复(Redis) trace=%s stage=%s", tid, ck_stage)
        else:
            # ② Redis 未命中, 回退 MySQL
            conv = await db.get(Conversation, conversation_id)
            if conv and conv.checkpoint_data and conv.status == "paused":
                try:
                    ck_data = json.loads(conv.checkpoint_data)
                    ck_stage = conv.checkpoint_stage or "?"
                    logger.info("[chat] 断点恢复(MySQL) trace=%s stage=%s", tid, ck_stage)
                except json.JSONDecodeError:
                    logger.warning("[chat] checkpoint_data 解析失败, 降级为普通对话")
        if ck_data:
            payload["checkpoint"] = ck_data
            payload["resume_mode"] = "correct" if correct else "resume"
            if isinstance(ck_data, dict) and ck_data.get("messages"):
                payload["messages"] = ck_data["messages"] + messages
            logger.info("[chat] 断点恢复 trace=%s stage=%s mode=%s", tid, ck_stage, payload.get("resume_mode"))

    async def publisher():
        # read 不超时(生成可能持续数分钟),connect 给 10s
        timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
        assistant_parts: list[str] = []
        preview_url: str | None = None  # 捕获预览直链(供分享「复制预览链接」使用)
        qc_result: dict | None = None  # 捕获后置 QC 三裁判聚合结果(供落库 + 前端展示)
        event_seq: int = 0  # 结构化事件序号(供回放重建时间线)
        terminal_status: str = "done"
        captured_level1: str = "unknown"  # 从 intent 事件捕获, 供统计
        event_counts: dict[str, int] = {}  # 各类 SSE 事件计数(供日志)
        logger.info("[chat] [5/8] 连接 AI 服务 %s", gen_url)
        logger.info("[chat] ▸ 请求体 model=%s resume=%s after=%s msgs=%d checkpoint=%s",
                     model, resume, after or "-",
                     len(payload.get("messages", [])),
                     "有" if payload.get("checkpoint") else "无")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                try:
                    async with client.stream(
                        "POST",
                        gen_url,
                        json=payload,
                    ) as resp:
                        if resp.status_code >= 400:
                            err_text = await resp.aread()
                            code, message = _map_upstream_error(resp.status_code, err_text)
                            terminal_status = "error"
                            logger.warning(
                                "[chat] AI 服务返回 %s: %s", resp.status_code, message
                            )
                            yield _error_frame(code, message)
                            return
                        logger.info("[chat] [6/8] AI 服务已连接, 开始接收事件流")
                        event = None
                        data_parts: list[str] = []
                        async for raw_line in resp.aiter_lines():
                            if raw_line == "":
                                if event is not None or data_parts:
                                    data = "\n".join(data_parts)
                                    event_counts[event or "message"] = (
                                        event_counts.get(event or "message", 0) + 1
                                    )
                                    # 统一解析 payload / 初始化 stage(供下方所有事件分支共用,
                                    # 避免 checkpoint/paused/unsupported/intent 分支引用未初始化变量)
                                    payload_obj = None
                                    stage = None
                                    if event != "token" and data:
                                        try:
                                            payload_obj = json.loads(data)
                                        except Exception:
                                            payload_obj = None
                                    if event == "token":
                                        # AI 服务 token 数据格式为 JSON({"data": "text"}), 需提取纯文本
                                        try:
                                            tok = json.loads(data)
                                            text = tok.get("data", data) if isinstance(tok, dict) else data
                                        except (json.JSONDecodeError, TypeError):
                                            text = data
                                        assistant_parts.append(text)
                                    elif event in ("node", "think", "plan", "error", "aborted", "degraded", "preview"):
                                        if isinstance(payload_obj, dict) and event in ("node", "think"):
                                            stage = payload_obj.get("stage")
                                        if event == "node" and isinstance(payload_obj, dict):
                                            if payload_obj.get("stage") == "preview" and payload_obj.get("url"):
                                                preview_url = payload_obj["url"]
                                        if event == "preview" and isinstance(payload_obj, dict) and payload_obj.get("url"):
                                            preview_url = payload_obj["url"]
                                        event_seq += 1
                                        # trace_event 暂存, 由 finally 批量写入
                                        if event == "aborted":
                                            terminal_status = "aborted"
                                        elif event == "error":
                                            terminal_status = "error"
                                    elif event == "unsupported":
                                        terminal_status = "unsupported"
                                        await record_unsupported(user.id, user_text)
                                        await record_intent_decision("unsupported")
                                    elif event == "checkpoint" and isinstance(payload_obj, dict):
                                        # 断点续跑(§7): 写 Redis(不阻塞 SSE), MySQL 异步同步
                                        stage = payload_obj.get("stage", "?")
                                        ck_data = payload_obj.get("data", {})
                                        progress_pct = {
                                            "planner_done": 25, "coder_done": 65,
                                            "reviewer_r0": 75, "reviewer_r1": 85, "reviewer_r2": 95,
                                        }.get(stage, 50)
                                        # 主路径: Redis( <1ms, 不阻塞 SSE 流)
                                        await ck_set(conversation_id, stage, ck_data, progress_pct)
                                        logger.info("[chat] 断点→Redis conv=%s stage=%s", conversation_id, stage)
                                        # 异步同步到 MySQL(后台, 不阻塞)
                                        asyncio.create_task(_sync_checkpoint_to_mysql(
                                            conversation_id, stage, ck_data, progress_pct))
                                    elif event == "paused":
                                        terminal_status = "paused"
                                        # 方案确认暂停: 保存阶段信息到 checkpoint, 恢复时锁死 generate_site
                                        if isinstance(payload_obj, dict) and payload_obj.get("stage") == "await_confirm":
                                            await ck_set(conversation_id, "await_confirm",
                                                        {"title": payload_obj.get("plan_title", ""),
                                                         "goal": payload_obj.get("plan_goal", ""),
                                                         "steps": payload_obj.get("plan_steps", [])},
                                                        30)
                                    elif event == "intent" and isinstance(payload_obj, dict):
                                        # 两级意图记录(供管理后台系统分析)
                                        l1 = payload_obj.get("level1") or payload_obj.get("intent") or "unknown"
                                        l2 = payload_obj.get("level2") or "unknown"
                                        captured_level1 = l1
                                        await record_intent_result(l1, l2, True)
                                        # 决策分布(含 block/confirm/options/route/fallback)
                                        await record_intent_decision(
                                            payload_obj.get("decision") or "route",
                                            skill=payload_obj.get("selected_skill") or "",
                                            risk=payload_obj.get("risk_level") or "low",
                                        )
                                        logger.info(
                                            "[chat] 意图 %s/%s label=%s industry=%s confidence=%s",
                                            l1, l2,
                                            payload_obj.get("label", "-"),
                                            payload_obj.get("industry", "-"),
                                            payload_obj.get("confidence", "-"),
                                        )
                                    elif event in ("block", "confirm", "options"):
                                        # 决策统计: 安全拦截/二次确认/多选项(未确认态)
                                        if isinstance(payload_obj, dict):
                                            await record_intent_decision(
                                                event,
                                                skill=payload_obj.get("skill")
                                                or payload_obj.get("selected_skill") or "",
                                            )
                                    elif event == "qc" and isinstance(payload_obj, dict):
                                        # 后置 QC 三裁判结果(v0.8.5 M1): 捕获供落库 + 前端气泡展示
                                        qc_result = payload_obj
                                        logger.info(
                                            "[chat] ◇ QC 结果 trace=%s overall=%s needs_review=%s",
                                            tid, payload_obj.get("overall"), payload_obj.get("needs_review"),
                                        )
                                    logger.info(
                                            "[chat] ◇ SSE #%d type=%s stage=%s data=%.200s",
                                            event_seq, event, stage or "-", data,
                                        )
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
                except httpx.HTTPError as e:
                    logger.warning("[chat] AI 连接异常: %s", e)
                    terminal_status = "error"
                    await record_error("upstream_error")
                    yield _error_frame("UPSTREAM_ERROR", "AI 服务暂时不可用，请稍后重试")
                    return
        finally:
            # 获取完整 assistant 文本并立即落库(不依赖 finally 内异步)
            assistant_full_text = "".join(assistant_parts)
            approx_tokens = max(0, len(assistant_full_text) // 4)
            logger.info(
                "[chat] [7/8] 流结束 trace=%s 状态=%s events=%d tokens≈%d preview=%s output=%d字符",
                tid, terminal_status, sum(event_counts.values()),
                approx_tokens, bool(preview_url), len(assistant_full_text),
            )
            if assistant_full_text:
                logger.info("[chat]   响应预览(首500字): %.500s", assistant_full_text)
            # 事件分布
            if event_counts:
                evt_detail = " ".join(f"{k}={v}" for k, v in sorted(event_counts.items()))
                logger.info("[chat]   事件分布: %s", evt_detail)
            # 后台落库任务(独立 session + 重试, 不在 generator finally 中同步等待)
            logger.info("[chat] [8/8] 启动后台落库 trace=%s user_text=%.50s", tid, user_text)
            asyncio.create_task(_do_persist(
                user_id=user.id,
                conversation_id=conversation_id,
                tid=tid,
                model=model,
                terminal_status=terminal_status,
                user_text=user_text,
                assistant_text=assistant_full_text,
                preview_url=preview_url,
                qc_result=qc_result,
            ))

    return StreamingResponse(
        publisher(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": tid,  # 便于前端在没自带 trace_id 时也能取消
        },
    )


def _normalize_assistant_text(text: str) -> str:
    """拆解 JSON 碎片 {"data":"a"}{"data":"b"} → "ab"。
    若 text 是纯文本或结构化 JSON 则原样返回。
    """
    if not text or not text.startswith('{"data":'):
        return text
    # 多段拼接
    parts = []
    pos = 0
    while True:
        start = text.find('{"data":', pos)
        if start == -1:
            break
        end = text.find('}', start)
        if end == -1:
            break
        try:
            seg = json.loads(text[start:end + 1])
            if isinstance(seg, dict) and "data" in seg:
                parts.append(seg["data"])
        except Exception:
            pass
        pos = end + 1
    if parts:
        return "".join(parts)
    # 单层
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "data" in obj:
            return obj.get("data", text)
    except Exception:
        pass
    return text


async def _persist_conversation(
    db: AsyncSession,
    user_id: int,
    conversation_id: int,
    model: str,
    user_text: str,
    assistant_text: str,
    trace_id: str,
    preview_url: str | None = None,
) -> None:
    """SSE 结束后落库。build 类消息走 Artifact+结构化 JSON, chat 类存纯文本。"""
    # 归一化: 拆解 {"data":"x"}{"data":"y"}... → "xy..." (兜底防 AI 服务旧格式)
    assistant_text = _normalize_assistant_text(assistant_text)
    logger.info("[chat] _persist 调用 trace=%s conv=%s uid=%s user_text=%.50s alen=%s preview=%s",
                trace_id, conversation_id, user_id, user_text, len(assistant_text), bool(preview_url))
    conv = await conv_repo.get_by(db, id=conversation_id, user_id=user_id)
    if conv is None:
        logger.warning("[chat] 落库失败: 会话不存在 conv=%s user=%s", conversation_id, user_id)
        return

    # user 消息跳过重复(重连防重)
    user_msg = await message_repo.get_by_trace(db, trace_id, "user")
    if user_msg is None:
        db.add(Message(
            conversation_id=conv.id, role="user",
            content=user_text, model_id=model, trace_id=trace_id,
        ))

    # assistant 消息: 按内容分两路
    is_html = assistant_text and ("<html" in assistant_text[:500].lower() or "<!doctype" in assistant_text[:500].lower())
    if is_html:
        # ---- 建站/代码生成: 始终建 Artifact(COS 失败也用 srcdoc 兜底) ----
        repo = "site"
        art = Artifact(
            project_id=conv.project_id or 0,
            conversation_id=conv.id,
            trace_id=trace_id,
            title=conv.title or user_text[:20],
            repo=repo,
            files=[{"name": "index.html", "size": len(assistant_text.encode("utf-8")), "content": assistant_text}],
            preview_url=preview_url or "",
            download_url=preview_url or "",
            status="done",  # HTML 已可用(srcdoc 兜底, COS 链接可有可无)
        )
        db.add(art)
        await db.flush()
        content_obj = {
            "type": repo,
            "artifact_id": art.id,
            "title": art.title or "",
            "preview_url": preview_url or "",
            "download_url": preview_url or "",
            "files": art.files or [],
        }
        await message_repo.upsert_assistant(db, conv.id, trace_id, json.dumps(content_obj, ensure_ascii=False), model)
        if preview_url:
            proj = await db.get(Project, conv.project_id)
            if proj is not None:
                proj.preview_url = preview_url
        logger.info("[chat] Artifact 已创建 id=%s repo=%s preview=%s fallback=srcdoc", art.id, repo, preview_url or "(无)")
    else:
        # ---- 闲聊/文档: 纯文本 ----
        if assistant_text:
            await message_repo.upsert_assistant(db, conv.id, trace_id, assistant_text, model)

    if not conv.title and user_text:
        conv.title = user_text[:20]
        logger.info("[chat] 自动设置会话标题 conv=%s title=%.20s", conv.id, user_text)
    conv.updated_at = datetime.utcnow()
    await db.commit()
    logger.info("[chat] 消息落库成功 conv=%s", conv.id)
    # 失效消息历史缓存(含 cursor 分页的所有变体), 使下一轮对话从 MySQL 重取最新上下文。
    # 否则 Redis 旧缓存(600s TTL)会让后续消息不可见, AI 上下文永远停留在首条消息。
    try:
        r = await get_redis()
        keys = await r.keys(f"chat:msgs:{conversation_id}:*")
        if keys:
            await r.delete(*keys)
            logger.info("[chat] 消息历史缓存已失效 conv=%s keys=%d", conversation_id, len(keys))
    except Exception as e:
        logger.warning("[chat] 失效消息缓存失败 conv=%s: %s", conversation_id, e)


@router.post("/feedback")
async def post_feedback(
    req: FeedbackReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用户提交评价(气泡内星级 + 六维子星)。落库并写入统计系统。"""
    fb = await feedback_repo.upsert(
        db, user.id, req.trace_id, req.conversation_id, req.rating, req.comment,
        dimensions=req.dimensions,
    )
    logger.info("[chat] 收到用户评价 user=%s trace=%s rating=%s 含维度=%s",
                user.id, req.trace_id, req.rating, bool(req.dimensions))
    # 同步统计: 提交次数 / 平均评分 / 含六维子星占比
    await record_feedback(req.rating, bool(req.dimensions))
    return {"ok": True, "rating": fb.rating}


@router.post("/cancel")
async def cancel(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """级联取消(C1):转发到 AI 服务的 /cancel。需登录且为 trace 所有者(防越权取消他人生成)。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    trace_id = body.get("trace_id") or (await request.body()).decode("utf-8", "ignore")
    if not trace_id:
        raise HTTPException(status_code=400, detail="missing trace_id")
    # 归属校验:trace_id 必须属于当前用户,否则 403。
    # 用 traces 表(user_id + trace_id 是规范的归属记录);若该 trace 尚未落库
    # (极端竞态),owner_id 为 None → 放行(不误杀),避免取消不了自己的生成。
    from sqlmodel import select as _select
    owner_id = (await db.execute(
        _select(Trace.user_id).where(Trace.trace_id == trace_id).limit(1)
    )).scalar_one_or_none()
    if owner_id is not None and owner_id != user.id:
        raise HTTPException(status_code=403, detail="not owner of this trace")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{settings.ai_service_url}/cancel", json={"trace_id": trace_id})
        return r.json()

# reload v99


