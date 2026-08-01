from __future__ import annotations

from app.core.contracts import StageId, StageStatus
from app.core.turn_context import TurnContext
from app.router.intent import classify
from .base import BaseStage


class S4ClassifyStage(BaseStage):
    """S4 分类与受限计划(§5.6, BoundedPlan)。

    基于 S2 的理解结果调用 ``router.intent.classify`` 生成 intent_bundle 与受约束计划
    (plan.action_items)。无理解结果直接 FAILED(前置依赖缺失)。
    """

    stage_id = StageId.S4

    async def run(self, context: TurnContext):
        if context.understanding is None:
            logger.warning("[S4] 缺少 understanding,失败 turn=%s", context.turn_id)
            return self.result(StageStatus.FAILED, "understanding_required")
        context.intent_bundle, context.plan = classify(context.clean_message, context.understanding)
        n_actions = len(context.plan.action_items) if context.plan else 0
        logger.info("[S4] 生成受限计划: %d 个 action_item", n_actions)
        return self.result(StageStatus.COMPLETED, "bounded_plan_created")
