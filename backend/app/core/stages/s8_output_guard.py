from __future__ import annotations

from app.core.contracts import GuardResult, StageId, StageStatus
from app.core.turn_context import TurnContext
from .base import BaseStage


class S8OutputGuardStage(BaseStage):
    stage_id = StageId.S8

    async def run(self, context: TurnContext):
        fragments = [fragment.text for fragment in context.response_fragments if fragment.text]
        if context.validation is not None and context.validation.status == "needs_approval":
            context.reply_draft = "此操作正在等待审批。"
        elif context.validation is not None and context.validation.status == "block":
            context.reply_draft = "该操作已被安全策略阻止。"
        else:
            context.reply_draft = "\n\n".join(fragments) or "已完成本轮处理。"
        # Deterministic output guard: model/provider raw text never reaches this layer.
        context.reply_final = context.reply_draft.replace("<script", "&lt;script")
        context.guard_result = GuardResult(status="passed")
        return self.result(StageStatus.COMPLETED, "deterministic_output_guard")
