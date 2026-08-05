from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

from app.core.contracts import ResponseFragment, StageId, StageStatus, ValidationResult
from app.core.governance import action_requires_approval, action_risk_label, governance_basis
from app.core.turn_context import TurnContext
from app.domains.project import project_service
from app.analytics import record_intent_decision
from app.slots import SlotStack
from app.db.repositories import sir_snapshots as sir_repo
from .base import BaseStage

# 建站类动作的「执行前硬闸门」：必填 ``site.*`` 槽位未收集齐，则挂起执行、先反问收集。
# 这是此前"帮我做个网站吗"被直接建站的根因——S5 原本只检查高危/低置信/空计划，
# 完全没有"必填信息是否收集齐"这一支。A 方案已把必填 key 对齐到 ``site.*`` 命名空间
# （与 build_spec 实际消费的 SIR 键一致），本闸门才能成立。
_SITE_REQUIRED_SLOT_KEYS = ("site.name", "site.theme", "site.brief", "site.deploy_target")


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

        # 必填信息收集闸门（方案 A+B 硬闸门）：建站类 action 在「非高危、非澄清」的前提下，
        # 仍可能因必填槽位缺失而直接建出"无名/无主题/无风格"的空壳站。此处强制拦截：
        #   - 从 S2 的 slot_stack(required) 取必填 key（已是 site.* 命名空间）；
        #   - 用 S3 合并后的 sir_after_dst.slots 判"已填"（site.name 可由既有 project.name 兜底）；
        #   - 缺失则产出 needs_info：挂起执行 + 收集引导 fragment（走 chat 风格提问），
        #     并把首个待收集 action 绑定到 validation.pending_action_id，下一轮补齐槽位后放行 S6。
        if context.intent_bundle is not None and context.understanding is not None:
            missing = self._site_missing_required(context)
            if missing:
                logger.info(
                    "[S5] 建站必填信息未齐,挂起收集 turn=%s 缺失=%s",
                    context.turn_id, [s.key for s in missing],
                )
                labels = "、".join(s.label for s in missing)
                questions = []
                for s in missing:
                    if s.prompt_hint:
                        questions.append(s.prompt_hint)
                ask = "；".join(questions) if questions else f"请补充以下信息：{labels}"
                frag = ResponseFragment(
                    status="info",
                    text=f"在动手搭建前，我还需要确认几项关键信息（{labels}）。{ask}。",
                    producer_stage=StageId.S5,
                )
                # 绑定首个 site action 作为 pending，待下一轮补齐槽位后由 S6 执行。
                pending_aid = next(
                    (a.id for a in context.plan.action_items if a.domain.value == "site"),
                    None,
                ) if context.plan else None
                # 把待收集槽位写入 SIR pending，供下一轮 S2 识别「正在回答收集问题」并走 LLM 抽槽。
                # 去重：若基态已带同名待收集项不重复追加（避免多轮 needs_info 累积重复）。
                existing = {
                    p.get("key") for p in context.sir_after_dst.pending
                    if isinstance(p, dict)
                }
                for s in missing:
                    if s.key not in existing:
                        context.sir_after_dst.pending.append(
                            {"key": s.key, "label": s.label, "prompt_hint": s.prompt_hint}
                        )
                        existing.add(s.key)
                # 持久化 pending（新建/更新 SIR base 快照），使下一轮 S1 能加载到待收集清单。
                try:
                    snap = await sir_repo.insert(
                        self.session,
                        conversation_id=context.session.conversation_id,
                        turn_id=context.turn_id,
                        kind="base",
                        snapshot=context.sir_after_dst.model_dump(),
                        prev_snapshot_id=context.sir_after_dst_snapshot_id,
                    )
                    context.sir_after_dst_snapshot_id = snap.id
                except Exception as exc:  # noqa: BLE001 — 持久化失败不得中断收集
                    logger.warning("[S5] 持久化 pending 快照失败(非致命): %s", exc)
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

    # ------------------------------------------------------------ 硬闸门辅助
    @staticmethod
    def _site_missing_required(context: TurnContext) -> list:
        """返回建站 action 当前缺失的必填槽位（SlotDef 列表）。

        判定依据：
          - 仅当计划中存在 site 域 action（create/edit）时才需收集；
          - 必填 key 取自 understanding.slot_stack.required（已是 site.* 命名空间），
            与 domains/site/workflow.build_spec 消费的 SIR 键一致；
          - "已填"集合 = S3 合并后的 sir_after_dst.slots 键 ∪ 既有 project.name
            （site.name 可由 session.project_id 指向的既有项目名兜底，避免对已建项目追问站名）。
        """
        if context.plan is None or context.understanding is None:
            return []
        site_actions = [a for a in context.plan.action_items if a.domain.value == "site"]
        if not site_actions:
            return []
        ss = context.understanding.slot_stack
        if not isinstance(ss, dict) or not ss.get("slots"):
            return []
        try:
            stack_obj = SlotStack.model_validate(ss)
        except Exception:  # noqa: BLE001
            return []
        required = [s for s in stack_obj.required if s.key in _SITE_REQUIRED_SLOT_KEYS]
        if not required:
            return []
        # 已填集合：S3 合并后的 SIR 槽位键。
        filled: set[str] = set((context.sir_after_dst.slots or {}).keys())
        # site.name 兜底：若当前会话已绑定项目（project 已存在），其 name 即默认站名。
        project_id = getattr(context.session, "project_id", None)
        if project_id:
            filled.add("site.name")
        return [s for s in required if s.key not in filled]
