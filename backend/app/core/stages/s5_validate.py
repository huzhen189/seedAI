from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

from app.core.contracts import ResponseFragment, StageId, StageStatus, ValidationResult
from app.core.governance import action_requires_approval, action_risk_label, governance_basis
from app.core.turn_context import TurnContext
from app.domains.project import project_service
from app.domains.project.guard import targeted_action_guard
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

        # ── 目标存在性/就绪校验（执行前前置闸门，必须早于审批卡创建与 S6 落地）────
        # 指向既有资源的意图（edit/review/publish/trash/restore/purge）若目标项目不存在 /
        # 已被清除 / 站点未建成，应在“让用户审批删除一个不存在的项目”之前就打回，
        # 而不是等到审批通过进 ops 执行时才报 project_not_found。
        # 判定规则由 app.domains.project.guard 统一提供（S5 与 ops 共用，单一真相源）。
        reject = await self._verify_targets_ready(context)
        if reject is not None:
            return reject

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

        # 全为低风险动作：放行，S6 串行执行。
        context.validation = ValidationResult(status="pass")
        await record_intent_decision("route", skill="multi_action", risk="low")
        return self.result(StageStatus.COMPLETED, "validation_passed")

    # 目标校验配置：domain.value + speech_act.value → 是否需要前置「目标存在/就绪」校验。
    # 凡是“指向既有资源”的意图都要列进来；create/research/chat 不需要既有资源，放行。
    #   - site/edit、site/review：除项目可操作外还需已建成站点；
    #   - project/publish|trash|restore|purge：项目须存在且未处于 purging。
    # 具体判定规则（状态白名单、鉴权、就绪）全部委托 app.domains.project.guard 单一真相源，
    # 此处只是“哪些意图需要校验”的开关表，便于一眼看清覆盖。
    _TARGET_REQUIREMENTS: "set[tuple[str, str]]" = {
        ("site", "edit"),
        ("site", "review"),
        ("project", "publish"),
        ("project", "trash"),
        ("project", "restore"),
        ("project", "purge"),
    }

    async def _verify_targets_ready(self, context: "TurnContext"):
        """执行前前置闸门：逐 action_item 校验目标存在性/就绪；任一不达标则打回 clarify。

        返回 None 表示全部通过；返回 StageStatus 结果表示已打回（调用方直接 return）。
        规则来自 guard.targeted_action_guard（与 ops 执行期共用同一套），S5 本就持 session；
        无 session 时不校验（保持旧行为，交由 ops 兜底）。
        """
        if self.session is None or context.plan is None:
            return None
        uid = context.user.user_id
        for a in context.plan.action_items:
            if (a.domain.value, a.speech_act.value) not in self._TARGET_REQUIREMENTS:
                continue
            pid = self._resolve_project_id(context, a)
            if pid is None:
                logger.warning("[S5] 意图 %s/%s 无法解析目标 project，打回 turn=%s",
                               a.domain.value, a.speech_act.value, context.turn_id)
                return await self._clarify_missing(
                    context, "project_unresolved",
                    "我没能确定要操作的项目，请先选择或指定一个项目。",
                    skill=f"{a.domain.value}_{a.speech_act.value}",
                )
            ok, code, text = await targeted_action_guard(
                self.session, pid, uid, a.speech_act.value
            )
            if not ok:
                logger.warning("[S5] 意图 %s/%s 前置不满足 code=%s project=%s,打回 turn=%s",
                               a.domain.value, a.speech_act.value, code, pid, context.turn_id)
                return await self._clarify_missing(
                    context, code, text, skill=f"{a.domain.value}_{a.speech_act.value}",
                )
        return None

    @staticmethod
    def _resolve_project_id(context: "TurnContext", action) -> "int | None":
        """解析某 action 的目标 project_id：优先用 action.target.id（若为整数），
        否则回落 prior_project_id / session.project_id。"""
        tid = getattr(getattr(action, "target", None), "id", None)
        if tid is not None and str(tid).isdigit():
            return int(tid)
        pid = context.prior_project_id or getattr(getattr(context, "session", None), "project_id", None)
        return pid

    async def _clarify_missing(self, context: "TurnContext", reason: str, text: str, *, skill: str):
        frag = ResponseFragment(status="clarify", text=text, producer_stage=StageId.S5)
        context.validation = ValidationResult(
            status="clarify", reason_codes=[reason], response_fragments=[frag],
        )
        context.response_fragments.append(frag)
        await record_intent_decision("clarify", skill=skill, risk="low")
        return self.result(StageStatus.COMPLETED, reason)
