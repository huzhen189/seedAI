from __future__ import annotations

from app.core.contracts import ExecutionResult, ResponseFragment, StageId, StageStatus, TaskResult
from app.core.turn_context import TurnContext
from app.domains.chat import chat_service
from app.domains.research import research_service
from app.domains.site import site_service
from .base import BaseStage


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
        context.execution = ExecutionResult(status="failed", committed=False)
        context.response_fragments.append(ResponseFragment(status="error", text="当前操作尚未具备可执行实现。", producer_stage=StageId.S6))
        return self.result(StageStatus.BLOCKED, "unsupported_project_action")
