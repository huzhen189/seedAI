from __future__ import annotations

from app.core.contracts import ResponseFragment, StageId, StageStatus, ValidationResult
from app.core.turn_context import TurnContext
from app.domains.project import project_service
from .base import BaseStage


class S5ValidateStage(BaseStage):
    stage_id = StageId.S5

    async def run(self, context: TurnContext):
        if context.plan is None or not context.plan.action_items:
            context.validation = ValidationResult(status="pass")
            return self.result(StageStatus.NO_OP, "no_executable_action")
        action = context.plan.action_items[0]
        if action.speech_act.value in {"publish", "purge", "trash"}:
            if self.session is None:
                raise RuntimeError("S5 requires a database session")
            approval = await project_service.request_approval(self.session, context, action.speech_act.value)
            nonce = approval.__dict__.pop("_decision_nonce")
            context.validation = ValidationResult(
                status="needs_approval",
                approval_id=approval.approval_id,
                reason_codes=["approval_required"],
                response_fragments=[
                    ResponseFragment(
                        status="approval",
                        text="该操作需要在审批卡中确认后才能继续。",
                        producer_stage=StageId.S5,
                        output_refs=[approval.approval_id],
                    )
                ],
            )
            context.response_fragments.extend(context.validation.response_fragments)
            # nonce is emitted once; only its hash is persisted.
            context.response_fragments.append(ResponseFragment(status="approval", text=nonce, producer_stage=StageId.S5, output_refs=[approval.approval_id]))
            return self.result(StageStatus.PAUSED, "approval_created", output_refs=[approval.approval_id])
        context.validation = ValidationResult(status="pass")
        return self.result(StageStatus.COMPLETED, "validation_passed")
