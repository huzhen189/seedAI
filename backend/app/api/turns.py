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
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.contracts import ApprovalStatus, StageId, StreamEvent, TurnStatus
from app.core.pipeline import InMemoryAuditSink, StageResult
from app.core.stages import build_pipeline
from app.core.turn_context import TurnContext
from app.db import get_db, transaction
from app.db.repositories import approvals as approvals_repo
from app.db.repositories import outbox as outbox_repo
from app.db.repositories import turns as turns_repo
from app.domains.project import OpsOutcome, project_ops
from app.models import Approval, Conversation, Message, ToolCall, Turn
from app.security import CurrentUser, get_current_user
from app.services.turns import turn_service
from app.transport.stream_broker import broker

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


class TurnControlRequest(BaseModel):
    action: Literal["stop", "pause", "resume", "correct", "supplement", "discard"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    decision_nonce: str = Field(min_length=1, max_length=128)


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


async def _run_pipeline(context: TurnContext) -> None:
    """在独立事务中执行 S0-S9，并把阶段轨迹实时投递到流。"""
    try:
        async with transaction() as session:
            audit_sink = InMemoryAuditSink()
            pipeline = build_pipeline(audit_sink=audit_sink, session=session)

            async def observe(result: StageResult) -> None:
                await _publish(
                    context,
                    "stage",
                    {
                        "stage": result.stage.value,
                        "status": result.status.value,
                        "reason_code": result.reason_code,
                        "duration_ms": result.duration_ms,
                    },
                )
                # S5 挂起审批时，紧跟一个 approval 事件把审批卡推给前端。
                # 质询明文只在此刻下发这一次(库里只有 sha256)，错过即无法再取得。
                if result.stage is StageId.S5 and result.reason_code == "approval_created":
                    await _publish_approval_card(session, context)

            results = await pipeline.run(context, observe)
            # 终态收口的唯一归属是 S9(内部调 finalize)。此处只读取其结论，
            # 绝不重复调用 finalize —— 否则同事务二次 add(assistant Message)
            # 会撞 uq_messages_turn_role 唯一约束，导致整个 Turn 回滚。
            terminal = _terminal_of(results)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[pipeline] turn=%s 执行失败: %s", context.turn_id, exc)
        await _mark_failed(context.turn_id)
        await _publish(
            context,
            "error",
            {"code": "PIPELINE_FAILED", "message": str(exc), "retryable": False},
        )
        return

    await _publish(
        context,
        "done",
        {
            "status": terminal,
            "reply": context.reply_final,
            "artifact_refs": list(context.execution.artifact_refs) if context.execution else [],
        },
    )


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


@router.post("/chat")
async def create_turn(
    payload: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """受理 Turn 并返回事件流。

    幂等：同一 client_msg_id 重复提交不会重复执行，只重新挂接已有流。
    """
    logger.info(
        "[chat] 受理 Turn: user=%s conv=%s msg_len=%d client_msg_id=%s",
        user.id, payload.conversation_id, len(payload.message), payload.client_msg_id,
    )
    async with transaction() as session:
        accepted = await turn_service.accept(
            session,
            user=user,
            conversation_id=payload.conversation_id,
            client_msg_id=payload.client_msg_id,
            raw_message=payload.message,
            expected_conversation_version=payload.expected_conversation_version,
        )

    context = accepted.context
    if not accepted.existing:
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
    """用户侧控制。状态跃迁一律走 CAS，拒绝覆盖已终态的 Turn。"""
    logger.info("[control] turn=%s action=%s user=%s", turn_id, payload.action, user.id)
    async with transaction() as session:
        turn = await turns_repo.by_turn_id(session, turn_id)
        if turn is None or turn.user_id != user.id:
            raise HTTPException(status_code=404, detail={"code": "TURN_NOT_FOUND"})
        if turn.status in _TERMINAL_TURN_STATUS:
            raise HTTPException(status_code=409, detail={"code": "TURN_ALREADY_TERMINAL", "status": turn.status})

        action = payload.action
        if action in {"stop", "discard"}:
            target, expected = TurnStatus.CANCELLED.value, turn.status
        elif action == "pause":
            target, expected = TurnStatus.PAUSED.value, TurnStatus.RUNNING.value
        elif action == "resume":
            target, expected = TurnStatus.RUNNING.value, TurnStatus.PAUSED.value
        else:  # correct / supplement：不改变运行态，仅登记控制意图
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
