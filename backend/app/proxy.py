"""生成代理:统一单进程应用的唯一对外对话入口(业务层 + 推理层同进程)。

- 前端永不直接触达 AI 服务。
- 鉴权门禁:GET /api/chat 需登录(从 HttpOnly Cookie 取 JWT);未登录时下发
  SSE error 事件(code=AUTH_REQUIRED, message="Missing authentication"),而非 JSON 401,
  以便前端 EventSource 识别并主动弹出登录框(文档 §3.7 / §5 / §2.1)。
- 单进程合并后:本模块不再用 httpx 把请求转发给独立的 AI 服务。
  取而代之 —— 直接把 job 投递给同进程的 Worker 队列,订阅同一个
  `gen:stream:<tid>` 频道,把 Worker 产出的事件字典原样序列化为 SSE 帧吐出。
  彻底消除了「双进程 SSE 二次转发 + business→ai_service httpx 代理」这一历史包袱。
- 鉴权 / 限流 / 用量计量在此拦截(已登录用户按真实 user_id 计量)。
- **落库(M1)**:/api/chat 入参新增 conversation_id;SSE 流结束(或中断)时,
  把首条用户消息 + AI 完整回复双写进 Message,并更新 Conversation.updated_at,
  首条时自动生成会话标题。落库失败不阻塞已完成的流。

SSE 透传策略:Worker 经队列产出 `{"event":..,"data":..}` 字典事件。
我们用 to_sse() 序列化为标准 SSE 文本帧(event:/data: 行 + 空行),
保证 think/token/node/preview/done/error/aborted/degraded 一字不差地转发,
兼容前端 EventSource,同时收集 token 帧内容用于落库。
"""

import json
import logging
import time
import uuid
from typing import Any
from datetime import datetime, timezone

import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .analytics import (  # 业务端统计接入(§「新增功能必接统计」约定)
    record_error,
    record_intent_result,
    record_model_detail,
    record_skill_outcome,
    record_user_active,
    record_intent_decision,
)
from .analytics import record_context_detection, record_requirement_doc
from .analytics import record_feedback
from .cache import cache_get, cache_set, ck_delete, ck_get, ck_set, enqueue_write_error, get_redis
from .config import settings
from .db import get_db
from .metrics import consume_daily_quota, record_model_usage, record_model_tokens, record_api_latency, record_unsupported
from .models import Artifact, Conversation, Message, Project, Trace, User
from .repos.business_repos import conv_repo, message_repo, artifact_repo
from .repos.trace_repos import feedback_repo, qc_score_repo, trace_repo
from .schemas import FeedbackReq
from .security import ACCESS_COOKIE, CurrentUser, _set_access_cookie, create_access_token, decode_token, get_current_user
# 单进程合并:直接复用同进程的推理队列(取代 httpx 转发)
from .agent.core.queue import get_queue
from .tracing import append_trace_event, create_trace, finish_trace, log_usage
from .user_state import touch_user_state, get_user_state, reset_user_state

# 续接守卫: 刷新/重连(无新输入)且流尚未建立时, 轮询等待 in-flight Worker 建流的最长秒数。
# 通常 Worker 在数秒内产出首事件(XADD 建流), 仅当 Worker 真已结束/死亡才超时干净收尾。
STREAM_WAIT_SECONDS = 30

import html as _html
import re


# ---------- 内容安全 ----------
_SENSITIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"<script[^>]*>.*?</script>", re.I | re.S),
    re.compile(r"javascript\s*:", re.I),
    re.compile(r"on\w+\s*=", re.I),
    re.compile(r"<iframe[^>]*>", re.I),
    re.compile(r"<object[^>]*>", re.I),
    re.compile(r"<embed[^>]*>", re.I),
    re.compile(r"data\s*:\s*text/html", re.I),
    re.compile(r"expression\s*\(.*\)", re.I),
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

logger = logging.getLogger("business.proxy")

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


def _sse_done_event() -> StreamingResponse:
    """v4 续接守卫: 带 after 但流已消失(过期/从未生成)时, 直接下发 done 事件干净收尾,
    让前端 resumeStream 的 onDone 正常触发(复位 generating/清理 userStatus), 绝不空 q 重入队。
    """
    async def gen():
        yield b"event: done\ndata: {}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    ttl = settings.chat_recent_redis_ttl  # 30min 滑动窗口（原硬编码 600s）

    # ── 1) Redis ──
    try:
        cached = await r.get(redis_key)
        if cached:
            messages = json.loads(cached)
            # 过滤旧缓存中残留的 trail 消息
            messages = [m for m in messages if _strip_trail(m.get("content", "")) is not None]
            await r.expire(redis_key, ttl)
            logger.info("[chat] Redis命中 conv=%d cursor=%s cnt=%d TTL已刷新(%ds)",
                       conversation_id, cursor_id or 'latest', len(messages), ttl)
            # C3 修复: 缓存命中仍须补当前 q(缓存只存历史)
            return await _append_q(messages, request)
    except Exception as e:
        logger.warning("[chat] Redis读失败 conv=%d err=%s", conversation_id, e)

    # ── 2) MySQL ──
    messages: list = []
    if conversation_id:
        try:
            stmt = select(Message).where(Message.conversation_id == conversation_id)
            if cursor_id:
                stmt = stmt.where(Message.id < cursor_id)
            stmt = stmt.order_by(desc(Message.id)).limit(settings.chat_recent_limit)
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
            await r.set(redis_key, json.dumps(messages, ensure_ascii=False), ex=ttl)
            logger.info("[chat] Redis回填 conv=%d cursor=%s cnt=%d TTL=%ds",
                       conversation_id, cursor_id or 'latest', len(messages), ttl)
        except Exception as e:
            logger.warning("[chat] Redis回填失败 conv=%d err=%s", conversation_id, e)

    # ── 4) 追加当前输入 ──
    return await _append_q(messages, request)


async def _append_q(messages: list, request: Request) -> list:
    """追加当前用户输入(q)。

    缓存命中/未命中两条路径都会走到这里: Redis 缓存只存历史(不含当前 q),
    因此无论来源都必须补上本次用户输入。末尾去重逻辑保证:若历史最后一条已是
    相同 user q,则不重复追加(C3 修复:此前 from_cache=True 短路导致 2nd turn 起
    当前输入丢失, LLM 上下文缺最新用户消息)。
    """
    q = request.query_params.get("q")
    resume = request.query_params.get("resume", "").lower() in ("true", "1")
    if q:
        if not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != q:
            messages.append({"role": "user", "content": q})
            logger.info("[chat] 追加当前用户输入 q=%.60s", q)
    if not messages:
        # 续接/回放模式允许空 messages: F5 刷新在生成中途断开时, 若尚未收到任何 id 帧则
        # after 游标缺失, 但只要存在在途流(stream_exists)就应全量回放, 而非 400(修复 #V2)。
        _tid = request.query_params.get("trace_id")
        _replay = bool(resume or request.query_params.get("after"))
        if not _replay and _tid:
            try:
                _replay = await get_queue().stream_exists(_tid)
            except Exception:
                _replay = False
        if _replay:
            logger.info("[chat] 续接/回放模式: 无历史消息, 允许空 messages 进行流回放 trace=%s", _tid)
        else:
            raise HTTPException(status_code=400, detail="missing 'q' query param and no history")
    logger.info("[chat] 最终消息数=%d", len(messages))
    return messages


# ── 路由定义 ──
# 注: /models 与 /agents 已迁移到 main.py(单进程直读 Provider/SkillRegistry, 不再经
#     httpx 转发到已不存在的 ai_service —— 旧转发会在合并后 500)。见 main.py。


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
                    json={"model": "deepseek-v4-flash",
                          "messages": [{"role":"user","content":
                              f"把对话压缩成 ≤200字 摘要(主题/决策/进度)。\n{raw}"}],
                          "max_tokens": 200, "temperature": 0.1},
                )
                new_summary = resp.json()["choices"][0]["message"]["content"].strip()
            # 写回 Redis
            r2 = await get_redis()
            await r2.setex(f"summary:{conversation_id}", settings.conversation_summary_ttl, new_summary[:1000])
            logger.info("[chat] 摘要过期回退重压 conv=%s len=%d", conversation_id, len(new_summary))
            from .analytics import record_summary_fallback
            await record_summary_fallback(conversation_id)  # v0.9.0 统计
            return new_summary
    except Exception as e:
        logger.debug("[chat] 摘要过期回退失败: %s", e)
        return ""


