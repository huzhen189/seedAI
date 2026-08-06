from __future__ import annotations
import logging
import time

logger = logging.getLogger(__name__)

from app.core.contracts import ExecutionResult, ResponseFragment, StageId, StageStatus, TaskResult
from app.core.governance import action_requires_approval, action_risk_label, governance_basis
from app.core.intent_labels import intent_label
from app.core.tool_runner import collect_operation_keys
from app.core.turn_context import TurnContext
from app.domains.chat import chat_service
from app.domains.project import project_ops
from app.domains.research import research_service
from app.domains.site import site_service
from app.analytics import record_skill_outcome, record_ai_subtask
from .base import BaseStage

# 高危项目操作由 S5 审批闸门承载，决策端点负责真实执行；
# S6 只直接执行低危动作，避免同一副作用出现两条执行路径。
# 判定统一走 app.core.governance.action_requires_approval（ToolMeta ∪ 历史规则），
# 与 S5 共用同一真相源——此前 S5/S6 各写一份 {"publish","trash","purge"} 集合，
# 任一处漏改就会出现「S5 放行、S6 拒执行」或反过来的错盘。


async def _emit_task(
    context: TurnContext,
    task_id: str,
    label: str,
    status: str,
    *,
    duration_ms: float | None = None,
) -> None:
    """下发子任务状态帧（SSE ``task`` 事件）。

    ``task_id`` 必须与 S4 下发的执行计划条目 id 同源（ActionItem.id / 纯聊天固定 "chat"），
    前端才能把状态回填到正确的行。fail-soft：推流失败不得影响业务执行本身。
    """
    payload: dict[str, object] = {"task_id": task_id, "label": label, "status": status}
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 1)
    try:
        await context.emit("task", payload)
    except Exception as exc:  # noqa: BLE001 — 推流是旁路，不能反噬主流程
        logger.warning("[S6] task 事件下发失败 task=%s: %s", task_id, exc)


