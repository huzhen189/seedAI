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
            context.validation = ValidationResult(
                status="needs_approval",
                approval_id=approval.approval_id,
                reason_codes=["approval_required"],
                # 质询明文只在内存里交给 SSE 层下发一次(字段 exclude=True, 不进任何持久化载荷)。
                # 绝不可放进 response_fragments —— 那会被拼进 assistant 正文并落库到 messages。
                decision_nonce=approval.__dict__.pop("_decision_nonce"),
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
            return self.result(StageStatus.PAUSED, "approval_created", output_refs=[approval.approval_id])
        context.validation = ValidationResult(status="pass")
        return self.result(StageStatus.COMPLETED, "validation_passed")
