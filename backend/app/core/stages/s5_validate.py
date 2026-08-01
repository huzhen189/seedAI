from __future__ import annotations

from app.core.contracts import ResponseFragment, StageId, StageStatus, ValidationResult
from app.core.turn_context import TurnContext
from app.domains.project import project_service
from .base import BaseStage


class S5ValidateStage(BaseStage):
    """S5 校验/审批闸门(§5.6,最高危阶段)。

    决定本轮是否可执行、是否需要用户审批:
      - 无可执行 action_item → 直接 pass(NO_OP);
      - 首个动作属于高危集 {publish, purge, trash} → 调 ``request_approval`` 创建审批卡,
        本阶段 PAUSED,把审批质询明文(只下发一次)交给 SSE 层,不落任何持久化载荷;
      - 其他 → validation 通过(COMPLETED)。
    高危动作的"真实执行"不在 S6,而在审批决策端点(decide_approval),避免双执行路径。
    """

    stage_id = StageId.S5

    async def run(self, context: TurnContext):
        if context.plan is None or not context.plan.action_items:
            logger.debug("[S5] 无可执行动作 turn=%s", context.turn_id)
            context.validation = ValidationResult(status="pass")
            return self.result(StageStatus.NO_OP, "no_executable_action")
        action = context.plan.action_items[0]
        if action.speech_act.value in {"publish", "purge", "trash"}:
            if self.session is None:
                raise RuntimeError("S5 requires a database session")
            logger.info("[S5] 高危动作需审批 action=%s turn=%s", action.speech_act.value, context.turn_id)
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