class S6ExecuteStage(BaseStage):
    """S6 执行(§5.6,真实副作用唯一落点)。

    前置:必须由 S5 校验通过(``validation.status == "pass"``)才会进入;否则 SKIPPED。
    多意图串行执行(BoundedPlan.serial=True)：
      - 规划 0 个 action → 纯聊天(chat_service.respond) 兜底;
      - 规划 1..N 个低风险 action → 依次按域分派(site/research/project/chat);
      - 每个 action 独立计时、独立写 analytics(ai:sub / skill_outcome)，并 append 各自的 ResponseFragment;
      - 所有 fragment 由 S8 汇总拼成回复(S8 逻辑不变)，``done.reply`` 携带合并结果，
        彻底解决「复合句只返回部分动作结果」的漏盘。
    """

    stage_id = StageId.S6

    async def run(self, context: TurnContext):
        if context.validation is None or context.validation.status != "pass":
            logger.debug("[S6] 校验未通过,跳过 turn=%s", context.turn_id)
            return self.result(StageStatus.SKIPPED, "validation_not_pass")

        actions = list(context.plan.action_items) if (context.plan and context.plan.action_items) else []
        if context.plan:
            for idx, a in enumerate(actions):
                logger.info(
                    "[S6] plan.action[%d] id=%s intent=%s domain=%s speech=%s prior_turn=%s args=%s",
                    idx, a.id, a.intent_id, a.domain.value, a.speech_act.value,
                    a.prior_turn_id, (a.arguments or {}),
                )
        t_total = time.time()

        # 纯聊天兜底：没有任何可执行 action（例如整句都是 CHAT 兜底意图）。
        # task_id 固定 "chat"，与 turns._plan_payload 的虚拟条目对齐，
        # 让「纯聊天」在前端执行计划列表里也有一行并能实时变状态。
        if not actions:
            logger.debug("[S6] 纯聊天回复 turn=%s", context.turn_id)
            t0 = time.time()
            await _emit_task(context, "chat", "对话答疑", "running")
            text = await chat_service.respond(context)
            elapsed = (time.time() - t0) * 1000
            context.response_fragments.append(ResponseFragment(status="success", text=text, producer_stage=StageId.S6))
            context.execution = ExecutionResult(status="succeeded", committed=True)
            await _emit_task(context, "chat", "对话答疑", "succeeded", duration_ms=elapsed)
            await record_skill_outcome("chat", "ok", elapsed)
            await record_ai_subtask(skill="chat", status="succeeded", risk="low", duration_ms=elapsed)
            return self.result(StageStatus.COMPLETED, "chat_completed")

        if self.session is None:
            logger.error("[S6] 缺少数据库会话,无法执行动作 turn=%s", context.turn_id)
            return self.result(StageStatus.FAILED, "no_session")

        succeeded = 0
        total = len(actions)
        has_error = False
        artifact_refs: list[str] = []
        task_results: list[TaskResult] = []
        # 开启 operation_key 收集域：域内任意深度的 call_tool(ledger=True) 都会自动登记，
        # 用于回填 ExecutionResult.operation_keys（此前该字段恒空，本轮副作用在契约层不可见）。
        with collect_operation_keys() as op_keys:
            for action in actions:
                ok, refs, tr = await self._run_one(context, action)
                artifact_refs.extend(refs)
                if tr is not None:
                    task_results.append(tr)
                if ok:
                    succeeded += 1
                else:
                    has_error = True
            operation_keys = list(op_keys)
        if operation_keys:
            logger.info("[S6] 本轮副作用操作键 count=%d keys=%s turn=%s",
                        len(operation_keys), operation_keys[:5], context.turn_id)

        context.response_fragments.append(ResponseFragment(
            status="info",
            text=f"已完成 {succeeded}/{total} 项操作。",
            producer_stage=StageId.S6,
        ))

        elapsed = (time.time() - t_total) * 1000
        if has_error and succeeded == 0:
            context.execution = ExecutionResult(status="failed", committed=False,
                                                artifact_refs=artifact_refs, task_results=task_results,
                                                operation_keys=operation_keys)
            return self.result(StageStatus.BLOCKED, "all_actions_failed")
        context.execution = ExecutionResult(
            status="succeeded" if not has_error else "partial",
            committed=True,
            artifact_refs=artifact_refs,
            task_results=task_results,
            operation_keys=operation_keys,
        )
        return self.result(
            StageStatus.COMPLETED,
            "multi_action_completed" if total > 1 else "single_action_completed",
        )

    async def _run_one(self, context: TurnContext, action):
        """执行单个 action_item。

        返回 (ok, artifact_refs, task_result)。成功 ok=True；失败落 error fragment 并返回
        ok=False（不中断兄弟动作，闭合「单动作失败整轮崩」的漏盘分支）。
        """
        logger.info("[S6] 执行动作 domain=%s speech=%s turn=%s", action.domain.value, action.speech_act.value, context.turn_id)
        label = intent_label(action.intent_id, action.domain.value, action.speech_act.value)
        t_item = time.time()
        # 逐项发 task 事件：running → succeeded/failed。前端按 task_id 回填到
        # S4 下发的执行计划列表对应行，实现「子任务状态」实时可见（此前后端从未发过 task 事件，
        # 前端 activities 永远为空，列表看起来是死的）。
        await _emit_task(context, action.id, label, "running")
        ok = False
        try:
            ok, refs, tr = await self._dispatch(context, action)
            return ok, refs, tr
        except Exception as exc:  # noqa: BLE001 — 单个动作失败不影响兄弟动作，但必须被记录
            logger.exception("[S6] 动作执行异常 domain=%s: %s", action.domain.value, exc)
            context.response_fragments.append(ResponseFragment(
                status="error",
                text=f"执行「{label}」时出现问题，其它动作不受影响。",
                producer_stage=StageId.S6))
            await record_skill_outcome(action.intent_id, "fail", 0.0)
            await record_ai_subtask(skill=action.intent_id, status="blocked", risk="low")
            return False, [], None
        finally:
            # finally 收口保证异常路径也能把行状态从 running 落到 failed，
            # 否则前端计划列表会有一行永远转圈。
            await _emit_task(
                context, action.id, label,
                "succeeded" if ok else "failed",
                duration_ms=(time.time() - t_item) * 1000,
            )

    async def _dispatch(self, context: TurnContext, action):
        """按域分派到具体领域服务（异常向上抛给 _run_one 统一收口）。"""
        if action.domain.value == "site":
            return await self._run_site(context, action)
        if action.domain.value == "research":
            return await self._run_research(context, action)
        if action.domain.value == "project":
            return await self._run_project(context, action)
        if action.domain.value == "chat":
            return await self._run_chat(context, action)
        # 未知域：兜底 fragment，不抛错，避免整轮崩溃（闭合未知分支）。
        context.response_fragments.append(ResponseFragment(
            status="error", text="当前操作尚未具备可执行实现。", producer_stage=StageId.S6))
        await record_skill_outcome("unknown", "fail", 0.0)
        await record_ai_subtask(skill="unknown", status="blocked", risk="low")
        return False, [], None

    async def _run_site(self, context: TurnContext, action):
        if self.session is None:
            raise RuntimeError("S6 site action requires a database session")
        t0 = time.time()
        # 建站流式透传：把 LLM 逐块输出转成独立 SSE 事件(gen_token/gen_think)，
        # 让前端「正在为你生成网站…」上方小窗实时滚动展示。刻意**不复用**聊天用的
        # token/think 事件——否则建站正文会混进助手回复气泡(state.response)，污染最终答案。
        # fail-soft：emit 失败不得反噬建站主链路。
        async def _on_site_chunk(kind: str, text: str) -> None:
            event_type = "gen_think" if kind == "think" else "gen_token"
            try:
                await context.emit(event_type, {"text": text})
            except Exception as exc:  # noqa: BLE001
                logger.warning("[S6] 建站 token 帧下发失败(忽略): %s", exc)
        artifact, text = await site_service.create_or_edit(self.session, context, on_chunk=_on_site_chunk)
        elapsed = (time.time() - t0) * 1000
        logger.info("[S6] site 动作产物 artifact_id=%s v=%s 文本首120=%r",
                     artifact.id, getattr(artifact, "version", "?"), text[:120])
        context.response_fragments.append(ResponseFragment(
            status="success", text=text, producer_stage=StageId.S6, output_refs=[str(artifact.id)]))
        await record_skill_outcome("site", "ok", elapsed)
        await record_ai_subtask(skill="site", status="succeeded", risk="low", duration_ms=elapsed)
        return True, [str(artifact.id)], TaskResult(task_id=action.id, status="succeeded", output_refs=[str(artifact.id)])

    async def _run_research(self, context: TurnContext, action):
        t0 = time.time()
        text = await research_service.research(context)
        elapsed = (time.time() - t0) * 1000
        logger.info("[S6] research 动作产物 文本首200=%r", text[:200])
        # research 走 fail-soft 委派时会返回空串（正文与流式帧已由 chat_service.respond
        # 自行 emit + append）。此处必须判空，否则会多塞一个空 ResponseFragment，
        # S8 汇总时拼出多余空行/重复分隔符。
        if text.strip():
            context.response_fragments.append(ResponseFragment(status="success", text=text, producer_stage=StageId.S6))
        else:
            logger.debug("[S6] research 返回空文本(已委派 chat)，跳过 fragment 追加 turn=%s", context.turn_id)
        await record_skill_outcome("research", "ok", elapsed)
        await record_ai_subtask(skill="research", status="succeeded", risk="low", duration_ms=elapsed)
        return True, [], TaskResult(task_id=action.id, status="succeeded")

    async def _run_chat(self, context: TurnContext, action):
        seg = (action.arguments or {}).get("message") or context.clean_message
        t0 = time.time()
        text = await chat_service.respond(context)
        elapsed = (time.time() - t0) * 1000
        logger.info("[S6] chat 动作产物 文本首200=%r", text[:200])
        context.response_fragments.append(ResponseFragment(status="success", text=text, producer_stage=StageId.S6))
        await record_skill_outcome("chat", "ok", elapsed)
        await record_ai_subtask(skill="chat", status="succeeded", risk="low", duration_ms=elapsed)
        return True, [], TaskResult(task_id=action.id, status="succeeded")

    async def _run_project(self, context: TurnContext, action):
        """项目域执行：低危动作直落 ProjectOps（高危在 S5 已被拦截）。"""
        session = self.session
        if session is None:
            raise RuntimeError("S6 project action requires a database session")
        act = action.speech_act.value
        risk = action_risk_label(act)
        t0 = time.time()
        if action_requires_approval(act):
            # 正常不会到这里(S5 已 PAUSED)；到了说明闸门被绕过，必须拒绝而不是执行。
            # 这里额外打 basis，便于事后定位「谁把高危动作放进了 S6」。
            logger.error(
                "[S6] 高危动作绕过 S5 闸门被拦截 act=%s risk=%s basis=%s turn=%s",
                act, risk, governance_basis(act), context.turn_id,
            )
            context.response_fragments.append(ResponseFragment(
                status="error", text="该操作需要先通过审批确认。", producer_stage=StageId.S6))
            elapsed = (time.time() - t0) * 1000
            await record_skill_outcome(f"project_{act}", "fail", elapsed)
            await record_ai_subtask(skill=f"project_{act}", status="blocked", risk=risk, duration_ms=elapsed)
            return False, [], None

        target_id = action.target.id
        project_id = int(target_id) if (target_id or "").isdigit() else (context.session.project_id or 0)
        if not project_id:
            context.response_fragments.append(ResponseFragment(
                status="error", text="未能确定目标项目，请先选择项目。", producer_stage=StageId.S6))
            elapsed = (time.time() - t0) * 1000
            await record_skill_outcome(f"project_{act}", "fail", elapsed)
            await record_ai_subtask(skill=f"project_{act}", status="blocked", risk=risk, duration_ms=elapsed)
            return False, [], None

        outcome = await project_ops.execute(
            session, action=act, project_id=project_id,
            user_id=context.user.user_id, trace_id=context.trace_id,
        )
        succeeded = outcome.status == "succeeded"
        elapsed = (time.time() - t0) * 1000
        logger.info("[S6] project 动作(%s) 产物 status=%s refs=%s 文本首120=%r",
                     act, outcome.status, list(outcome.output_refs), outcome.text[:120])
        context.response_fragments.append(ResponseFragment(
            status="success" if succeeded else "error",
            text=outcome.text, producer_stage=StageId.S6,
            output_refs=list(outcome.output_refs)))
        await record_skill_outcome(f"project_{act}", "ok" if succeeded else "fail", elapsed)
        await record_ai_subtask(
            skill=f"project_{act}", status="succeeded" if succeeded else "failed",
            risk=risk, duration_ms=elapsed)
        return succeeded, [], TaskResult(
            task_id=action.id,
            status="succeeded" if succeeded else "failed",
            output_refs=list(outcome.output_refs),
        )
