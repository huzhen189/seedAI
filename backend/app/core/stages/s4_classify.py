from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

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
        context.intent_bundle, context.plan = classify(context.clean_message, context.understanding, context.prior_turn_id)
        n_actions = len(context.plan.action_items) if context.plan else 0
        # 逐条打印 action_item 的具体内容（id / 意图 / 域 / 言语行为 / 回溯绑定 / 依赖 / 参数摘要），
        # 此前仅打印条数，无法判断每个 action 实际要做什么。
        if context.plan and context.plan.action_items:
            for a in context.plan.action_items:
                args_preview = {k: (str(v)[:60] if not isinstance(v, (dict, list)) else f"<{type(v).__name__}>")
                                for k, v in (a.arguments or {}).items()}
                logger.info(
                    "[S4] action_item id=%s intent=%s domain=%s speech=%s prior_turn=%s depends_on=%s args=%s",
                    a.id, a.intent_id, a.domain.value, a.speech_act.value, a.prior_turn_id,
                    a.depends_on or "-", args_preview,
                )
        logger.info(
            "[S4] 生成受限计划: %d 个 action_item | max_risk=%s | has_gated=%s | serial=%s",
            n_actions,
            context.plan.max_risk.value if context.plan else "-",
            context.plan.has_gated if context.plan else "-",
            context.plan.serial if context.plan else "-",
        )
        return self.result(StageStatus.COMPLETED, "bounded_plan_created")
