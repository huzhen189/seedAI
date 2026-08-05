"""新链路唯一 HTTP 入口：Turn 受理 / SSE 流 / 断线续传 / 控制 / 审批闸门。

对外契约与 frontend/src/api/chat.ts 一一对应：
  POST /api/chat                     受理 Turn 并返回 text/event-stream
  GET  /api/streams/{stream_id}      断线续传（after = 已消费的最大 seq）
  GET  /api/turns/{turn_id}          Turn 快照
  POST /api/turns/{turn_id}/control  stop / pause / resume / correct / supplement / discard
  GET  /api/gate/pending             当前用户待决审批
  POST /api/gate/{approval_id}       审批决策（CAS 单次消费 + nonce 校验）

事务边界只在本层与 Service 层：Repository 一律不 commit。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.audit import DbAuditSink
from app.core.contracts import ApprovalStatus, StageId, StreamEvent, TurnStatus
from app.core.intent_labels import intent_payload, plan_item_payload
from app.core.pipeline import StageResult
from app.core.stages import build_pipeline
from app.core.turn_context import TurnContext
from app.db import get_db, transaction
from app.db.repositories import approvals as approvals_repo
from app.db.repositories import outbox as outbox_repo
from app.db.repositories import turns as turns_repo
from app.db.repositories.feedback import feedback_repo
from app.domains.project import OpsOutcome, project_ops
from app.models import Approval, Conversation, Message, ToolCall, Turn
from app.security import CurrentUser, get_current_user
from app.services.turns import AcceptedTurn, turn_service
from app.transport.stream_broker import broker
from app.analytics import record_user_active, record_gen_stage, record_ai_orch, record_feedback

logger = logging.getLogger("app.api.turns")

router = APIRouter(prefix="/api", tags=["turns"])

# 关闭代理与浏览器缓冲，确保首字节即时可达（nginx 需要 X-Accel-Buffering）。
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# 强引用后台任务，避免事件循环回收导致 Pipeline 静默中断。
_RUNNING: set[asyncio.Task[None]] = set()

_TERMINAL_TURN_STATUS = {
    TurnStatus.COMPLETED.value,
    TurnStatus.FAILED.value,
    TurnStatus.CANCELLED.value,
    TurnStatus.BLOCKED.value,
}


# ---------------------------------------------------------------- 请求模型


class ChatRequest(BaseModel):
    client_msg_id: str = Field(min_length=1, max_length=128)
    conversation_id: int = Field(ge=1)
    message: str = Field(min_length=1)
    expected_conversation_version: int | None = None
    # 前端模型选择器透传：用户指定的执行模型(qwen/deepseek/hy3)；None 走后端默认链。
    model: str | None = None


class TurnControlRequest(BaseModel):
    action: Literal["stop", "pause", "resume", "correct", "supplement", "discard"]
    payload: dict[str, Any] = Field(default_factory=dict)
    # 回溯控制（correct/supplement）时必填：用户对上一轮产物下达的新指令（修改/补充内容）。
    instruction: str | None = Field(default=None, max_length=8000)
    client_msg_id: str | None = Field(default=None, max_length=128)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    decision_nonce: str = Field(min_length=1, max_length=128)


class FeedbackRequest(BaseModel):
    """用户对某一轮生成的评价。trace_id 由 SSE 事件带回前端，天然对齐回放。"""

    trace_id: str = Field(min_length=1, max_length=64)
    rating: int = Field(ge=1, le=10)
    comment: str | None = Field(default=None, max_length=2000)
    # 六维细分星级 {relevance: 8, accuracy: 9, ...}，可选
    dimensions: dict[str, int] | None = None
    conversation_id: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------- SSE 编帧


def _frame(event: StreamEvent) -> str:
    """SSE 单帧。前端只解析 data 行，id/event 供浏览器与调试使用。"""
    return f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def _iter_frames(stream_id: str, after_seq: int | None) -> AsyncIterator[str]:
    """按 seq 续传。

    Broker 的游标是后端相关的 event_id（Redis 为 `ts-seq`，内存为 `memory-N`），
    而前端只持有单调递增的 seq。这里统一以 seq 过滤，两种后端语义一致。
    """
    try:
        async for event in broker.subscribe(stream_id, None):
            if after_seq is not None and event.seq <= after_seq:
                continue
            yield _frame(event)
    except asyncio.CancelledError:  # 客户端断开属正常路径，不记录为错误
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[sse] 流 %s 中断: %s", stream_id, exc)
        yield 'event: error\ndata: {"type":"error","data":{"code":"STREAM_BROKEN"}}\n\n'


def _sse(stream_id: str, after_seq: int | None) -> StreamingResponse:
    return StreamingResponse(
        _iter_frames(stream_id, after_seq),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# ---------------------------------------------------------------- Pipeline 驱动


async def _publish(context: TurnContext, event_type: str, data: dict[str, Any]) -> None:
    await broker.publish(
        stream_id=context.stream_id,
        turn_id=context.turn_id,
        trace_id=context.trace_id,
        type=event_type,
        data=data,
    )


async def _publish_approval_card(session: AsyncSession, context: TurnContext) -> None:
    """把审批卡(含一次性质询明文)推给前端。

    前端 reducer 以 ``approval`` 事件填充审批卡；``decision_nonce`` 只在此下发一次，
    数据库仅存 sha256，因此刷新页面后 /api/gate/pending 拿不到明文——这是刻意的
    "非盲审批"约束，不是缺陷。
    """
    validation = context.validation
    if validation is None or not validation.approval_id:
        return
    approval = (
        await session.execute(select(Approval).where(Approval.approval_id == validation.approval_id))
    ).scalar_one_or_none()
    if approval is None:
        return
    await _publish(
        context,
        "approval",
        {
            "approval_id": approval.approval_id,
            "turn_id": context.turn_id,
            "decision_nonce": validation.decision_nonce,
            "action": approval.action,
            "status": approval.status,
            "risk_level": approval.risk_level,
            "step": approval.step,
            "target": {"type": approval.target_type, "id": approval.target_id},
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        },
    )


def _terminal_of(results: Sequence[StageResult]) -> str:
    """从 S9 的 StageResult 还原终态。

    S9 以 ``turn_<terminal>`` 形式回填 reason_code(completed/waiting_approval/blocked)。
    """
    for result in reversed(results):
        if result.stage is StageId.S9:
            return (result.reason_code or "").removeprefix("turn_") or "completed"
    return "completed"


# 前端 StageRail 每步反馈文案(用户友好, 不暴露内部 reason_code/阶段号)。
# key=阶段号, value=不同状态下的文案; S6 按主域给出更具体的"正在生成网站"等。
_STAGE_DETAIL_RUNNING: dict[str, str] = {
    "S0": "已收到你的请求",
    "S1": "正在回忆上下文…",
    "S2": "正在理解你的需求…",
    "S3": "正在整合已知信息…",
    "S4": "正在规划执行步骤…",
    "S5": "正在检查风险与条件…",
    "S6": "正在执行…",
    "S7": "正在保存结果…",
    "S8": "正在质量校验…",
    "S9": "正在收尾归档…",
}
_STAGE_DETAIL_DONE: dict[str, str] = {
    "S0": "请求已接受",
    "S1": "上下文已就绪",
    "S2": "需求已理解",
    "S3": "信息已整合",
    "S4": "路径已确定",
    "S5": "条件已通过",
    "S6": "执行完成",
    "S7": "结果已保存",
    "S8": "校验通过",
    "S9": "已完成",
}
_S6_DOMAIN_RUNNING: dict[str, str] = {
    "site": "正在为你生成网站…",
    "research": "正在搜集研究资料…",
    "project": "正在处理项目…",
    "chat": "正在组织回复…",
}
_S6_DOMAIN_DONE: dict[str, str] = {
    "site": "网站已生成",
    "research": "资料已整理",
    "project": "项目已处理",
    "chat": "回复已生成",
}


def _stage_detail(stage: str, status: str, context: TurnContext) -> str:
    """给前端 StageRail 的每步反馈文案。

    - 进行中(running/enter):友好的"正在…"文案;
    - 结束(completed/no_op):"…完成"文案;
    - skipped/paused/failed/blocked:对应状态文案。
    S6 按本轮主域(建站/研究/项目/闲聊)给出更具体的描述。
    """
    domain = ""
    if context.understanding is not None:
        for it in context.understanding.resolved_intents:
            if getattr(it, "executable", False):
                domain = it.domain.value
                break
    if status in ("running", "enter"):
        if stage == "S6":
            return _S6_DOMAIN_RUNNING.get(domain, _STAGE_DETAIL_RUNNING["S6"])
        return _STAGE_DETAIL_RUNNING.get(stage, "进行中…")
    if status == "skipped":
        return "首轮对话，暂无历史上下文" if stage == "S1" else "本轮无需执行"
    if status == "paused":
        return "待你确认一项操作"
    if status == "blocked":
        return "已被风险拦截"
    if status == "failed":
        return "执行出错"
    if stage == "S6":
        return _S6_DOMAIN_DONE.get(domain, _STAGE_DETAIL_DONE["S6"])
    return _STAGE_DETAIL_DONE.get(stage, "完成")


async def _run_pipeline(context: TurnContext) -> None:
    """在独立事务中执行 S0-S9，并把阶段轨迹实时投递到流。"""
    # 审计 sink：缓冲每阶段 IN/OUT 快照，Turn 收尾时一次性落 trace_events，
    # 供管理后台「回放」还原完整链路（此前只能去 app.log 翻 [pipeline.io]）。
    audit_sink = DbAuditSink(trace_id=context.trace_id, turn_id=context.turn_id)
    audit_sink.add_event(
        "turn_start",
        {
            "turn_id": context.turn_id,
            "stream_id": context.stream_id,
            "conversation_id": context.session.conversation_id,
            "project_id": context.session.project_id,
            "user_id": context.user.user_id,
            "client_msg_id": context.client_msg_id,
            "user_input": context.clean_message,
        },
    )
    try:
        async with transaction() as session:
            pipeline = build_pipeline(audit_sink=audit_sink, session=session)
            t_start = time.time()

            async def observe(result: StageResult) -> None:
                payload: dict[str, Any] = {
                    "stage": result.stage.value,
                    "status": result.status.value,
                    "reason_code": result.reason_code,
                    "duration_ms": result.duration_ms,
                    # 前端 StageRail 每步反馈文案(用户友好, 不暴露内部 reason_code)。
                    "detail": _stage_detail(result.stage.value, result.status.value, context),
                }
                # 可追溯性：阶段产出的引用（S3 的 SIR 快照链 + 槽位 diff、S6 的 artifact id）
                # 直接透出到 SSE，前端调试面板与线上排障都能按 turn 还原状态演进。
                if result.output_refs:
                    payload["output_refs"] = list(result.output_refs)
                if result.stage is StageId.S3 and context.sir_diff:
                    payload["sir_diff"] = context.sir_diff
                # S2 出栈即把「识别到的意图」中文列表随阶段事件下发：前端的
                # “理解意图” token 框收流后直接换成这张列表（单/多意图统一列表渲染）。
                # 放在 stage 事件而非 done，是因为 done 已是整轮末尾，那时列表才出来就失去了过程感。
                # 注意：pipeline 每阶段先发一条 reason_code="enter" 的合成 running 事件（在 stage.run 之前），
                # 彼时 understanding 尚未算好，必须跳过合成事件，只在真实出栈结果上挂意图列表，否则会重复下发。
                if result.stage is StageId.S2 and result.reason_code != "enter" and context.understanding is not None:
                    payload["intents"] = [
                        intent_payload(item) for item in context.understanding.resolved_intents
                    ]
                # S4 出栈即下发「执行计划」：BoundedPlan.action_items 的中文列表，
                # id 与 S6 的 task 事件 task_id 同源，前端据此把子任务状态回填到对应行。
                # 纯聊天(0 action)在此补一条虚拟 chat 条目，保证列表风格统一、不出现空列表。
                # 同样要跳过 reason_code="enter" 的合成事件：enter 时 context.plan 还没算（classify 在 S4.run 内才写），
                # 若在此下发会得到空的虚拟 chat 条目，紧接着真实结果又下发真实条目，前端瞬时出现重复行再被覆盖。
                if result.stage is StageId.S4 and result.reason_code != "enter":
                    payload["plan"] = _plan_payload(context)
                await _publish(context, "stage", payload)
                # 统计: 各生成阶段耗时(覆盖 S0-S9 全阶段)
                await record_gen_stage(result.stage.value, result.duration_ms)
                # S5 挂起审批时，紧跟一个 approval 事件把审批卡推给前端。
                # 质询明文只在此刻下发这一次(库里只有 sha256)，错过即无法再取得。
                if result.stage is StageId.S5 and result.reason_code == "approval_created":
                    await _publish_approval_card(session, context)

            results = await pipeline.run(context, observe)
            # 终态收口的唯一归属是 S9(内部调 finalize)。此处只读取其结论，
            # 绝不重复调用 finalize —— 否则同事务二次 add(assistant Message)
            # 会撞 uq_messages_turn_role 唯一约束，导致整个 Turn 回滚。
            terminal = _terminal_of(results)
            # 统计: 整轮 s0-s9 编排(总耗时 + 是否成功终态)。
            # 终态语义(finalize): completed=成功落库 / waiting_approval=闸门挂起(有效终态,非失败)
            # / blocked=校验拦截(非崩溃但非成功);异常路径在 except 分支单独记 success=False。
            total_ms = (time.time() - t_start) * 1000
            orch_success = terminal in ("completed", "waiting_approval")
            logger.info(
                "[pipeline] 整轮编排终态 turn=%s terminal=%s success=%s total=%.1fms",
                context.turn_id, terminal, orch_success, total_ms,
            )
            await record_ai_orch(success=orch_success, duration_ms=total_ms)
        audit_sink.add_event(
            "turn_end",
            {
                "terminal": terminal,
                "total_ms": round(total_ms, 1),
                "reply_final": context.reply_final,
                "artifact_refs": list(context.execution.artifact_refs) if context.execution else [],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[pipeline] turn=%s 执行失败: %s", context.turn_id, exc)
        audit_sink.add_event("turn_error", {"message": str(exc), "type": type(exc).__name__})
        await audit_sink.flush()
        await _mark_failed(context.turn_id)
        # 统计: 整轮编排失败
        try:
            await record_ai_orch(success=False, duration_ms=0.0)
        except Exception:  # noqa: BLE001
            pass
        await _publish(
            context,
            "error",
            {"code": "PIPELINE_FAILED", "message": str(exc), "retryable": False},
        )
        return

    # 成功路径统一在业务事务之外落审计（失败路径已在 except 分支落过，保留现场）。
    await audit_sink.flush()

    # 真实意图（S2 已算好）：下发给前端用于切换思考流/阶段叙事文案，
    # 避免前端只能用「当前是否有 project」近似判断，导致复用建站 project 后闲聊也显示"建设中"。
    # 带 label(中文) 一起下发，前端不再维护第二份 intent_id→中文 的映射表。
    intents = [
        intent_payload(item)
        for item in (context.understanding.resolved_intents if context.understanding else [])
    ]
    # 计划终态：把每条 action 的最终子任务状态回填进列表（S6 的 task_results 是权威结论），
    # 断线重连/回放场景下前端只凭 done 也能还原完整的执行计划列表与逐项状态。
    await _publish(
        context,
        "done",
        {
            "status": terminal,
            "reply": context.reply_final,
            "artifact_refs": list(context.execution.artifact_refs) if context.execution else [],
            "intents": intents,
            "plan": _plan_payload(context, final=True),
        },
    )


def _plan_payload(context: TurnContext, *, final: bool = False) -> list[dict[str, Any]]:
    """构造前端「执行计划列表」。

    - 常规：BoundedPlan.action_items 逐条转中文条目；
    - 纯聊天（S4 产出 0 个 action，S6 走 chat 兜底分支）：补一条虚拟 ``chat`` 条目，
      让「纯聊天」也作为列表中的一个元素展示，保持与多意图一致的视觉风格；
    - ``final=True``：用 ``execution.task_results`` 回填每条的终态；虚拟条目按整轮终态推断。
    """
    results: dict[str, str] = {}
    if final and context.execution is not None:
        results = {tr.task_id: tr.status for tr in (context.execution.task_results or [])}

    actions = list(context.plan.action_items) if (context.plan and context.plan.action_items) else []
    if actions:
        return [plan_item_payload(a, status=results.get(a.id, "succeeded" if final else "pending")) for a in actions]

    # 纯聊天兜底条目：id 固定 "chat"，与 S6 兜底分支发出的 task 事件 task_id 对齐。
    exec_ok = final and context.execution is not None and context.execution.status == "succeeded"
    return [{
        "id": "chat",
        "domain": "chat",
        "intent_id": "chat_ask",
        "speech_act": "ask",
        "label": "对话答疑",
        "status": "succeeded" if exec_ok else ("failed" if final else "pending"),
    }]


async def _mark_failed(turn_id: str) -> None:
    """Pipeline 抛错后把 Turn 落到终态，避免留下永久 running 孤儿。"""
    try:
        async with transaction() as session:
            turn = await turns_repo.by_turn_id(session, turn_id)
            if turn is not None and turn.status not in _TERMINAL_TURN_STATUS:
                turn.status = TurnStatus.FAILED.value
                turn.terminal_error_code = "PIPELINE_FAILED"
                turn.lock_version += 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("[pipeline] turn=%s 终态回写失败: %s", turn_id, exc)


def _spawn(context: TurnContext) -> None:
    task = asyncio.create_task(_run_pipeline(context), name=f"turn:{context.turn_id}")
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


# ---------------------------------------------------------------- 端点


@router.get("/models")
async def list_models_endpoint(
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, str]]:
    """返回当前已配置的可用模型列表（供前端模型选择器枚举）。"""
    from app.llm import list_models

    return list_models()


@router.post("/chat")
async def create_turn(
    payload: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """受理 Turn 并返回事件流。

    幂等：同一 client_msg_id 重复提交不会重复执行，只重新挂接已有流。
    """
    logger.info(
        "[chat] 受理 Turn: user=%s conv=%s msg_len=%d client_msg_id=%s meg=%s",
        user.id, payload.conversation_id, len(payload.message), payload.client_msg_id, payload.message,
    )
    async with transaction() as session:
        # 同会话续聊：取上一条 turn 作为 prior_turn_id，供 S1 加载其 SIR 快照 /
        # 回溯上下文。此前普通 /chat 不线程化 prior_turn_id（仅 /control 回溯才传），
        # 导致续聊的结构化记忆（SIR 状态继承、站点产物锁定）整条断链——第二条消息
        # 既无法继承上一条 SIR 基态，S2 也无法锁定上一条产物做受控 edit。
        # 新 turn 尚未插入，故 limit(1) 取到的就是真正的前一条。
        prior = (
            await session.execute(
                select(turns_repo.model)
                .where(turns_repo.model.conversation_id == payload.conversation_id)
                .order_by(turns_repo.model.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        prior_turn_id = prior.turn_id if prior is not None else None
        accepted = await turn_service.accept(
            session,
            user=user,
            conversation_id=payload.conversation_id,
            client_msg_id=payload.client_msg_id,
            raw_message=payload.message,
            expected_conversation_version=payload.expected_conversation_version,
            prior_turn_id=prior_turn_id,
            model=payload.model,
        )

    context = accepted.context
    if not accepted.existing:
        # 统计: 活跃用户(DAU 按日去重 + 人均生成次数) — 每次新受理 Turn 记一次
        await record_user_active(user.id)
        _spawn(context)
    else:
        logger.info("[chat] 幂等命中，复用流 turn=%s", context.turn_id)
    return _sse(context.stream_id, None)


@router.get("/streams/{stream_id}")
async def replay_stream(
    stream_id: str,
    after: int | None = Query(default=None, ge=0),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """断线续传。只允许订阅自己的流。"""
    turn = (
        await session.execute(
            select(turns_repo.model).where(
                turns_repo.model.stream_id == stream_id,
                turns_repo.model.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if turn is None:
        raise HTTPException(status_code=404, detail={"code": "STREAM_NOT_FOUND"})
    return _sse(stream_id, after)


@router.get("/turns/{turn_id}")
async def get_turn(
    turn_id: str,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await turn_service.snapshot(session, turn_id, user.id)


@router.post("/turns/{turn_id}/control")
async def control_turn(
    turn_id: str,
    payload: TurnControlRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """用户侧控制。状态跃迁一律走 CAS，拒绝覆盖已终态的 Turn。

    correct / supplement（回溯控制，§13.1）：不再只登记，而是以用户新指令
    ``instruction`` 受理一个**回溯 turn**，绑定 ``prior_turn_id=turn_id``，走完整
    S0-S9。S2/S4/S6 据此锁定上一轮产物（同 conversation/project）做受控 edit，
    而非另起新站——实现「中断/回溯修改上一句」。
    """
    logger.info("[control] turn=%s action=%s user=%s", turn_id, payload.action, user.id)
    action = payload.action

    # 回溯控制：先行校验指令与上一轮存在，避免进入事务后报错。
    if action in {"correct", "supplement"}:
        if not payload.instruction or not payload.instruction.strip():
            raise HTTPException(status_code=422, detail={"code": "INSTRUCTION_REQUIRED"})
        if not payload.client_msg_id:
            raise HTTPException(status_code=422, detail={"code": "CLIENT_MSG_ID_REQUIRED"})
        prior = await _load_prior_turn(turn_id, user.id)
        if prior is None:
            raise HTTPException(status_code=404, detail={"code": "PRIOR_TURN_NOT_FOUND"})
        # 上一轮必须已终态（completed/blocked），运行中不允许回溯。
        if prior.status not in {TurnStatus.COMPLETED.value, TurnStatus.BLOCKED.value}:
            raise HTTPException(status_code=409, detail={"code": "PRIOR_TURN_NOT_TERMINAL", "status": prior.status})
        # 受理回溯 turn 并异步重跑（新会话/项目沿用 prior 的，保证锁定原产物）。
        accepted = await _accept_retro_turn(
            user=user,
            conversation_id=prior.conversation_id,
            client_msg_id=payload.client_msg_id,
            instruction=payload.instruction.strip(),
            prior_turn_id=turn_id,
        )
        _spawn(accepted.context)
        return {
            "turn_id": accepted.context.turn_id,
            "stream_id": accepted.context.stream_id,
            "prior_turn_id": turn_id,
            "action": action,
            "status": "running",
        }

    async with transaction() as session:
        turn = await turns_repo.by_turn_id(session, turn_id)
        if turn is None or turn.user_id != user.id:
            raise HTTPException(status_code=404, detail={"code": "TURN_NOT_FOUND"})
        if turn.status in _TERMINAL_TURN_STATUS:
            raise HTTPException(status_code=409, detail={"code": "TURN_ALREADY_TERMINAL", "status": turn.status})

        if action in {"stop", "discard"}:
            target, expected = TurnStatus.CANCELLED.value, turn.status
        elif action == "pause":
            target, expected = TurnStatus.PAUSED.value, TurnStatus.RUNNING.value
        elif action == "resume":
            target, expected = TurnStatus.RUNNING.value, TurnStatus.PAUSED.value
        else:  # 其它（未定义）保持原态
            target, expected = turn.status, turn.status

        if target != turn.status:
            changed = await turns_repo.cas_status(
                session,
                turn_id=turn_id,
                expected_status=expected,
                expected_version=turn.lock_version,
                target_status=target,
            )
            if not changed:
                raise HTTPException(status_code=409, detail={"code": "TURN_STATE_CONFLICT", "status": turn.status})

        await outbox_repo.insert(
            session,
            event_key=f"turn:{turn_id}:control:{action}:{turn.lock_version}",
            aggregate_type="turn",
            aggregate_id=turn_id,
            event_type=f"turn.control.{action}",
            payload={"action": action, **payload.payload},
        )
        stream_id, trace_id = turn.stream_id, turn.trace_id

    event_type = "done" if target == TurnStatus.CANCELLED.value else "suspended"
    await broker.publish(
        stream_id=stream_id,
        turn_id=turn_id,
        trace_id=trace_id,
        type=event_type,
        data={"status": target, "action": action},
    )
    return {"turn_id": turn_id, "status": target, "action": action}


async def _load_prior_turn(turn_id: str, user_id: int) -> Turn | None:
    """读取被回溯的上一轮 turn（仅用户自己、且必须是正经 turn）。"""
    async with transaction() as session:
        turn = await turns_repo.by_turn_id(session, turn_id)
        if turn is None or turn.user_id != user_id:
            return None
        return turn


async def _accept_retro_turn(
    *,
    user: CurrentUser,
    conversation_id: int,
    client_msg_id: str,
    instruction: str,
    prior_turn_id: str,
) -> AcceptedTurn:
    """受理一个回溯 turn：复用现有 accept，但携带 prior_turn_id 与新指令。"""
    async with transaction() as session:
        accepted = await turn_service.accept(
            session,
            user=user,
            conversation_id=conversation_id,
            client_msg_id=client_msg_id,
            raw_message=instruction,
            expected_conversation_version=None,
            prior_turn_id=prior_turn_id,
        )
        # 落一条 user Message（前端会与普通消息一致渲染）。
        await session.flush()
        return accepted


@router.post("/feedback")
async def submit_feedback(
    payload: FeedbackRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """提交/更新一轮生成的用户评价（1-10 分 + 可选六维 + 评语）。

    同一 trace 重复提交为覆盖更新（feedbacks.trace_id 唯一）。评分同时打进 Redis
    统计（an: 命名空间的 P_FEEDBACK），并落 MySQL 供管理后台回放侧展示。
    """
    logger.info(
        "[feedback] 提交评价 user=%s trace=%s rating=%s dims=%s",
        user.id, payload.trace_id, payload.rating, bool(payload.dimensions),
    )
    async with transaction() as session:
        try:
            record = await feedback_repo.upsert(
                session,
                user_id=user.id,
                trace_id=payload.trace_id,
                conv_id=payload.conversation_id,
                rating=payload.rating,
                comment=payload.comment,
                dimensions=dict(payload.dimensions) if payload.dimensions else None,
            )
        except LookupError as exc:
            # trace 尚无 assistant 消息（生成失败/未落库）——不是服务端错误。
            raise HTTPException(
                status_code=404, detail={"code": "TRACE_NOT_FOUND", "message": str(exc)}
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail={"code": "INVALID_FEEDBACK", "message": str(exc)}
            ) from exc
        feedback_id = record.id
    # 统计埋点放事务外：Redis 失败不得回滚已落库的评价。
    await record_feedback(payload.rating, bool(payload.dimensions))
    return {"ok": True, "feedback_id": feedback_id, "trace_id": payload.trace_id}


@router.get("/gate/pending")
async def pending_approvals(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, list[dict[str, Any]]]:
    rows = (
        await session.execute(
            select(Approval)
            .where(
                Approval.created_by == user.id,
                Approval.status.in_(
                    [ApprovalStatus.PENDING_FIRST.value, ApprovalStatus.PENDING_SECOND.value]
                ),
                Approval.expires_at > datetime.now(UTC),
            )
            .order_by(Approval.created_at.desc())
            .limit(50)
        )
    ).scalars()
    return {"approvals": [_approval_view(row) for row in rows]}


@router.post("/gate/{approval_id}")
async def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """审批决策。CAS 单次消费 + nonce 绑定，重放与并发只会有一次生效。"""
    logger.info("[gate] 审批决策 approval=%s decision=%s user=%s", approval_id, payload.decision, user.id)
    terminal_status: str | None = None
    reply_text: str = ""
    output_refs: list[str] = []
    purge_dispatch: int | None = None
    async with transaction() as session:
        approval = await approvals_repo.by_external_id(session, approval_id)
        if approval is None or approval.created_by != user.id:
            raise HTTPException(status_code=404, detail={"code": "APPROVAL_NOT_FOUND"})
        if approval.consumed_at is not None:
            raise HTTPException(status_code=409, detail={"code": "APPROVAL_ALREADY_CONSUMED"})
        if approval.status not in {ApprovalStatus.PENDING_FIRST.value, ApprovalStatus.PENDING_SECOND.value}:
            raise HTTPException(status_code=409, detail={"code": "APPROVAL_NOT_PENDING", "status": approval.status})
        expires_at = approval.expires_at
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(UTC).replace(tzinfo=None)
        if expires_at <= datetime.now(UTC).replace(tzinfo=None):
            approval.status = ApprovalStatus.EXPIRED.value
            approval.lock_version += 1
            raise HTTPException(status_code=409, detail={"code": "APPROVAL_EXPIRED"})

        nonce_hash = hashlib.sha256(payload.decision_nonce.encode("utf-8")).hexdigest()
        if nonce_hash != approval.challenge_nonce_hash:
            raise HTTPException(status_code=403, detail={"code": "APPROVAL_NONCE_MISMATCH"})

        now = datetime.now(UTC)
        if payload.decision == "reject":
            approval.status = ApprovalStatus.REJECTED.value
            approval.consumed_at = now
        elif approval.status == ApprovalStatus.PENDING_FIRST.value and approval.step > 1:
            # 高危双人/双段确认：第一段只推进，不放行。
            approval.status = ApprovalStatus.PENDING_SECOND.value
        else:
            approval.status = ApprovalStatus.APPROVED.value
            approval.consumed_at = now

        approval.decided_by = user.id
        approval.decided_at = now
        approval.lock_version += 1

        await outbox_repo.insert(
            session,
            event_key=f"approval:{approval_id}:{approval.status}:{approval.lock_version}",
            aggregate_type="approval",
            aggregate_id=approval_id,
            event_type=f"approval.{approval.status}",
            payload={"approval_id": approval_id, "turn_id": approval.turn_id, "decision": payload.decision},
        )

        turn = await turns_repo.by_turn_id(session, approval.turn_id)
        stream_id = turn.stream_id if turn else None
        trace_id = turn.trace_id if turn else approval.turn_id
        result = _approval_view(approval)

        # 决策即收口: 审批闸门的意义在于「取得用户同意后再落地」。
        # 同意 -> 同一 UoW 内 approved→consumed + 记 operation ledger + 真实执行 ProjectOps;
        # 拒绝 -> Turn 取消。这样 Turn 不会永远卡在 waiting_approval(闭环闭合)。
        # 仅当审批真正到达终态(approved/rejected)时收口; 双人/双段确认仍处于
        # pending_second 的不收口, Turn 继续等待第二段确认。
        if (
            approval.status
            in {ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value}
            and turn is not None
            and turn.status not in _TERMINAL_TURN_STATUS
        ):
            proj_row = (
                await session.execute(
                    select(Conversation.project_id).where(Conversation.id == turn.conversation_id)
                )
            ).scalar_one_or_none()
            project_id = proj_row if proj_row is not None else 0

            if approval.status == ApprovalStatus.REJECTED.value:
                terminal_status = "cancelled"
                ack_text = (
                    f"已拒绝操作：{approval.action}"
                    f"（目标 {approval.target_type}:{approval.target_id or '-'}）。"
                )
                content_refs: list[dict[str, Any]] = []
            else:
                outcome = await _execute_approved_action(
                    session,
                    approval=approval,
                    turn=turn,
                    project_id=project_id,
                    actor_user_id=user.id,
                    trace_id=trace_id,
                )
                purge_dispatch = outcome.details.get("purge_job_id")
                terminal_status = "completed" if outcome.status == "succeeded" else "failed"
                ack_text = outcome.text
                output_refs = list(outcome.output_refs)
                content_refs = [{"ref": ref} for ref in outcome.output_refs]
                if outcome.error_code:
                    turn.terminal_error_code = outcome.error_code[:96]

            turn.status = terminal_status
            turn.last_event_id = f"decision:{payload.decision}"
            turn.lock_version += 1
            session.add(
                Message(
                    conversation_id=turn.conversation_id,
                    project_id=project_id,
                    turn_id=turn.turn_id,
                    trace_id=turn.trace_id,
                    role="assistant",
                    content=ack_text,
                    content_refs=content_refs,
                )
            )
            reply_text = ack_text
            await outbox_repo.insert(
                session,
                event_key=f"turn:{turn.turn_id}:{terminal_status}:decision",
                aggregate_type="turn",
                aggregate_id=turn.turn_id,
                event_type=f"turn.{terminal_status}",
                payload={"turn_id": turn.turn_id, "status": terminal_status, "decision": payload.decision},
            )

    if stream_id:
        await broker.publish(
            stream_id=stream_id,
            turn_id=approval.turn_id,
            trace_id=trace_id,
            type="approval",
            data=result,
        )
        if terminal_status is not None:
            await broker.publish(
                stream_id=stream_id,
                turn_id=approval.turn_id,
                trace_id=trace_id,
                type="done",
                data={
                    "status": terminal_status,
                    "reply": reply_text or result.get("action"),
                    "artifact_refs": output_refs,
                },
            )

    # purge 必须在 HTTP 请求之外分步执行(规范 §8.4)，事务提交后才派发后台 job。
    if purge_dispatch is not None:
        asyncio.create_task(_run_purge_job_background(purge_dispatch))
    return result


async def _execute_approved_action(
    session: AsyncSession,
    *,
    approval: Approval,
    turn: Turn,
    project_id: int,
    actor_user_id: int,
    trace_id: str,
) -> OpsOutcome:
    """approved→consumed + operation ledger(W0) + 领域真实执行，全部在同一 UoW。"""
    logger.debug("[gate] 执行已审批动作 approval=%s action=%s project=%s", approval.approval_id, approval.action, project_id)
    raw_target = approval.target_id or ""
    target_project_id = int(raw_target) if raw_target.isdigit() else project_id
    if not target_project_id:
        return OpsOutcome(status="failed", text="审批目标项目缺失，无法执行。", error_code="missing_target")

    # 稳定 operation_key: 同一审批重放只会占用同一条账本，不会重复产生副作用。
    operation_key = f"approval:{approval.approval_id}:{approval.action}"
    existing = (
        await session.execute(select(ToolCall).where(ToolCall.operation_key == operation_key))
    ).scalar_one_or_none()
    if existing is not None and existing.status == "succeeded":
        return OpsOutcome(
            status="succeeded",
            text="该操作此前已执行完成。",
            output_refs=[operation_key],
        )
    ledger = existing or ToolCall(
        turn_id=turn.turn_id,
        task_id=None,
        tool_name=f"project.{approval.action}"[:64],
        operation_key=operation_key,
        status="running",
        args_hash=approval.args_hash,
        fencing_token=approval.fencing_token,
        result_ref=None,
    )
    if existing is None:
        session.add(ledger)
    else:
        ledger.status = "running"
    await session.flush()

    outcome = await project_ops.execute(
        session,
        action=approval.action,
        project_id=target_project_id,
        user_id=actor_user_id,
        trace_id=trace_id,
        publish_files=list(approval.args.get("publish_files")) if approval.args.get("publish_files") else None,
    )

    ledger.status = "succeeded" if outcome.status == "succeeded" else "failed"
    ledger.result_ref = ",".join(outcome.output_refs)[:255] or None
    if outcome.status == "succeeded":
        approval.status = ApprovalStatus.CONSUMED.value
        approval.lock_version += 1
    await session.flush()
    return outcome


async def _run_purge_job_background(job_id: int) -> None:
    """后台跑 purge 分步 job；失败只记录，job 行保留 failed 状态可重入重试。"""
    try:
        # run_purge_job 自己按步开事务，这里不能再包一层外层事务。
        status = await project_ops.run_purge_job(job_id)
        logger.info("purge job %s 结束: status=%s", job_id, status)
    except Exception as exc:  # noqa: BLE001 - 后台任务不得让异常逃逸到事件循环
        logger.exception("purge job %s 执行异常: %s", job_id, exc)


def _approval_view(approval: Approval) -> dict[str, Any]:
    return {
        "approval_id": approval.approval_id,
        "turn_id": approval.turn_id,
        "action": approval.action,
        "target_type": approval.target_type,
        "target_id": approval.target_id,
        "risk_level": approval.risk_level,
        "status": approval.status,
        "step": approval.step,
        "expires_at": approval.expires_at.isoformat(),
    }
