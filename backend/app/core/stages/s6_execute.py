from __future__ import annotations

from app.core.contracts import ExecutionResult, ResponseFragment, StageId, StageStatus, TaskResult
from app.core.turn_context import TurnContext
from app.domains.chat import chat_service
from app.domains.project import project_ops
from app.domains.research import research_service
from app.domains.site import site_service
from .base import BaseStage

# 高危项目操作由 S5 审批闸门承载，决策端点负责真实执行；
# S6 只直接执行低危动作，避免同一副作用出现两条执行路径。
_GATED_PROJECT_ACTIONS = frozenset({"publish", "trash", "purge"})


class S6ExecuteStage(BaseStage):
    stage_id = StageId.S6

    async def run(self, context: TurnContext):
        if context.validation is None or context.validation.status != "pass":
            return self.result(StageStatus.SKIPPED, "validation_not_pass")
        if context.plan is None or not context.plan.action_items:
            text = await chat_service.respond(context)
            context.response_fragments.append(ResponseFragment(status="success", text=text, producer_stage=StageId.S6))
            context.execution = ExecutionResult(status="succeeded", committed=True)
            return self.result(StageStatus.COMPLETED, "chat_completed")
        if self.session is None:
            raise RuntimeError("S6 requires a database session")
        action = context.plan.action_items[0]
        if action.domain.value == "site":
            artifact, text = await site_service.create_or_edit(self.session, context)
            context.execution = ExecutionResult(status="succeeded", committed=True, artifact_refs=[str(artifact.id)], task_results=[TaskResult(task_id=action.id, status="succeeded", output_refs=[str(artifact.id)])])
            context.response_fragments.append(ResponseFragment(status="success", text=text, producer_stage=StageId.S6, output_refs=[str(artifact.id)]))
            return self.result(StageStatus.COMPLETED, "site_artifact_created", output_refs=[str(artifact.id)])
        if action.domain.value == "research":
            text = await research_service.research(context)
            context.execution = ExecutionResult(status="succeeded", committed=True, task_results=[TaskResult(task_id=action.id, status="succeeded")])
            context.response_fragments.append(ResponseFragment(status="success", text=text, producer_stage=StageId.S6))
            return self.result(StageStatus.COMPLETED, "research_completed")
        if action.domain.value == "project":
            return await self._run_project_action(context, action)
        context.execution = ExecutionResult(status="failed", committed=False)
        context.response_fragments.append(ResponseFragment(status="error", text="当前操作尚未具备可执行实现。", producer_stage=StageId.S6))
        return self.result(StageStatus.BLOCKED, "unsupported_project_action")

    async def _run_project_action(self, context: TurnContext, action):
        """项目域执行：低危动作直落 ProjectOps，高危动作留给审批链路。"""
        session = self.session
        if session is None:
            raise RuntimeError("S6 project action requires a database session")
        act = action.speech_act.value
        if act in _GATED_PROJECT_ACTIONS:
            # 正常不会到这里(S5 会 PAUSED)；到了说明闸门被绕过，必须拒绝而不是执行。
            context.execution = ExecutionResult(status="failed", committed=False)
            context.response_fragments.append(
                ResponseFragment(status="error", text="该操作需要先通过审批确认。", producer_stage=StageId.S6)
            )
            return self.result(StageStatus.BLOCKED, "project_action_requires_approval")

        target_id = action.target.id
        project_id = int(target_id) if (target_id or "").isdigit() else (context.session.project_id or 0)
        if not project_id:
            context.execution = ExecutionResult(status="failed", committed=False)
            context.response_fragments.append(
                ResponseFragment(status="error", text="未能确定目标项目，请先选择项目。", producer_stage=StageId.S6)
            )
            return self.result(StageStatus.BLOCKED, "project_target_missing")

        outcome = await project_ops.execute(
            session,
            action=act,
            project_id=project_id,
            user_id=context.user.user_id,
            trace_id=context.trace_id,
        )
        succeeded = outcome.status == "succeeded"
        context.execution = ExecutionResult(
            status="succeeded" if succeeded else "failed",
            committed=outcome.committed,
            artifact_refs=list(outcome.output_refs),
            task_results=[
                TaskResult(
                    task_id=action.id,
                    status="succeeded" if succeeded else "failed",
                    output_refs=list(outcome.output_refs),
                )
            ],
        )
        context.response_fragments.append(
            ResponseFragment(
                status="success" if succeeded else "error",
                text=outcome.text,
                producer_stage=StageId.S6,
                output_refs=list(outcome.output_refs),
            )
        )
        if not succeeded:
            return self.result(StageStatus.BLOCKED, outcome.error_code or "project_action_failed")
        return self.result(StageStatus.COMPLETED, f"project_{act}_completed", output_refs=list(outcome.output_refs))