async def save_summary(conversation_id: int, text: str) -> None:
    """写入对话摘要, TTL 由 settings.conversation_summary_ttl 控制(默认30min滑动窗口)。
    防御: 不允许写入空串(LLM 可能返回空摘要), 空串直接跳过, 避免覆盖成空。"""
    if not text or not text.strip():
        return
    try:
        r = await get_redis()
        await r.setex(f"summary:{conversation_id}", settings.conversation_summary_ttl, text[:1000])
    except Exception:
        pass


async def maybe_compress_summary(conversation_id: int, model: str, latest_user: str, latest_assistant: str) -> None:
    """每6条消息压缩一次摘要: 旧摘要 + 最新一轮 → LLM → 存 Redis"""
    try:
        r = await get_redis()
        # Redis 计数器: 每轮递增
        cnt = await r.incr(f"summary_cnt:{conversation_id}")
        await r.expire(f"summary_cnt:{conversation_id}", settings.conversation_summary_ttl)
        if cnt % 6 != 1:  # 每6条才压缩一次(第1/7/13...条)
            return
        old_summary = await get_summary(conversation_id)
        logger.info("[chat] 触发摘要压缩 conv=%s round=%s", conversation_id, cnt)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                compress_prompt = (
                    "把对话压缩成 ≤200字 摘要(主题/决策/进度)。\n"
                    "必须保留用户明确提出的网站类型/页面/功能需求原文, 不要丢弃或概括掉关键诉求。\n"
                    f"旧摘要: {old_summary or '(无)'}\n"
                    f"用户: {latest_user[:300]}\nAI: {latest_assistant[:500]}\n新摘要: "
                )
                resp = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                    json={"model": "deepseek-v4-flash", "messages": [{"role":"user","content":compress_prompt}],
                          "max_tokens": 200, "temperature": 0.1},
                )
                data = resp.json()
                new_summary = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
                if not new_summary:
                    # 不允许空摘要覆盖: 回退到旧摘要, 旧摘要也为空则用最新用户消息兜底
                    new_summary = (old_summary or latest_user or "").strip()[:1000]
                    if not new_summary:
                        logger.warning("[chat] 摘要压缩结果为空, 跳过写入 conv=%s", conversation_id)
                        return
                await save_summary(conversation_id, new_summary)
                logger.info("[chat] 对话摘要已更新 conv=%s len=%d", conversation_id, len(new_summary))
        except Exception as e:
            logger.debug("[chat] 摘要压缩失败: %s", e)
    except Exception:
        pass


async def _sync_checkpoint_to_mysql(conversation_id: int, stage: str,
                                     ck_data: dict, progress_pct: int) -> None:
    """将 Redis checkpoint 同步到 MySQL(conversations 表的 checkpoint_* 字段), 供服务重启后恢复断点; 不阻塞 SSE。"""
    from .db import SessionLocal as _S
    try:
        async with _S() as s:
            conv = await conv_repo.get_by_id(s, conversation_id)
            if conv is not None:
                await conv_repo.update(
                    s, conv,
                    checkpoint_stage=stage,
                    checkpoint_data=json.dumps(ck_data, ensure_ascii=False),
                    progress_pct=progress_pct,
                )
    except Exception as e:
        logger.warning("[chat] checkpoint MySQL 同步失败 conv=%s: %s", conversation_id, e)


async def _persist_worker_result(
    trace_id: str,
    conversation_id: int,
    user_id: int,
    model_id: str,
    user_text: str,
    assistant_text: str,
    terminal_status: str = "done",
    skill_name: str = "",
) -> None:
    """Worker 侧兜底落库: 与 SSE publisher 的 _do_persist 互补。
    
    当 SSE 客户端断开时 publisher 的 finally 仍会运行 _do_persist,
    但若 Worker 在 publisher 退出流读取后才产出内容, 该内容无法被 publisher 捕获。
    本函数在 Worker 完成时直接写 DB(幂等 upsert, 不依赖 SSE 通道)。
    """
    from .db import SessionLocal as _S

    try:
        async with _S() as s:
            conv = await conv_repo.get_by(s, id=conversation_id, user_id=user_id)
            if conv is None:
                return
            # user 消息(幂等: 已有则跳过)
            existing_user = await message_repo.get_by_trace(s, trace_id, "user")
            if existing_user is None and user_text:
                s.add(Message(
                    conversation_id=conv.id, role="user",
                    content=user_text, model_id=model_id, trace_id=trace_id,
                ))
            # assistant 消息(幂等 upsert); 失败/中断且无产出时仍补反馈消息
            if assistant_text.strip():
                # 防御: Worker 兜底在 publisher 之后跑时, 若 assistant_text 超长(≥64KB, 多为
                # 建站整站 HTML), 不把整个站点塞进 messages.content(即便已加宽 LONGTEXT 也不该
                # 覆盖 publisher 已写入的结构化气泡元信息)。仅落一条占位提示, 产物以 Artifact/COS 为准。
                _head = assistant_text.lstrip().lower()[:200]
                _is_site = _head.startswith("<!doctype") or "<html" in _head[:32] or "<!--" in _head and "file:" in _head
                if len(assistant_text) >= 64 * 1024 and _is_site:
                    await message_repo.upsert_assistant(
                        s, conv.id, trace_id,
                        "✅ 网站/代码已生成，可在右侧预览面板查看 / 下载。", model_id)
                else:
                    await message_repo.upsert_assistant(s, conv.id, trace_id, assistant_text, model_id)
            elif terminal_status in ("error", "aborted", "unsupported", "paused"):
                try:
                    existing = await message_repo.get_by_trace(s, trace_id, "assistant")
                    if existing is None:
                        _fb = {
                            "error": "⚠️ 生成失败，请稍后重试。",
                            "aborted": "⚠️ 生成已取消。",
                            "unsupported": "⚠️ 当前请求暂不支持。",
                            "paused": "⚠️ 生成已中断（可继续）。",
                        }.get(terminal_status, "⚠️ 生成未产生结果。")
                        await message_repo.upsert_assistant(s, conv.id, trace_id, _fb, model_id)
                except Exception:  # noqa: BLE001
                    pass
            # 更新会话标题(首次)
            if not conv.name and user_text:
                conv.name = user_text[:20]
            conv.updated_at = datetime.utcnow()
            await s.commit()
            logger.info("[chat] Worker 兜底落库完成 trace=%s conv=%s status=%s chars=%d",
                        trace_id, conversation_id, terminal_status, len(assistant_text))
    except Exception as e:
        logger.warning("[chat] Worker 兜底落库失败 trace=%s: %s", trace_id, e)


