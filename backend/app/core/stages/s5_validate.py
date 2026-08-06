from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

from app.core.contracts import ResponseFragment, StageId, StageStatus, ValidationResult
from app.core.governance import action_requires_approval, action_risk_label, governance_basis
from app.core.turn_context import TurnContext
from app.domains.project import project_service
from app.analytics import record_intent_decision
from .base import BaseStage


class S5ValidateStage(BaseStage):
    """S5 校验/审批闸门(§5.6,最高危阶段)。

    多意图下的闸门策略（避免错盘漏盘）：
      - 无任何 action_item → 纯聊天放行（NO_OP + route 统计）；
      - 只要计划里含任一高危动作（由 ``app.core.governance.action_requires_approval``
        统一裁决：``ToolMeta.requires_approval`` ∪ 历史 publish/purge/trash 规则）
        → 整轮挂起审批(PAUSED)，
        由首个高危动作创建审批卡，并明确提示「其余动作待审批通过后一并执行」。
        这样不会出现「低危动作已落库、高危动作却被网关拦截」的半执行错盘；
      - 其余 → validation 通过(COMPLETED)，S6 串行执行所有低风险 action_item。
    高危动作的「真实执行」仍在审批决策端点(decide_approval)内，避免双执行路径。
    """

    stage_id = StageId.S5

    async def run(self, context: TurnContext):
        if context.plan is None or not context.plan.action_items:
            logger.debug("[S5] 无 action_item，纯聊天放行 turn=%s", context.turn_id)
            context.validation = ValidationResult(status="pass")
            await record_intent_decision("route", skill="chat", risk="low")
            return self.result(StageStatus.NO_OP, "no_executable_action")

        # 整轮闸门：任一高危动作即挂起审批，覆盖所有低风险兄弟动作。
        # 判定不再硬编码 {"publish","purge","trash"}，改由 app.core.governance 统一裁决
        # （ToolMeta.requires_approval ∪ 历史规则），保证 S5/S6/工具层同一套真相源。
        gated = [a for a in context.plan.action_items
                 if action_requires_approval(a.speech_act.value)]
        if gated:
            if self.session is None:
                logger.error("[S5] 缺少数据库会话,无法创建审批卡 turn=%s", context.turn_id)
                return self.result(StageStatus.FAILED, "no_session")
            action = gated[0]
            logger.info(
                "[S5] 含高危动作需审批 action=%s gated_count=%d basis=%s turn=%s",
                action.speech_act.value, len(gated),
                governance_basis(action.speech_act.value), context.turn_id,
            )
            approval = await project_service.request_approval(self.session, context, action.speech_act.value)
            has_other = len(context.plan.action_items) > 1
            nonce = approval.__dict__.pop("_decision_nonce")
            notes = ("此操作需要在审批卡中确认后才能继续。"
                     + ("其余已规划的动作将在审批通过后立即执行。" if has_other else ""))
            context.validation = ValidationResult(
                status="needs_approval",
                approval_id=approval.approval_id,
                reason_codes=["approval_required"],
                decision_nonce=nonce,
                response_fragments=[
                    ResponseFragment(
                        status="approval",
                        text=notes,
                        producer_stage=StageId.S5,
                        output_refs=[approval.approval_id],
                    )
                ],
            )
            context.response_fragments.extend(context.validation.response_fragments)
            # risk 标签同样走 governance（取 ToolMeta 与历史推导的上界，只升不降）。
            risk = action_risk_label(action.speech_act.value)
            await record_intent_decision("confirm", skill=f"project_{action.speech_act.value}", risk=risk)
            return self.result(StageStatus.PAUSED, "approval_created", output_refs=[approval.approval_id])

        # 必填信息收集闸门：**只读 S3 算好的 round_plan，不再自己推断**。
        #
        # 旧实现在这里现算缺失槽、现拼追问文案、还顺手往 SIR pending 里塞——
        # 与 S2（读 pending 决定续答抽槽）、S3（自清已填 pending）三处各持一份逻辑，
        # 结果就是用户反馈的两个症状：追问永远是那套模板、且完全不接前文。
        # v2 起唯一策略在 ``core.transition.plan_round``（纯函数、可单测），
        # S5 退化为**执行器**：按 action 下发，文案直接用 plan 现成的 followup_text。
        plan = context.round_plan
        if plan is not None and plan.action == "collect" and context.intent_bundle is not None:
            missing_keys = [a.slot_key for a in plan.agenda if a.action == "collect"]
            logger.info(
                "[S5] 建站必填信息未齐,挂起收集 turn=%s phase=%s 缺失=%s",
                context.turn_id, plan.phase.value, missing_keys,
            )
            frag = ResponseFragment(
                status="info",
                text=plan.followup_text,
                producer_stage=StageId.S5,
            )
            # 绑定首个 site action 作为 pending，待下一轮补齐槽位后由 S6 执行。
            pending_aid = next(
                (a.id for a in context.plan.action_items if a.domain.value == "site"),
                None,
            ) if context.plan else None
            # 注：**不再**在此处追加 SIR pending —— agenda 已是单一真相，
            # S3 已把它镜像成 pending 写进 sir_after_dst，下一轮 S2 照常能读到。
            # 快照也不再由 S5 落：轮末固化统一交给 S7（唯一状态固化点），
            # 避免同一轮出现两条 base 快照、让"最新一条"这个概念本身失去意义。
            context.validation = ValidationResult(
                status="needs_info",
                reason_codes=["missing_required_slots"],
                pending_action_id=pending_aid,
                response_fragments=[frag],
            )
            context.response_fragments.append(frag)
            await record_intent_decision("collect", skill="site", risk="low")
            return self.result(StageStatus.PAUSED, "needs_info")

        # 低置信澄清门控：LLM 升级把本有明确域的句子错判成闲聊（或内部歧义）时，
        # 不硬猜、直接反问用户，避免执行错误动作。正常高置信规则流不会进入本分支。
        if context.intent_bundle is not None and context.intent_bundle.needs_clarification:
            logger.info("[S5] 低置信意图，进入澄清 turn=%s", context.turn_id)
            frag = ResponseFragment(
                status="clarify",
                text="我不太确定您的意图，能否再补充说明一下您想做什么？"
                     "（例如：是要新建某个网站，还是修改 /删除整个项目 ？）",
                producer_stage=StageId.S5,
            )
            context.validation = ValidationResult(
                status="clarify",
                reason_codes=["needs_clarification"],
                response_fragments=[frag],
            )
            context.response_fragments.append(frag)
            await record_intent_decision("clarify", skill="unknown", risk="low")
            return self.result(StageStatus.COMPLETED, "needs_clarification")

        # ── edit 真实性兜底（2026-08-06 增补）──────────────────────────
        # 上游 S3.edit_mode 短路只在「意图含 SITE+EDIT 且存在可改 project」时判 edit，
        # 但仍可能指向「无已建成 artifact 的空 project」（首建站失败/占位/被软删）。
        # 这种悬空 edit 若放行，S6 会在错对象上改建或直接退化模板，用户侧即「页面还是组件库」。
        # 故 S5 作为最后一道闸门补验：被判 site_edit 但目标 project 查不到已建成 site
        # （verified / preview_ready）→ 打回澄清，让 S2 下一轮重新判断
        # （用户实际意图通常是不含 EDIT 触发词的新建，_is_edit_mode 会回落收集闸门）。
        target_kind = getattr(getattr(context.round_plan, "task", None), "kind", None)
        if target_kind == "site_edit" and self.session is not None:
            pid = context.prior_project_id or getattr(context.session, "project_id", None)
            if pid is not None:
                from sqlalchemy import select

                from app.models import Artifact

                exists = await self.session.scalar(
                    select(Artifact.id)
                    .where(
                        Artifact.project_id == pid,
                        Artifact.status.in_(["verified", "preview_ready"]),
                    )
                    .limit(1)
                )
                if exists is None:
                    logger.warning(
                        "[S5] 被判 site_edit 但 project=%s 无已建成 site，打回澄清 turn=%s",
                        pid, context.turn_id,
                    )
                    frag = ResponseFragment(
                        status="clarify",
                        text=("我没能找到可以修改的已生成网站（项目可能尚未建好或已被清除）。"
                              "是要新建一个网站，还是切换/指定某个已有的项目？"),
                        producer_stage=StageId.S5,
                    )
                    context.validation = ValidationResult(
                        status="clarify",
                        reason_codes=["edit_target_missing"],
                        response_fragments=[frag],
                    )
                    context.response_fragments.append(frag)
                    await record_intent_decision("clarify", skill="site_edit", risk="low")
                    return self.result(StageStatus.COMPLETED, "edit_target_missing")

        # 全为低风险动作：放行，S6 串行执行。
        context.validation = ValidationResult(status="pass")
        await record_intent_decision("route", skill="multi_action", risk="low")
        return self.result(StageStatus.COMPLETED, "validation_passed")
