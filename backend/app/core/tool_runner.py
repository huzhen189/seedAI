"""统一原子工具执行器 ``call_tool`` —— Phase 0 基础设施（规范 §9.2 + docs/06 §3）。

本模块是 Tool 层唯一的「执行入口」。所有原子工具（``app/tools/*`` 下声明的 16 个）
都应经此函数执行，从而统一获得：

1. **W0 操作账本（MID 模式）**：执行前先 upsert 一条 ``running`` 的 ``ToolCall``，
   执行后回写终态（succeeded/failed/unknown）+ ``result_ref`` + ``error_code``。
   已审批动作的「落账」与常规工具调用走同一路径，保证账本一致性。
2. **审批闸门**：``ToolMeta.requires_approval`` 的工具，必须由调用方显式传入
   ``approved=True``（S5/S6 已放行）才能执行；否则直接 ``failed`` 并落账，绝不静默放行。
3. **超时护栏**：用 ``asyncio.wait_for`` 套 ``ToolMeta.timeout_seconds``，超时即判失败。
4. **重试策略**：消费 ``ToolMeta.retry_policy``（max_retries / error_codes / backoff）。
5. **上下文作用域隔离**：执行器只把「调用方显式传入的最小字段」通过 kwargs 转交
   ``Tool.run``，**绝不**把整个 ``TurnContext`` 塞进工具（防跨工具污染）。

调用方（S6 / domain service）职责：
- 用 :func:`make_tool_context` 从 ``TurnContext`` 投影出最小 ``ToolContext``；
- 把工具需要的额外依赖（``session`` / ``project`` / ``turn_context`` 等）作为 kwargs 传入；
- 需要写账本时传入 ``turn_id`` / ``fencing_token`` / ``session``。

设计原则：**执行器只编排，不实现副作用**；副作用永远在 ``Tool.run`` 内。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import random
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import record_ai_tool_call
from app.db.repositories.tool_calls import ToolCallsRepo
from app.tools._registry import get_registry
from app.tools.base import ToolContext, ToolResult, ToolStatus

logger = logging.getLogger("app.core.tool_runner")

# 账本状态与 ToolStatus 的映射
_STATUS_TO_LEDGER = {
    ToolStatus.SUCCEEDED: "succeeded",
    ToolStatus.FAILED: "failed",
    ToolStatus.UNKNOWN: "unknown",
}

# ── operation_key 归集（供 S6 回填 ExecutionResult.operation_keys）────────────
# 契约里 ``ExecutionResult.operation_keys`` 从来没人写过（永远是空列表），
# 导致「本轮到底动了哪些带副作用的操作」在响应契约层不可见，对账只能翻 tool_calls 表。
# 用 ContextVar 做隐式归集：S6 在执行动作前开一个收集域，域内任意深度的 call_tool
# 都会把自己的 operation_key 记进来，领域服务无需逐层透传参数。
_operation_keys_var: ContextVar[list[str] | None] = ContextVar("tool_operation_keys", default=None)


@contextmanager
def collect_operation_keys() -> Iterator[list[str]]:
    """开启一个 operation_key 收集域。

    用法::

        with collect_operation_keys() as op_keys:
            ...  # 期间所有 call_tool(ledger=True) 的 operation_key 都会落进 op_keys
        context.execution = ExecutionResult(..., operation_keys=list(op_keys))

    嵌套安全（用 token 复位）；只读工具（``ledger=False``）不入列，避免噪声。
    """
    bucket: list[str] = []
    token = _operation_keys_var.set(bucket)
    try:
        yield bucket
    finally:
        _operation_keys_var.reset(token)


def _record_operation_key(key: str) -> None:
    """把 operation_key 记入当前收集域（无收集域时静默跳过）。"""
    bucket = _operation_keys_var.get()
    if bucket is not None and key not in bucket:
        bucket.append(key)


def make_tool_context(
    context: Any,
    *,
    project_id: int | None = None,
    conversation_id: int | None = None,
) -> ToolContext:
    """从 ``TurnContext`` 投影出**最小** ``ToolContext``（作用域隔离，绝不带入全轮状态）。

    只取身份类字段：user_id / project_id / conversation_id / trace_id。
    其余（全轮消息、其它子任务槽位、全局 DST）一律不进 ``ToolContext``，
    杜绝跨工具/跨子任务污染。

    Args:
        context: 通常是 ``TurnContext``；也可传任意带 ``user`` / ``session`` / ``trace_id`` 的对象。
        project_id: 工具要操作的项目 id（优先用显式传入；缺省回退到 ``context.session.project_id``）。
        conversation_id: 覆盖用（缺省回退到 ``context.session.conversation_id``）。
    """
    user = getattr(context, "user", None)
    session = getattr(context, "session", None)
    user_id = getattr(user, "user_id", None)
    # 优先使用显式 project_id，其次 context.session.project_id（TurnContext 没有顶层 project_id 属性）。
    proj = project_id if project_id is not None else getattr(session, "project_id", None)
    conv = conversation_id if conversation_id is not None else getattr(session, "conversation_id", None)
    trace_id = getattr(context, "trace_id", None)
    return ToolContext(
        user_id=user_id,
        project_id=proj,
        conversation_id=conv,
        trace_id=trace_id,
    )


def _json_default(o: Any) -> str:
    """把不可 JSON 序列化的入参降级成稳定字符串。

    关键点：**带主键的 ORM 实例必须带上主键**（``<Project:12>``），否则
    「同一份 HTML 发布到不同项目」会算出同一个 ``args_hash`` → 同一个
    ``operation_key`` → 命中 UNIQUE 约束后被误判成幂等重放，第二个项目的
    发布会被吞掉。这是一个真实的账本串号风险，必须在哈希层根治。
    """
    oid = getattr(o, "id", None)
    if oid is not None and not callable(oid):
        return f"<{type(o).__name__}:{oid}>"
    return f"<{type(o).__name__}>"


def _args_hash(kwargs: dict[str, Any]) -> str:
    """对工具入参计算稳定哈希，用于账本 ``args_hash`` 与幂等 ``operation_key``。

    入参可能含不可 JSON 序列化的对象（ORM 实例、TurnContext 等），用
    :func:`_json_default` 把这类对象降级为 ``<类型名[:主键]>``，
    保证哈希始终可算、同语义入参哈希一致、不同实体互不串号。
    """
    try:
        payload = json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=_json_default)
    except Exception:  # 极端兜底：直接 repr
        payload = repr(sorted((k, type(v).__name__) for k, v in kwargs.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def _derive_operation_key(tool_id: str, meta_idempotency: bool, args_hash: str,
                          idempotency_key: str | None) -> str:
    """推导账本 ``operation_key``：

    - 显式传入 ``idempotency_key`` 优先（工具自身语义稳定键）。
    - 幂等工具(``idempotency=True``)：``<tool_id>:<args_hash>`` —— 同入参多次调用共享一条账本。
    - 非幂等工具：追加随机后缀，每次调用唯一（``operation_key`` 列有唯一约束）。
    """
    if idempotency_key:
        return idempotency_key
    if meta_idempotency:
        return f"{tool_id}:{args_hash}"
    return f"{tool_id}:{args_hash}:{uuid.uuid4().hex[:8]}"


async def _ledger_upsert(
    session: AsyncSession | None,
    *,
    tool_name: str,
    operation_key: str,
    args_hash: str,
    turn_id: str | None,
    fencing_token: str | None,
    task_id: int | None,
    status: str,
    result_ref: str | None = None,
    error_code: str | None = None,
) -> None:
    """幂等写/更账本。``session`` 或 ``turn_id`` 缺失时 fail-soft 跳过（仅调试日志）。"""
    if session is None or not turn_id:
        logger.debug("[tool_runner] 跳过账本写入 tool=%s reason=%s", tool_name,
                     "no_session" if session is None else "no_turn_id")
        return
    try:
        repo = ToolCallsRepo()
        await repo.upsert_by_operation_key(
            session,
            tool_name=tool_name,
            operation_key=operation_key,
            args_hash=args_hash,
            turn_id=turn_id,
            fencing_token=fencing_token or "",
            task_id=task_id,
            status=status,
            result_ref=result_ref,
            error_code=error_code,
        )
    except Exception as exc:  # 账本失败绝不影响主流程
        logger.warning("[tool_runner] 账本写入失败 tool=%s op=%s err=%s", tool_name, operation_key, str(exc)[:160])


async def call_tool(
    tool_id: str,
    ctx: ToolContext,
    *,
    session: AsyncSession | None = None,
    turn_id: str | None = None,
    task_id: int | None = None,
    fencing_token: str | None = None,
    approved: bool = False,
    idempotency_key: str | None = None,
    ledger: bool = True,
    **kwargs: Any,
) -> ToolResult:
    """统一执行一个原子工具。

    Args:
        tool_id: 注册在 ``ToolRegistry`` 的工具 id（如 ``site_publish`` / ``rag_query``）。
        ctx: 由 :func:`make_tool_context` 投影出的最小 ``ToolContext``。
        session: 数据库会话（写账本 / 工具内部 ORM 操作需要）。可 None（跳过账本）。
        turn_id / fencing_token: 账本归因字段（来自 ``TurnContext``）。
        approved: 是否已通过审批闸门。``requires_approval`` 工具必须 ``True`` 才执行。
        idempotency_key: 显式幂等键（覆盖工具自身默认）。
        ledger: 是否写 W0 操作账本（默认 True）。
        **kwargs: 透传给 ``Tool.run`` 的额外依赖（如 ``html`` / ``project`` / ``turn_context``）。

    Returns:
        ``ToolResult``（成功/失败/未知），绝不抛裸异常。
    """
    registry = get_registry()
    try:
        meta = registry.get(tool_id)
    except KeyError as exc:
        logger.error("[tool_runner] 未注册工具 tool=%s", tool_id)
        return ToolResult.fail(
            ErrorEnvelope_("tool_unknown", "config", f"未注册的原子工具: {tool_id}",
                           "确认 tool_id 拼写与注册", retryable=False, retry_scope="none"),
        )

    args_hash = _args_hash(kwargs)
    operation_key = _derive_operation_key(tool_id, meta.idempotency, args_hash, idempotency_key)
    # 只把「会写账本」的操作计入 operation_keys（读-only 检索不算副作用操作）。
    if ledger:
        _record_operation_key(operation_key)

    # ── 闸门 1：审批 ───────────────────────────────────────────────
    if meta.requires_approval and not approved:
        logger.warning("[tool_runner] 审批闸门拦截 tool=%s risk=%s（未持 approved）",
                       tool_id, meta.risk.value)
        # 统计：被审批闸门拦截记为 blocked（未真正执行副作用），与 succeeded/failed 区分。
        try:
            await record_ai_tool_call(
                tool_name=tool_id, status="blocked", risk=meta.risk.value,
                duration_ms=0, attempts=1,
            )
        except Exception:  # noqa: BLE001 — 统计失败绝不影响主流程
            pass
        if ledger:
            await _ledger_upsert(session, tool_name=tool_id, operation_key=operation_key,
                                 args_hash=args_hash, turn_id=turn_id, fencing_token=fencing_token,
                                 task_id=task_id, status="failed", error_code="tool_requires_approval")
        return ToolResult.fail(
            ErrorEnvelope_("tool_requires_approval", "approval",
                           f"工具 {tool_id} 需先审批", "通过 S5/S6 审批流程后再调用",
                           retryable=False, retry_scope="none"),
            idempotency_key=operation_key,
        )

    # ── 账本前置写 running（MID 模式）─────────────────────────────
    if ledger:
        await _ledger_upsert(session, tool_name=tool_id, operation_key=operation_key,
                             args_hash=args_hash, turn_id=turn_id, fencing_token=fencing_token,
                             task_id=task_id, status="running")

    logger.info("[tool_runner] ▶ 执行 tool=%s risk=%s idempotency=%s approved=%s op=%s",
                tool_id, meta.risk.value, meta.idempotency, approved, operation_key)

    # ── 执行 + 超时 + 重试 ────────────────────────────────────────
    tool = registry.build(tool_id)
    # 把 call_tool 收到的 session 透传给需要它的工具（如 SitePublishTool /
    # SiteDeleteTool / SiteDeployTool 的 run 都声明了 session 参数）。
    # call_tool 此前只用 session 写账本，未转交工具，导致这些工具因缺
    # session 抛 TypeError（见测试发现的缺陷）。仅当工具 run 签名确实接受
    # session 且调用方未显式传入时才注入，避免误伤不需要 session 的工具。
    try:
        _run_params = inspect.signature(tool.run).parameters
        if "session" in _run_params and "session" not in kwargs:
            kwargs["session"] = session
    except (ValueError, TypeError):
        pass
    result: ToolResult | None = None
    attempt = 0
    started = time.perf_counter()
    max_retries = int((meta.retry_policy or {}).get("max_retries", 0))
    retry_codes = list((meta.retry_policy or {}).get("error_codes", []) or [])
    backoff = str((meta.retry_policy or {}).get("backoff", "none"))

    while True:
        try:
            result = await asyncio.wait_for(
                tool.run(ctx, **kwargs),
                timeout=meta.timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.error("[tool_runner] 超时 tool=%s limit=%ss op=%s", tool_id, meta.timeout_seconds, operation_key)
            result = ToolResult.fail(
                ErrorEnvelope_("tool_timeout", "timeout", f"执行超过 {meta.timeout_seconds}s",
                               "检查工具实现或调大 timeout_seconds", retryable=True, retry_scope="task"),
                idempotency_key=operation_key,
            )
        except Exception as exc:  # 工具约定不抛，但防御性兜底
            logger.exception("[tool_runner] 工具抛异常 tool=%s op=%s", tool_id, operation_key)
            result = ToolResult.fail(
                ErrorEnvelope_("tool_internal_error", "internal", f"工具内部异常: {str(exc)[:200]}",
                               "查看工具日志", retryable=True, retry_scope="task"),
                idempotency_key=operation_key,
            )

        # 成功或未知终态不重试；仅 FAILED 且命中重试策略才重试
        if result.status != ToolStatus.FAILED:
            break
        code = (result.error.code if result.error else "") or ""
        retryable = (not retry_codes) or (code in retry_codes)
        if attempt >= max_retries or not retryable:
            break
        attempt += 1
        delay = 0.0
        if backoff == "exp_jitter":
            delay = min(2 ** attempt + random.uniform(0, 1), 5.0)
        logger.warning("[tool_runner] 重试 tool=%s attempt=%d/%d delay=%.2fs code=%s",
                       tool_id, attempt, max_retries, delay, code)
        if delay > 0:
            await asyncio.sleep(delay)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    # 工具级执行统计(与 s6 的 ai:sub 子任务统计互补)：耗时/尝试次数/终态/风险 写入统计基建。
    try:
        await record_ai_tool_call(
            tool_name=tool_id,
            status=result.status.value,
            risk=meta.risk.value,
            duration_ms=elapsed_ms,
            attempts=attempt + 1,
        )
    except Exception:  # noqa: BLE001 — 统计失败绝不影响主流程
        pass
    # 回填操作键与账本引用
    if result.idempotency_key is None:
        result.idempotency_key = operation_key

    result_ref = result.data.get("result_ref") or result.data.get("artifact_id") or result.data.get("deployment_id")
    result_ref = str(result_ref) if result_ref is not None else None
    ledger_status = _STATUS_TO_LEDGER.get(result.status, "unknown")
    if ledger:
        await _ledger_upsert(session, tool_name=tool_id, operation_key=operation_key,
                             args_hash=args_hash, turn_id=turn_id, fencing_token=fencing_token,
                             task_id=task_id, status=ledger_status,
                             result_ref=result_ref,
                             error_code=(result.error.code if result.error else None))

    logger.info("[tool_runner] ■ 完成 tool=%s status=%s op=%s ref=%s elapsed=%dms attempts=%d",
                tool_id, result.status.value, operation_key, result_ref, elapsed_ms, attempt + 1)
    return result


def ErrorEnvelope_(
    code: str, category: str, what: str, next_: str,
    *, retryable: bool = False, retry_scope: str = "none",
) -> Any:
    """轻量构造 ``ErrorEnvelope``，避免在本模块直接 import 重依赖。

    延后到调用点 import 的目的是让 ``call_tool`` 在缺少 pydantic 的极简测试环境也能
    被 import；真实运行中 ``app.core.contracts.ErrorEnvelope`` 一定可用。
    """
    from app.core.contracts import ErrorEnvelope

    return ErrorEnvelope(
        code=code, category=category, what=what, why="",
        next=next_, retryable=retryable, retry_scope=retry_scope,
    )