async def _do_persist(user_id: int, conversation_id: int, tid: str, model: str,
                      terminal_status: str, user_text: str, assistant_text: str,
                      preview_path: str | None = None,
                      files_dict: dict[str, str] | None = None,
                      doc_files: dict[str, dict] | None = None,
                      refined_summary: str = "",
                      deliver_fallback_content: str | None = None,
                      qc_result: dict | None = None,
                      project_id: int | None = None,
                      requirement_doc: dict | None = None) -> None:
    """后台异步落库(独立 session, 3 次重试, 全失败入 Redis 错误队列兜底)"""
    from .db import SessionLocal as _S
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            logger.info("[chat] [8/8] 后台落库 trace=%s attempt=%d", tid, attempt + 1)
            async with _S() as s:
                await finish_trace(s, tid, terminal_status, max(0, len(assistant_text) // 4))
                await _persist_conversation(s, user_id, conversation_id, model, user_text, assistant_text, tid, preview_path, files_dict, refined_summary, terminal_status, doc_files, deliver_fallback_content)
                # 后置 QC 结果落库 MySQL qc_scores(幂等 upsert by trace_id);
                # 不再写入 Redis 统计(无性能考量), 后台「系统分析」QC 面板改读该表。
                if qc_result is not None:
                    try:
                        await qc_score_repo.upsert(
                            s, tid, model, conversation_id, qc_result)
                        logger.info("[chat] QC 已落库 trace=%s overall=%s",
                                    tid, qc_result.get("overall"))
                    except Exception as qc_e:  # noqa: BLE001
                        logger.warning("[chat] QC 落库失败(跳过) trace=%s: %s", tid, qc_e)
                # 需求文档落库(幂等覆盖, 供前端重启后还原"📋 需求文档"条目)
                if requirement_doc is not None and project_id:
                    try:
                        proj = await s.get(Project, project_id)
                        if proj is not None:
                            proj.requirement_doc = json.dumps(requirement_doc, ensure_ascii=False)
                            await s.commit()
                            logger.info("[chat] 需求文档已落库 proj=%s", project_id)
                    except Exception as rd_e:  # noqa: BLE001
                        logger.warning("[chat] 需求文档落库失败(跳过) trace=%s: %s", tid, rd_e)
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
            "preview_path": preview_path,
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
    new_token: str | None = None
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
    # 滑动过期(产品需求): 每次有效对话操作都重新签发 token, 刷新过期时间。
    # 双通道回传: StreamingResponse 挂 X-Access-Token 头(非浏览器客户端轮换),
    # 并 Set-Cookie(浏览器同源 SSE/页面自动携带)。
    new_token = create_access_token(user.id, user.role)
    logger.info("[chat] [1/8] 鉴权通过 user=%s role=%s (滑动续期已签发新 token)", user.id, user.role)

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
    # 计量: 模型用量计数 + (统计) DAU 活跃用户 + (统计) 模型成功率基线(首调用记为成功样本,
    # 后续失败会经 record_model_detail(success=False) 修正, 供「系统分析」模型成功率分布)。
    await record_model_usage(user.id, model)
    await record_user_active(user.id)  # 统计: DAU(按日去重活跃用户) + 人均生成次数
    await record_model_detail(model, success=True, intent="chat")  # 统计: per-model 成功率/意图分布基线
    await create_trace(db, user.id, conversation_id, tid, model)
    # 落到 UsageLog 表, 供「AI 生成质量 → 模型用量」报表按模型统计生成次数。
    # (record_model_usage 仅写 Redis 供 /admin/analytics, 不进 DB, 故此处单独补写 DB。)
    try:
        await log_usage(db, user.id, tid, provider=None, model=model,
                       prompt_tokens=0, completion_tokens=0, cost=0.0)
    except Exception as _lu:
        logger.debug("[chat] log_usage 写入失败(忽略): %s", _lu)
    logger.info("[chat] [4/8] 计量已记录 + trace=%s 已创建", tid)

    payload = {"model_id": model, "messages": messages, "trace_id": tid,
               "conversation_id": conversation_id, "user_id": user.id, "project_id": None}
    # 前端二次确认回传(安全 confirm 通过后带 confirmed=1 重发, Worker 据此跳过拦截)
    confirmed = request.query_params.get("confirmed")
    if confirmed in ("1", "true", "True"):
        payload["confirmed"] = True
        logger.info("[chat] 二次确认已通过, 跳过安全拦截")
    # 澄清回填: 用户在前端 clarify 卡选完并确认后, 带 clarified=1 重发。
    #   答案已随 q 参数作为新用户消息追加(见 _append_q), 后端据此跳过意图分类直接路由。
    clarified = request.query_params.get("clarified")
    if clarified in ("1", "true", "True"):
        payload["clarified"] = True
        logger.info("[chat] 澄清回填 clarified=1(q=%.60s)", user_text)
    # 多意图编排: 前端回传已确认的中风险子任务 id(逗号分隔)
    confirmed_subtasks = request.query_params.get("confirmed_subtasks")
    if confirmed_subtasks:
        payload["confirmed_subtasks"] = [s.strip() for s in confirmed_subtasks.split(",") if s.strip()]
        logger.info("[chat] 已确认中风险子任务: %s", payload["confirmed_subtasks"])
    # 前端指定 skill(confirm 确认后回传: agent_delete/agent_generate_site 等)
    skill_param = request.query_params.get("skill")
    if skill_param:
        payload["skill"] = skill_param
        logger.info("[chat] 前端指定 skill=%s", skill_param)
    # 前端上下文检测 + Redis 对话摘要
    ctx = request.query_params.get("context_hint")
    if ctx:
        payload["context_hint"] = ctx
        logger.info("[chat] 上下文检测 frontend_context=%.80s", ctx)
        await record_context_detection("frontend")  # 上下文来源: 前端直传(非向量检索)
    else:
        await record_context_detection("chroma")  # 上下文来源: 服务端 Chroma 向量检索
    summary = await get_summary(conversation_id)
    if summary:
        payload["conversation_summary"] = summary
    # 单进程合并后: AI 核心与业务同进程, 不再走 httpx 转发。
    # 直接把 job 投递给同进程的队列 Worker(SSE 端点订阅同一 gen:stream 频道回放)。
    if after:
        from urllib.parse import urlencode
        # 续传游标仅用于 SSE 订阅端(after= 透传给 q.subscribe), 不再拼到 http URL。
        logger.info("[chat] 续传游标 after=%s", after)
    # 项目上下文: 状态+需求文档+系统prompt+硬约束
    # 注意: 必须先取 Conversation 得到 project_id(此前此处直接引用 conv 而未定义,
    # 整段被 try/except 吞掉, 导致 project_status/requirement_doc 从未下发 —— 已修正)
    try:
        conv = await db.get(Conversation, conversation_id)
        project_id = conv.project_id if conv else None
        # 关键修复: 将真实的 conv.project_id 回填进 payload, 否则 Worker 始终收不到
        # project_id(3.4 的 project_memory 按 project_id 隔离检索无法生效, auto-start
        # 会话尤其明显——会话已被挂在项目下但 project_id 一直是 None)。
        payload["project_id"] = project_id
        if project_id:
            # 计算产物版本号(第几版): 该项目现有 artifact 数 + 1, 用于 COS 版本化(避免覆盖历史)
            try:
                cnt = (await db.execute(
                    select(func.count()).select_from(Artifact).where(Artifact.project_id == project_id)
                )).scalar() or 0
                payload["version"] = cnt + 1
            except Exception:
                payload["version"] = 1
            proj = await db.get(Project, project_id)
            if proj:
                payload["project_status"] = proj.build_status or "draft"
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
    # v4: 记录「我的状态」入口(上一次在操作哪一个项目/会话 + 当前生成中)
    # 断点复联三场景的权威状态源, 供 GET /api/my-info 恢复前端上下文。
    await touch_user_state(
        user.id,
        current_project_id=project_id,
        current_conversation_id=conversation_id,
        active_trace_id=tid,
        status="running",
        current_stage="router",
        pause_reason=None,
        pending_decision=None,
    )
    # 站已生成信号: 该对话历史中曾产出预览 → 迭代修改类消息应走建站(意图纠偏)
    try:
        payload["site_generated"] = bool(await cache_get(f"site_generated:{conversation_id}"))
    except Exception:
        payload["site_generated"] = False
    # 断点续跑: 方案确认后→锁死 generate_site, 防止重新进需求分析
    use_skill_override = False
    if resume:
        ck_redis = await ck_get(conversation_id)
        if ck_redis and ck_redis.get("stage") == "await_confirm":
            # 🔧 修正: 注册名是 agent_generate_site(带 agent_ 前缀), 原 "generate_site" 在
            #   技能注册表里不存在 → 续跑时 Worker 报 "Skill 'generate_site' 不存在"。
            payload["skill"] = "agent_generate_site"
            use_skill_override = True

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
            try:
                conv = await db.get(Conversation, conversation_id)
                if conv and conv.checkpoint_data and conv.status == "paused":
                    try:
                        ck_data = json.loads(conv.checkpoint_data)
                        ck_stage = conv.checkpoint_stage or "?"
                        logger.info("[chat] 断点恢复(MySQL) trace=%s stage=%s", tid, ck_stage)
                    except json.JSONDecodeError:
                        logger.warning("[chat] checkpoint_data 解析失败, 降级为普通对话")
            except Exception as _ck_e:  # noqa: BLE001
                # 会话不可用(断连/毒化等)时降级为普通对话, 绝不 500(防御 create_trace 之外的异常)
                logger.warning("[chat] 断点恢复(MySQL) 失败, 降级为普通对话 conv=%s: %s", conversation_id, _ck_e)
        if ck_data:
            payload["checkpoint"] = ck_data
            payload["resume_mode"] = "correct" if correct else "resume"
            if isinstance(ck_data, dict) and ck_data.get("messages"):
                payload["messages"] = ck_data["messages"] + messages
            logger.info("[chat] 断点恢复 trace=%s stage=%s mode=%s", tid, ck_stage, payload.get("resume_mode"))

    async def publisher():
        t_start_chat = time.time()  # v0.9.0: API 延迟起点
        # read 不超时(生成可能持续数分钟),connect 给 10s
        timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
        # ── 断连自动取消(C1, 生产级闭环) ──
        # 用"活跃连接集合 clients:<tid>"记录本 SSE 连接的唯一 id。只有最后一个客户端离开时
        # 才置 cancel:<tid>, 避免刷新/多标签时旧连接误伤仍在使用同一 trace_id 的新连接(回放)。
        conn_id = uuid.uuid4().hex
        saw_terminal = False  # 是否已收到终止事件(done/aborted/error/unsupported/paused)
        _paused_locked = False  # 多意图修复: 命中 await_confirm 暂停后锁死 terminal_status=paused,
                                # 避免其他子任务跑完使整条流以 done 收尾, 把刚存的断点 ck 误删(续跑丢断点)
        try:
            r0 = await get_redis()
            before = await r0.scard(f"clients:{tid}")
            await r0.sadd(f"clients:{tid}", conn_id)
            await r0.expire(f"clients:{tid}", 3600)  # 安全 TTL: 进程崩溃未 SREM 时自动清理
            if before == 0:
                # 首个连接(含刷新重连): 清除可能残留的取消标志, 保证回放/续跑不被旧标志误伤
                await r0.delete(f"cancel:{tid}")
                logger.info("[chat] 新连接清除残留 cancel 标志 trace=%s", tid)
        except Exception as e:
            logger.warning("[chat] 连接登记异常 trace=%s: %s", tid, e)

        async def _on_disconnect() -> int:
            """断连清理(v4 续接): 仅移除本连接登记; 不主动暂停 Worker —— 让生成继续跑完,
            前端 F5 后用本地记录的 after 游标重新订阅 /api/chat 即可续接回放(含已产生事件)。

            返回剩余活跃连接数(异常时返回 1, 保守地视为仍有其他客户端)。
            """
            try:
                rc = await get_redis()
                await rc.srem(f"clients:{tid}", conn_id)
                remaining = await rc.scard(f"clients:{tid}")
                logger.info("[chat] 断连清理 trace=%s remaining=%d (不暂停, 允许续接)", tid, remaining)
                return remaining
            except Exception as e:
                logger.warning("[chat] 断连清理异常 trace=%s: %s", tid, e)
                return 1

        assistant_parts: list[str] = []
        preview_path: str | None = None  # P1: 捕获本地产物预览路径(相对 ARTIFACT_DIR, 供前端拼 ${origin}/artifacts/{path})
        files_dict: dict[str, str] = {}  # 多文件产物: {文件名: 相对路径} (P1: 存本地路径, 不再 COS URL)
        doc_files: dict[str, dict] = {}  # 文档产物: {文件名: {name,size,content}} (Fix B #483, doc 技能)
        refined_text: str = ""  # 文字总结: agent 生成完毕后的自然语言反馈(v1.2.2)
        deliver_fallback_content: str | None = None  # E(#488): COS 不可用时站点 HTML 兜底内容(新链路极少触发)
        qc_result: dict | None = None  # 捕获后置 QC 三裁判聚合结果(供落库 + 前端展示)
        requirement_doc_captured: dict | None = None  # 捕获需求文档(供落库 + 前端重启还原)
        event_seq: int = 0  # 结构化事件序号(供回放重建时间线)
        terminal_status: str = "running"
        captured_level1: str = "unknown"  # 从 intent 事件捕获, 供统计
        event_counts: dict[str, int] = {}  # 各类 SSE 事件计数(供日志)
        logger.info("[chat] [5/8] 同进程 Worker 队列就绪 trace=%s", tid)
        logger.info("[chat] ▸ 请求体 model=%s resume=%s after=%s msgs=%d checkpoint=%s",
                     model, resume, after or "-",
                     len(payload.get("messages", [])),
                     "有" if payload.get("checkpoint") else "无")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                try:
                    # 单进程合并: 不再经 httpx 转发, 直接把 job 投递给同进程 Worker 队列,
                    # 订阅同一 gen:stream:<tid> 频道, 把事件字典序列化为 SSE 帧透传(零逻辑改动)。
                    q = get_queue()
                    if resume:
                        # G5 修复: await_confirm 阶段已 open_channel 建流 → stream_exists(tid) 恒 True,
                        # 若直接回放旧的 paused 流则永不重新执行 Coder。故先删旧流, 再强制新 trace 入队。
                        try:
                            await q.delete_channel(tid)
                        except Exception as _dc_e:  # noqa: BLE001
                            logger.warning("[chat] delete_channel 失败(忽略) trace=%s: %s", tid, _dc_e)
                        # v4: 续跑前清暂停 + 杀掉可能存活的老 Worker(避免双 Worker 并发), 再重新入队
                        try:
                            await q.clear_pause(tid)
                            await q.set_cancel(tid)   # 仅用于通知可能存活的老 Worker 停止
                            await q.clear_cancel(tid) # 必须在 enqueue 之前清除, 否则新 run 复用同一
                                                     # trace_id 入队后一查 is_cancelled 即命中,
                                                     # 导致所有子任务被误标 skipped(建站/天气全空跑)。
                        except Exception as _cp_e:  # noqa: BLE001
                            logger.warning("[chat] clear_pause/set_cancel 失败(忽略) trace=%s: %s", tid, _cp_e)
                        # v4: 状态翻回 running(从断点续跑)
                        await touch_user_state(
                            user.id, status="running", pause_reason=None, pending_decision=None)
                        await q.open_channel(tid)
                        await q.enqueue(payload)
                        logger.info("[chat] [2/3] resume 删旧流并重新入队(执行断点续跑) trace=%s queue=%s",
                                    tid, type(q).__name__)
                    else:
                        resuming = await q.stream_exists(tid)
                        if not resuming:
                            await q.open_channel(tid)
                            await q.enqueue(payload)
                            logger.info("[chat] [2/3] 新任务入队 trace=%s queue=%s", tid, type(q).__name__)
                        else:
                            logger.info("[chat] [2/3] 续接已有流 trace=%s after=%s 全量回放", tid, after or "无")
                    logger.info("[chat] [6/8] 订阅同进程事件流, 开始接收事件")

                    async def _sse_lines():
                        # 把同进程 Worker 产出的事件字典 -> 标准 SSE 文本行序列,
                        # 复用下方既有的 `if raw_line == ""` 解析分支(零逻辑改动)。
                        # 注入 `id:` 帧(事件在队列中的序号 / Redis entry id), 使浏览器
                        # EventSource 能拿到 lastEventId, 供前端 F5 后用 after 游标精确续接。
                        async for ev in q.subscribe(tid, after):
                            event = ev.get("event") or "message"
                            data = ev.get("data")
                            if isinstance(data, (dict, list)):
                                data = json.dumps(data, ensure_ascii=False)
                            elif data is None:
                                data = ""
                            else:
                                data = str(data)
                            ev_id = ev.get("_id")
                            if ev_id is not None:
                                yield f"id: {ev_id}"
                            yield f"event: {event}"
                            yield f"data: {data}"
                            yield ""  # 空行: 触达 `if raw_line == ""` 处理分支

                    event = None
                    data_parts: list[str] = []
                    # 批量发送缓冲: 仅对纯 token 帧做合并(SSE_OUT_BATCH 帧), 减少网络写次数;
                    # 所有状态/控制类帧(node/intent/think/plan/qc/... 非 token)必须每帧立即下发,
                    # 不等凑批——否则模型思考等待期后状态帧会"一次性蹦出"(见 #530 实时反馈诉求)。
                    out_buf: list[bytes] = []
                    SSE_OUT_BATCH = 8
                    async for raw_line in _sse_lines():
                            # 主动检测客户端断连(关闭/刷新/导航离开): 立即终止读取上游并触发级联取消
                            try:
                                if await request.is_disconnected():
                                    logger.info("[chat] 客户端已断开, 提前终止读取上游 trace=%s", tid)
                                    # v4 续接: 断连不暂停 —— 仅移除连接登记, 让 Worker 继续跑完;
                                    # 前端 F5 后用本地 after 游标重新订阅 /api/chat 续接回放。不在此置
                                    # paused(旧逻辑会把 Trace 误标暂停, 与 Worker 仍运行冲突, 导致续接失败)。
                                    await _on_disconnect()
                                    break
                            except Exception:
                                pass
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
                                            if event == "node" and stage:
                                                # v4: 阶段推进 → 实时记录当前 stage 到 user_states
                                                await touch_user_state(user.id, status="running", current_stage=stage)
                                        if event == "node" and isinstance(payload_obj, dict):
                                            # 兼容旧 node(stage=preview) 形态; 现代 preview 事件已在上方专用分支处理。
                                            if payload_obj.get("stage") == "preview":
                                                _np = payload_obj.get("path") or payload_obj.get("url")
                                                if _np:
                                                    preview_path = preview_path or _np
                                                await cache_set(f"site_generated:{conversation_id}", "1", ttl=86400)
                                            # Fix B (#483): doc 技能下发的 Markdown 文件产物 → 供右侧面板预览/下载
                                            # ev() 把 data 关键字参数包成 payload 的嵌套 data 子键:
                                            #   {"event":"node","data":{"stage":"doc_file","data":{"name":..,"content":..,"url":..}}}
                                            # 故从 payload_obj["data"] 取产物字典。
                                            if payload_obj.get("stage") == "doc_file":
                                                _doc = payload_obj.get("data") or {}
                                                if isinstance(_doc, dict) and _doc.get("content"):
                                                    _dname = _doc.get("name") or "开发文档.md"
                                                    _dentry = {
                                                        "name": _dname,
                                                        "size": _doc.get("size") or len(_doc["content"].encode("utf-8")),
                                                        "content": _doc["content"],
                                                    }
                                                    _durl = _doc.get("url")
                                                    if _durl:
                                                        _dentry["url"] = _durl
                                                    doc_files[_dname] = _dentry
                                                    logger.info("[chat] 捕获 doc 产物 trace=%s name=%s cos=%s", tid, _dname, bool(_durl))
                                        if event == "preview" and isinstance(payload_obj, dict):
                                            # P1: preview 事件带 path(相对 ARTIFACT_DIR) 与主文件相对路径
                                            _pp = payload_obj.get("path")
                                            if _pp:
                                                preview_path = _pp
                                            await cache_set(f"site_generated:{conversation_id}", "1", ttl=86400)
                                            # 多文件: 捕获 AI 产出的完整文件列表(v1.2.1+, P1 改为相对路径)
                                            if isinstance(payload_obj.get("files"), dict):
                                                files_dict = payload_obj["files"]
                                            # 注: 不再捕获 preview 的 HTML content 作为兜底推送(用户不关心生成中的文件内容, 也不下发)

                                        event_seq += 1
                                        # trace_event 暂存, 由 finally 批量写入
                                        if event == "aborted":
                                            terminal_status = "aborted"
                                            saw_terminal = True
                                            await touch_user_state(user.id, status="aborted")
                                        elif event == "error":
                                            terminal_status = "error"
                                            saw_terminal = True
                                            await touch_user_state(user.id, status="error")
                                    elif event == "unsupported":
                                        terminal_status = "unsupported"
                                        saw_terminal = True
                                        await touch_user_state(user.id, status="unsupported")
                                        await record_unsupported(user.id, user_text)
                                        await record_intent_decision("unsupported")
                                    elif event in ("disk_save", "progress") and isinstance(payload_obj, dict):
                                        # P1: 本地落盘进度事件透传给前端 exec-head 进度条渲染
                                        # (progress.pct 实时反映落盘中 NN%)；disk_save 含 path 便于前端预热
                                        event_seq += 1
                                        if event == "disk_save" and isinstance(payload_obj.get("path"), str) and payload_obj["path"]:
                                            preview_path = preview_path or payload_obj.get("path")
                                        logger.info(
                                            "[chat] ◇ 落盘事件 trace=%s event=%s file=%s pct=%s",
                                            tid, event, payload_obj.get("file") or payload_obj.get("filename"),
                                            payload_obj.get("pct"),
                                        )
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
                                        # 同步到 MySQL(await: 确保服务重启后可恢复, 不再 fire-and-forget)
                                        await _sync_checkpoint_to_mysql(
                                            conversation_id, stage, ck_data, progress_pct)
                                        # v4: 同步记录断点进度到 user_states
                                        await touch_user_state(
                                            user.id, status="running",
                                            checkpoint_stage=stage, progress_pct=progress_pct)
                                    elif event == "paused":
                                        terminal_status = "paused"
                                        saw_terminal = True
                                        # 方案确认暂停: 保存阶段信息到 checkpoint, 恢复时锁死 generate_site
                                        if isinstance(payload_obj, dict) and payload_obj.get("stage") == "await_confirm":
                                            await ck_set(conversation_id, "await_confirm",
                                                        {"title": payload_obj.get("plan_title", ""),
                                                         "goal": payload_obj.get("plan_goal", ""),
                                                         "steps": payload_obj.get("plan_steps", [])},
                                                        30)
                                        # ── 持久化修复: paused 状态将方案摘要写入 assistant_parts, 确保
                                        #    _do_persist 有内容落库(即使客户端已断开, 刷新后也能看到"正在等待确认"的消息)
                                        _plan_title = (payload_obj or {}).get("plan_title") or (payload_obj or {}).get("title") or "方案已生成"
                                        _plan_goal = (payload_obj or {}).get("plan_goal") or (payload_obj or {}).get("goal") or ""
                                        assistant_parts.append(f"📋 {_plan_title}\n{_plan_goal}")
                                        # v4: 暂停状态落 user_states(权威状态源, 供 my-info 恢复)
                                        await touch_user_state(
                                            user.id,
                                            status="paused",
                                            pause_reason=(payload_obj or {}).get("reason") or "user_interrupt",
                                            pending_decision="continue_instruction",
                                            current_stage=(payload_obj or {}).get("stage"),
                                        )
                                        # 多意图修复: 方案确认暂停已落断点 → 锁死终态为 paused,
                                        # 即便 orchestrator 其他子任务跑完 emit done 使整条流收尾,
                                        # 也不覆盖(否则 finally 的 ck_delete 会把 await_confirm 断点删掉,
                                        # 续跑 ck_get 为空、永远产不出 preview)。done 仍照常转发给前端关闭流。
                                        _paused_locked = True
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
                                    elif event == "requirement_doc" and isinstance(payload_obj, dict):
                                        # 需求文档(requirement_agent 产出): 捕获供落库, 前端重启后可还原
                                        # 注意 SSE data 形如 {"data": <文档>}, 取内层 payload
                                        requirement_doc_captured = payload_obj.get("data")
                                        logger.info("[chat] ◇ 需求文档已捕获 trace=%s", tid)
                                    elif event == "refined" and isinstance(payload_obj, dict):
                                        # 文字总结(v1.2.2): agent 生成完毕反馈文案, 持久化供刷新展示
                                        refined_text = payload_obj.get("data") or ""
                                    elif event == "done":
                                        # 正常完成: 标记已见终止事件, finally 不再触发自动取消
                                        saw_terminal = True
                                        # 多意图修复: 若此前命中 await_confirm 暂停并锁死 paused,
                                        # 则本 done(其他子任务正常收尾)不覆盖终态 —— 仍转发给前端
                                        # 让流正常关闭, 但保留 paused 终态以保住断点(避免 ck_delete)。
                                        if not _paused_locked:
                                            terminal_status = "done"
                                            # v4: 正常完成 → user_states 翻 done, 清暂停标记
                                            await touch_user_state(
                                                user.id,
                                                status="done",
                                                current_stage="done",
                                                progress_pct=100,
                                                pause_reason=None,
                                                pending_decision=None,
                                            )
                                        else:
                                            logger.info(
                                                "[chat] done 被 paused 锁抑制(多意图方案确认仍生效) trace=%s", tid)
                                    # 仅记录事件元信息(类型/阶段/序号), 不再打印 data 内容, 避免日志量爆炸。
                                    # type=token 的帧高频(逐字)产生, 一律跳过打印, 避免日志刷屏。
                                    if event != "token":
                                        logger.info(
                                            "[chat] ◇ SSE #%d type=%s stage=%s",
                                            event_seq, event, stage or "-",
                                        )
                                    frame = ""
                                    if event:
                                        frame += f"event: {event}\n"
                                    frame += f"data: {data}\n\n"
                                    out_buf.append(frame.encode("utf-8"))
                                    # 流式发送策略:
                                    #  - 纯 token 帧: 累计到 SSE_OUT_BATCH 帧再一次性 flush(减少网络写, 逐字体验不受影响)
                                    #  - 非 token 帧(状态/控制类)与终止事件: 立即 flush, 不等待凑批,
                                    #    确保前端时间线状态(意图分析/路由/生成中...)实时逐步出现
                                    if event == "token":
                                        _flush_cond = len(out_buf) >= SSE_OUT_BATCH
                                    else:
                                        _flush_cond = True
                                    if _flush_cond:
                                        _send_ok = True
                                        for _f in out_buf:
                                            try:
                                                yield _f
                                            except (RuntimeError, ConnectionError, OSError, BrokenPipeError) as _e:
                                                # 客户端在发送中途断开(浏览器关闭/刷新): 触发级联取消并停止读取
                                                logger.info("[chat] 批量发送失败(客户端已断开) trace=%s: %s",
                                                            tid, type(_e).__name__)
                                                await _on_disconnect()
                                                _send_ok = False
                                                break
                                        out_buf.clear()
                                        if not _send_ok:
                                            break
                                event, data_parts = None, []
                                continue
                            if raw_line.startswith("event:"):
                                event = raw_line[6:].strip()
                            elif raw_line.startswith("data:"):
                                data_parts.append(raw_line[5:].strip())
                except Exception as e:  # noqa: BLE001
                    logger.warning("[chat] 订阅事件流异常: %s", e)
                    terminal_status = "error"
                    saw_terminal = True
                    await record_error("upstream_error")
                    out_buf.append(_error_frame("UPSTREAM_ERROR", "AI 服务暂时不可用，请稍后重试"))
                    for _f in out_buf:
                        try:
                            yield _f
                        except Exception:
                            pass
                    out_buf.clear()
                    return
        finally:
            # 批量发送收尾: 若仍有未冲刷的缓冲帧(极端情况: 上游在正常 done 前结束且不足一批),
            # 客户端仍连接时补发一次, 避免丢帧。
            if out_buf:
                try:
                    if not await request.is_disconnected():
                        for _f in out_buf:
                            yield _f
                except Exception:
                    pass
                out_buf.clear()
            # 断连自动取消: 用独立任务执行清理, 即便本流式任务被取消也能跑完(与 _do_persist 同模式,
            # 避免清理在 finally 的 await 中被取消信号打断)。主动检测/发送失败路径已 await 过,
            # 此处再跑一次幂等(SREM 无副作用, cancel 置位亦幂等)。
            asyncio.create_task(_on_disconnect())
            # 获取完整 assistant 文本并立即落库(不依赖 finally 内异步)
            assistant_full_text = "".join(assistant_parts)
            approx_tokens = max(0, len(assistant_full_text) // 4)
            logger.info(
                "[chat] [7/8] 流结束 trace=%s 状态=%s events=%d tokens≈%d preview_path=%s output=%d字符",
                tid, terminal_status, sum(event_counts.values()),
                approx_tokens, bool(preview_path), len(assistant_full_text),
            )
            # 不再打印完整响应内容(量大), 仅记录字符数已在上方流结束日志中体现
            # 事件分布
            if event_counts:
                evt_detail = " ".join(f"{k}={v}" for k, v in sorted(event_counts.items()))
                logger.info("[chat]   事件分布: %s", evt_detail)
            # 后台落库任务(独立 session + 重试, 不在 generator finally 中同步等待)
            # v4 续接: 仅当本连接真正收到终止事件(saw_terminal)才由 publisher 落库; 纯断连
            # (客户端 F5/导航离开) 时 Worker 仍在跑, 落库交给 Worker 的 _persist_worker_result,
            # 避免抢占式把 Trace 标成 paused/提前写部分结果 —— 前端续接回放拿到的仍是完整终态。
            if saw_terminal:
                logger.info("[chat] [8/8] 启动后台落库 trace=%s user_text=%.50s status=%s",
                            tid, user_text, terminal_status)
                asyncio.create_task(_do_persist(
                    user_id=user.id,
                    conversation_id=conversation_id,
                    tid=tid,
                    model=model,
                    terminal_status=terminal_status,
                    user_text=user_text,
                    assistant_text=assistant_full_text,
                    preview_path=preview_path,
                    files_dict=files_dict,
                    doc_files=doc_files,
                    refined_summary=refined_text,
                    deliver_fallback_content=deliver_fallback_content,
                    qc_result=qc_result,
                    project_id=project_id,
                    requirement_doc=requirement_doc_captured,
                ))
            else:
                logger.info("[chat] [8/8] 纯断连(未达终止事件), 跳过 publisher 落库, 交由 Worker 兜底落库 trace=%s", tid)
            # v0.9.0: token 统计 + API 延迟记录
            if approx_tokens > 0:
                asyncio.create_task(record_model_tokens(model, approx_tokens))
            _elapsed = (time.time() - t_start_chat) * 1000
            asyncio.create_task(record_api_latency("/api/chat", _elapsed))

    # 续接守卫(增强): 刷新/重连(after 为空 且无新输入 q)但流尚未建立 —— 通常是 Worker 预热中
    # (首事件 XADD 前 Redis Stream 不存在, stream_exists 恒 False)。若此时盲目入队, 会触发
    # 「从头做一遍」(重复生成)。故轮询等待 in-flight Worker 建流; 超时(Worker 真已结束/死亡)
    # 才干净收尾(发 done), 绝不空 q 重入队。
    if (not resume) and (not request.query_params.get("q")) and (not await get_queue().stream_exists(tid)):
        _appeared = False
        for _ in range(STREAM_WAIT_SECONDS // 2):
            await asyncio.sleep(2)
            if await get_queue().stream_exists(tid):
                _appeared = True
                break
        if not _appeared:
            logger.info("[chat] 续接无流且 Worker 未建流, 直接收尾(不重入队) trace=%s", tid)
            return _sse_done_event()

    # v4 续接守卫: 前端以 after 游标请求续接, 但流暂不存在。可能是 Worker 预热中(新 run 尚未
    # XADD)或确已结束/过期。先轮询等待建流; 仍无则直接发 done 干净收尾(不重入队)。
    if after and not await get_queue().stream_exists(tid):
        _appeared = False
        for _ in range(STREAM_WAIT_SECONDS // 2):
            await asyncio.sleep(2)
            if await get_queue().stream_exists(tid):
                _appeared = True
                break
        if not _appeared:
            logger.info("[chat] after 模式但流已消失, 直接收尾 trace=%s", tid)
            return _sse_done_event()

    resp = StreamingResponse(
        publisher(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": tid,  # 便于前端在没自带 trace_id 时也能取消
            "X-Access-Token": new_token,  # 滑动续期: 非浏览器客户端据此轮换 token
        },
    )
    # Set-Cookie: 浏览器同源客户端(SSE/页面)自动携带, 无需手动处理
    _set_access_cookie(resp, new_token)
    return resp


@router.post("/pause")
async def pause_chat(request: Request, user: CurrentUser = Depends(get_current_user)):
    """v4 手动停止: 置 pause:{tid}=user_interrupt, Worker 跑完当前阶段后暂停并落 checkpoint。

    区别于 /cancel(立即 abort, 不可续跑)。前端「停止」按钮走此接口;「放弃」仍走 /cancel。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    trace_id = body.get("trace_id")
    if not trace_id:
        return {"ok": False, "error": "missing_trace_id"}
    q = get_queue()
    await q.set_pause(trace_id, "user_interrupt")
    await touch_user_state(
        user.id,
        status="paused",
        pause_reason="user_interrupt",
        pending_decision="continue_instruction",
        active_trace_id=trace_id,
    )
    logger.info("[pause] 标记暂停 trace=%s user=%s", trace_id, user.id)
    return {"ok": True, "trace_id": trace_id}


@router.get("/my-info")
async def my_info(user: CurrentUser = Depends(get_current_user)):
    """v4 我的状态入口: 返回上一次项目/会话 + 任务状态, 供前端刷新/重开恢复上下文。

    读取优先级: Redis hash `user_states:{uid}` 优先, miss 回 MySQL `user_states` 表。
    """
    state = await get_user_state(user.id)
    if not state:
        return {
            "current_project_id": None,
            "current_conversation_id": None,
            "status": "idle",
            "current_stage": None,
            "progress_pct": 0,
            "pause_reason": None,
            "pending_decision": None,
            "active_trace_id": None,
            "needs_resume": False,
        }
    status = state.get("status", "idle")
    needs_resume = status in ("running", "paused")
    try:
        progress_pct = int(state.get("progress_pct") or 0)
    except Exception:
        progress_pct = 0
    return {
        "current_project_id": int(state["current_project_id"]) if state.get("current_project_id") else None,
        "current_conversation_id": int(state["current_conversation_id"]) if state.get("current_conversation_id") else None,
        "status": status,
        "current_stage": state.get("current_stage"),
        "progress_pct": progress_pct,
        "pause_reason": state.get("pause_reason"),
        "pending_decision": state.get("pending_decision"),
        "active_trace_id": state.get("active_trace_id"),
        "needs_resume": needs_resume,
    }


async def _reconcile_interruption_note(s, conv_id: int, trace_id: str, note: str) -> None:
    """孤儿/重启对账补反馈消息: 仅当该 trace 尚无 assistant 消息(此前未落任何结果)
    且确实存在对应用户消息(说明该轮已开始)时才写入, 保证幂等、不覆盖已落的部分结果。
    目的: 服务强杀(重启/杀端口)时在途 Worker 随进程死亡, finally 永不执行 → 该轮既无
    消息也无暂停落库; 此处补一条「中断」反馈, 让前端重载后总能看到结果(成功/失败/中断均落库)。"""
    try:
        existing = await message_repo.get_by_trace(s, trace_id, "assistant")
        if existing is not None:
            return
        user_msg = await message_repo.get_by_trace(s, trace_id, "user")
        if user_msg is None:
            return
        await message_repo.upsert_assistant(s, conv_id, trace_id, note, "system")
        logger.info("[reconcile] 补中断反馈消息 conv=%s trace=%s", conv_id, trace_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[reconcile] 补中断反馈消息失败 conv=%s trace=%s: %s", conv_id, trace_id, e)


async def _reconcile_once() -> None:
    """孤儿运行对账(单次): 进程被强杀(重启 / 杀端口)时, 在途 Worker 随之死亡,
    留下「孤儿 Trace」—— `Trace.status` 永远停在 'running'(无 Worker 回填 finished_at),
    且 `user_states` 停在 running/paused、Redis 残留 `pause:{tid}` 标志。

    后果: GET /conversations/{id}/status 与 /my-info 谎报 running,
    前端据此去 resume 一个已死、且无 checkpoint 可续的 Worker(本次用户踩到的 bug)。

    对账逻辑:
    - 遍历所有 status=='running' 的 Trace(重启后必然全是孤儿, 无活跃 Worker 能兑现)。
    - 若其会话存在可恢复 checkpoint(Redis `ck:{conv}` status=='paused',
      即 await_confirm 暂停 / 断连暂停) → 视为合法暂停, 不动 Trace,
      仅把 user_states 统一翻为 paused(断连前可能未达阶段边界仍停在 running),
      让前端经 my-info 进入续跑横幅(续跑走 checkpoint, 不依赖 Trace)。
    - 若无 checkpoint → 真正的孤儿: Trace 翻 aborted(回填 finished_at),
      user_states 重置为 idle(清 active_trace_id/current_stage 等脏值)。
    - 兜底: 清全部 Redis `pause:*` 标志(重启后无活跃 Worker 能兑现暂停语义;
      续跑时 resume 分支会自行 clear_pause 再 set_cancel, 不影响正常续跑)。

    注: 每条 Trace 独立 try/except, 单条失败不影响其余(避免某条缺列/脏数据导致整轮对账中断)。
    """
    # 1) 孤儿 running Trace 对账
    try:
        from .db import SessionLocal as _S
        from .repos.trace_repos import trace_repo
        from .cache import ck_get

        async with _S() as s:
            rows = (await s.execute(
                select(Trace).where(Trace.status == "running")
            )).scalars().all()
            if not rows:
                logger.debug("[reconcile] 无孤儿 running Trace, 跳过")
            for t in rows:
                try:
                    conv_id = t.conversation_id
                    uid = t.user_id
                    # 判断是否存在可恢复 checkpoint
                    has_ck = False
                    try:
                        ck = await ck_get(conv_id)
                        has_ck = bool(ck and ck.get("status") == "paused")
                    except Exception:
                        has_ck = False
                    if has_ck:
                        # 合法暂停 → 不动 Trace; user_states 统一翻 paused 保证前端进续跑横幅
                        await touch_user_state(
                            uid,
                            status="paused",
                            pause_reason="offline_timeout",
                            pending_decision="continue_instruction",
                            active_trace_id=t.trace_id,
                        )
                        # 若断连前尚未落任何消息(硬重启杀死在途 Worker), 补一条「可续」反馈
                        await _reconcile_interruption_note(
                            s, conv_id, t.trace_id,
                            "⚠️ 生成因服务重启中断，已保留断点，可点击「继续」从中断处恢复。")
                        logger.info("[reconcile] 保留可恢复暂停 trace=%s conv=%s", t.trace_id, conv_id)
                        continue
                    # 无 checkpoint → 孤儿, 翻 aborted(回填 finished_at)
                    # total_tokens 传 0: 真实计费由 tracing.finish_trace 在生成完成时落;
                    # 孤儿场景没有精确值, 且 finish() 内部已对缺列做 hasattr 防御, 不在此引用 t.total_tokens 以免属性缺失时崩。
                    await trace_repo.finish(s, t, status="aborted", total_tokens=0)
                    await reset_user_state(uid)
                    # 补一条「中断」反馈消息, 让前端重载后看到结果而非静默空白
                    await _reconcile_interruption_note(
                        s, conv_id, t.trace_id,
                        "⚠️ 本次生成因服务重启而中断，未能生成完整结果。你可以重新发起这条对话继续。")
                    logger.info("[reconcile] 孤儿 Trace 翻 aborted trace=%s conv=%s user=%s",
                                t.trace_id, conv_id, uid)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[reconcile] 单条处理失败 trace=%s: %s", getattr(t, "trace_id", "?"), e)
                    continue
    except Exception as e:  # noqa: BLE001
        logger.error("[reconcile] 孤儿运行对账失败: %s", e)

    # 2) 兜底: 清全部 Redis pause:* 标志
    try:
        rc = await get_redis()
        keys = await rc.keys("pause:*")
        if keys:
            await rc.delete(*keys)
            logger.info("[reconcile] 清除 %d 个 pause:* 标志", len(keys))
    except Exception as e:  # noqa: BLE001
        logger.warning("[reconcile] 清除 pause:* 失败: %s", e)


async def reconcile_orphaned_runs() -> None:
    """进程启动一次性对账(兼容旧调用点): 直接跑一轮 _reconcile_once()。"""
    await _reconcile_once()


async def run_orphan_reconciler(interval: float = 30.0) -> None:
    """周期性孤儿对账(后台常驻): 每 interval 秒扫一次 status='running' 的 Trace 并翻终态。

    为什么需要周期跑(用户踩到的 bug 根源):
    启动时的一次性对账只在进程起来那刻生效。进程存活期间, 任何「Worker 跑完但落库/
    翻状态失败」的僵尸, 以及「前端刷新时谎报 running → 触发全量回放旧流」的恶性循环,
    都靠本条周期任务自愈 —— 最多 interval 秒(默认 30s)内把孤儿 Trace 翻 aborted、
    清 user_states 脏值, 从根上消除「前端刷新反复 replay 死流」。
    """
    logger.info("[reconcile] 周期对账已启动 (interval=%.0fs)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await _reconcile_once()
        except Exception as e:  # noqa: BLE001
            logger.warning("[reconcile] 周期对账本轮异常(忽略): %s", e)


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


def _extract_clean_html(text: str) -> str:
    """从 AI 多文件拼接流中提取干净的 HTML 内容。

    去杂: ① 移除 <!-- FILE: ... --> 多文件标记行;
          ② 取 <!doctype html>... 或 <html>... 段, 截掉前后上下文残片。
    未匹配到 HTML 时直接返回原文本。
    """
    import re
    # 去掉多文件分隔标记(整行)
    cleaned = re.sub(r'^<!--\s*FILE:.*?-->\s*$', '', text, flags=re.MULTILINE)
    cleaned = cleaned.strip()
    if not cleaned:
        return text
    # 优先提取 doctype→</html> 或 <html→</html>
    for start_tag in ('<!doctype html', '<!DOCTYPE html', '<html'):
        lc = cleaned.lower()
        idx = lc.find(start_tag)
        if idx >= 0:
            end_idx = lc.find('</html>', idx)
            if end_idx >= 0:
                return cleaned[idx:end_idx + len('</html>')]
            # 只有一个开标签无闭合: 取从开标签到末尾
            return cleaned[idx:]
    return cleaned


async def _persist_conversation(
    db: AsyncSession,
    user_id: int,
    conversation_id: int,
    model: str,
    user_text: str,
    assistant_text: str,
    trace_id: str,
    preview_path: str | None = None,
    files_dict: dict[str, str] | None = None,
    refined_summary: str = "",
    terminal_status: str = "done",
    doc_files: dict[str, dict] | None = None,
    deliver_fallback_content: str | None = None,
) -> None:
    """SSE 结束后落库。build 类消息走 Artifact+结构化 JSON, chat 类存纯文本。

    P1: 建站产物只存**本地相对路径**(预览_path)不内联文件体(DB 零超长内容,根治 1406 类隐患);
    前端据 `${origin}/artifacts/{preview_path}` 同源拉取预览,nginx 静态直出。
    发布(P4)后再回填 preview_url(COS 直链)并置 deployed。
    """
    # 归一化: 拆解 {"data":"x"}{"data":"y"}... → "xy..." (兜底防 AI 服务旧格式)
    assistant_text = _normalize_assistant_text(assistant_text)
    logger.info("[chat] _persist 调用 trace=%s conv=%s uid=%s user_text=%.50s alen=%s preview_path=%s",
                trace_id, conversation_id, user_id, user_text, len(assistant_text), bool(preview_path))
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
    # ⚠️ 判定必须宽松 —— 只要疑似站点/代码产物就走 Artifact 分支(生成预览链接 + 大文件不进
    # messages.content)。漏判会让整站 HTML 掉进纯文本分支写进 64KB 的 messages.content(加宽后
    # 虽不报错, 但会丢失 Artifact/预览链接)。覆盖: <html / <HTML / <!doctype / <!-- FILE: 各种空白变体。
    _norm_head = (assistant_text or "").strip()[:2000].lower()
    is_html = bool(_norm_head) and (
        "<html" in _norm_head
        or "<!doctype" in _norm_head
        or "<!--" in _norm_head and "file:" in _norm_head
        or assistant_text.lstrip().startswith("<!doctype")
        or "<svg" in _norm_head[:200]
    )
    if is_html:
        # ---- 建站/代码生成: 幂等 upsert Artifact(同一 trace 重连/续传/重试只 1 条) ----
        repo = "site"
        # P1: 多文件产物存**本地相对路径**(不内联内容)。files_dict 已是 {fname: rel_path}。
        if files_dict:
            art_files = {fname: {"name": fname, "size": 0, "path": p} for fname, p in files_dict.items()}
        else:
            # 无多文件 dict(少见): 用主文件预览路径兜底(单文件站点)。
            art_files = {"index.html": {"name": "index.html", "size": 0, "path": preview_path or ""}}
        art = await artifact_repo.upsert_by_trace(
            db, trace_id,
            project_id=conv.project_id or 0,
            conversation_id=conv.id,
            title=conv.name or (user_text or "")[:20],
            repo=repo,
            files=art_files,  # dict{name → {name, size, path}}
            preview_url="",    # P1: 未发布不写 COS 直链; 发布(P4)回填
            preview_path=preview_path or None,
        )
        # A(#485): 气泡只渲染「文字总结 + 右侧 artifact-summary-card」, 去掉冗余 site-card。
        # ⚠️ 关键: bubbles.content 只带文件元信息(name/path/size), 绝不内联整站 HTML(根治 1406)。
        _bubble_files = {}
        if isinstance(art.files, dict):
            for _fname, _fmeta in art.files.items():
                if isinstance(_fmeta, dict):
                    _bubble_files[_fname] = {
                        "name": _fmeta.get("name", _fname),
                        "path": _fmeta.get("path") or "",
                        "size": _fmeta.get("size", 0),
                    }
                else:
                    _bubble_files[_fname] = {"name": _fname, "path": "", "size": 0}
        content_obj = {
            "type": "plain",
            "text": refined_summary or "✅ 网站已生成，可在右侧预览面板查看 / 下载。",
            "artifact_id": art.id,
            "title": art.title or "",
            "preview_path": preview_path or "",
            "deployed": False,           # P1: 是否已发布到 COS(发布后 True, 切公开直链)
            "files": _bubble_files,
            "summary": refined_summary,  # v1.2.2: 文字总结持久化
        }
        await message_repo.upsert_assistant(db, conv.id, trace_id, json.dumps(content_obj, ensure_ascii=False), model)
        # P1: 不再回填 Project.preview_url(本地路径非公开); 发布(P4)后才写。
        logger.info("[chat] Artifact 幂等落库 id=%s trace=%s repo=%s preview_path=%s", art.id, trace_id, repo, preview_path or "(无)")
    else:
        # ---- 闲聊/文档: 纯文本 ----
        # #551: 失败兜底 —— 若 LLM 未产出 token(assistant_text 为空), 但 emit_llm_failure 已产出
        # refined_summary(道歉+重试建议文案), 必须把它落库为正式 assistant 消息, 否则用户看到空气泡。
        if assistant_text:
            await message_repo.upsert_assistant(db, conv.id, trace_id, assistant_text, model)
        elif refined_summary:
            await message_repo.upsert_assistant(db, conv.id, trace_id, refined_summary, model)
        elif terminal_status in ("error", "aborted"):
            # 极端情况: 既无产出也无 refined(兜底中的兜底), 至少留一条可见错误提示, 避免空回复。
            _err_msg = ("⚠️ 生成失败，请稍后再试一次，或换一个模型。" if terminal_status == "error"
                        else "⚠️ 已被取消。")
            await message_repo.upsert_assistant(db, conv.id, trace_id, _err_msg, model)

    # Fix B (#483): doc 技能下发的 Markdown 产物 → 额外落 Artifact(repo="doc"),
    # 右侧面板可预览/下载; 气泡仍保留 Markdown 原文(上方 else 分支已存纯文本)。
    if doc_files:
        art_files = {}
        for fname, v in doc_files.items():
            entry: dict[str, Any] = {"name": fname, "size": v.get("size", 0)}
            if v.get("content"):
                entry["content"] = v["content"]  # 内联内容: 离线可预览
            if v.get("url"):
                entry["url"] = v["url"]          # COS 直链: 右侧下载走此
            art_files[fname] = entry
        art = await artifact_repo.upsert_by_trace(
            db, trace_id,
            project_id=conv.project_id or 0,
            conversation_id=conv.id,
            title=(conv.name or (user_text or "")[:20] or "开发文档") + " · 文档",
            repo="doc",
            files=art_files,
            preview_url="",
        )
        logger.info("[chat] Doc Artifact 幂等落库 id=%s trace=%s files=%s", art.id, trace_id, list(art_files.keys()))

    # 失败/中断/不支持 且整轮无任何产出时, 仍补一条反馈消息, 保证前端总能看到结果
    # (成功/失败/中断均落库, 满足"无论如何后端都要返回一条 message 反馈用户")。
    # 仅当该 trace 尚无 assistant 消息时写入, 幂等、不覆盖已落的部分结果。
    if not assistant_text.strip() and terminal_status in ("error", "aborted", "unsupported", "paused"):
        try:
            existing = await message_repo.get_by_trace(db, trace_id, "assistant")
            if existing is None:
                _fb = {
                    "error": "⚠️ 生成失败，请稍后重试。",
                    "aborted": "⚠️ 生成已取消。",
                    "unsupported": "⚠️ 当前请求暂不支持。",
                    "paused": "⚠️ 生成已中断（可继续）。",
                }.get(terminal_status, "⚠️ 生成未产生结果。")
                await message_repo.upsert_assistant(db, conv.id, trace_id, _fb, model)
                logger.info("[chat] 空产出补偿反馈消息 trace=%s status=%s", trace_id, terminal_status)
        except Exception as e:  # noqa: BLE001
            logger.warning("[chat] 空产出补偿失败 trace=%s: %s", trace_id, e)

    if not conv.name and user_text:
        conv.name = user_text[:20]
        logger.info("[chat] 自动设置会话标题 conv=%s name=%.20s", conv.id, user_text)
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


# 注: /cancel 已在 main.py 实现为单进程直写 cancel:<tid>(不再经 httpx 转发)。
# 旧 proxy 版转发到已不存在的 ai_service → 500, 且与 main.py 的 POST /cancel 重复挂载会
# 在 FastAPI 启动时报 "multiple routes" 冲突, 故整段删除。


# reload v99


