"""SIR 轮转状态机 ``plan_round`` 单元测试（纯函数、零 LLM、零 I/O）。

这些用例锁住的是**用户实际反馈过的三个症状**，而不是抽象契约：
  1. 「建站追问永远只问那套模板」→ 缺失槽必须真的按 slot_stack 算，且必问 site.type；
  2. 「完全不接前文」→ 承接必须播种 task.goal 且免问 brief；
  3. 「回一句『平台托管』就被当闲聊」→ 续答识别必须靠 prev_task.phase 成立。

外加两条防回归：承接只播种一次（v1 的 brief 滚雪球）、
编辑既有站不得触发必填闸门（新加的 L0_REQUIRED 回落别把回溯改样式误拦）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.continuation import Continuation
from app.core.contracts import (
    ActiveTask,
    AgendaItem,
    Domain,
    IntentItem,
    SirState,
    SpeechAct,
    TaskPhase,
    UnderstandingResult,
)
from app.core.transition import (
    SITE_REQUIRED_SLOT_KEYS,
    SITE_TYPE_KEY,
    agenda_to_pending,
    migrate_legacy_sir,
    plan_round,
)

ALL_SITE_SLOTS = {
    "site.name": "天气助手",
    "site.theme": "简约",
    "site.brief": "根据天气推荐雨具",
    "site.deploy_target": "平台托管",
    SITE_TYPE_KEY: "工具-决策辅助",
}


# ────────────────────────────────────────────────────────────── 构造辅助


def _site_intent(speech_act: SpeechAct = SpeechAct.CREATE) -> IntentItem:
    return IntentItem(
        id="i1",
        domain=Domain.SITE,
        speech_act=speech_act,
        intent_id="site.create",
        confidence=0.9,
        executable=True,
    )


def _project_intent() -> IntentItem:
    return IntentItem(
        id="i2",
        domain=Domain.PROJECT,
        speech_act=SpeechAct.TRASH,
        intent_id="project.trash",
        confidence=0.9,
        executable=True,
    )


def _understanding(intents: list[IntentItem] | None = None, slots: dict | None = None):
    u = UnderstandingResult(resolved_intents=intents or [])
    if slots:
        u = u.model_copy(
            update={"sir_delta": u.sir_delta.model_copy(update={"slots": slots})}
        )
    return u


def _cont(summary: str = "买雨伞好还是买雨衣好", confidence: float = 0.9) -> Continuation:
    return Continuation(
        relation="references",
        source_turn_id="t_umb",
        summary=summary,
        target_slots=["site.brief"],
        confidence=confidence,
    )


def _collect_keys(plan) -> set[str]:
    return {a.slot_key for a in plan.agenda if a.action == "collect"}


# ────────────────────────────────────────────────────────────── 用例


def test_new_site_request_creates_task_and_collects():
    """首轮「帮我做个网站」：新建 task，进入 COLLECTING，必填 + 类型全要问。"""
    plan = plan_round(
        SirState(),
        "帮我做个网站",
        _understanding([_site_intent()]),
        None,
        turn_id="t1",
        merged_slots={},
    )
    assert plan.action == "collect"
    assert plan.phase is TaskPhase.COLLECTING
    assert plan.task is not None and plan.task.id == "task_t1"
    assert plan.task.kind == "site_build"
    # 必填四项 + 半自由 site.type 一个都不能少（"永远只问那套模板"的反向断言）。
    assert _collect_keys(plan) == {*SITE_REQUIRED_SLOT_KEYS, SITE_TYPE_KEY}
    # 实际文案为"这个网站大致属于哪种类型？"，裸子串"网站类型"不连续，匹配完整片段。
    assert "这个网站大致属于哪种类型" in plan.followup_text


def test_continuation_seeds_goal_and_skips_brief():
    """承接高置信：播种 task.goal + 记 lineage，且不再追问 site.brief。"""
    plan = plan_round(
        SirState(),
        "做一个网站，根据天气推荐今天用哪个",
        _understanding([_site_intent()]),
        _cont(),
        turn_id="t1",
        merged_slots={},
    )
    assert plan.seeded_continuation is True
    assert plan.task is not None
    assert plan.task.goal == "买雨伞好还是买雨衣好"
    assert plan.task.continuation_source == "t_umb"
    # 承接的全部意义就在这一条：目标已由前情给定，别再傻问"网站要做什么"。
    assert "site.brief" not in _collect_keys(plan)
    assert "承接" in plan.followup_text


def test_continuation_seeded_only_once():
    """幂等：task 已带 lineage 时，第二轮承接不得再改写 goal（v1 的滚雪球回归防线）。"""
    prev = SirState(
        task=ActiveTask(
            id="task_t1",
            phase=TaskPhase.COLLECTING,
            goal="买雨伞好还是买雨衣好",
            continuation_source="t_umb",
            created_turn_id="t1",
            updated_turn_id="t1",
        )
    )
    plan = plan_round(
        prev,
        "平台托管",
        _understanding([_site_intent()]),
        # 下一轮很可能改指向"上一轮的追问消息"——异源承接更要挡住。
        _cont(summary="在动手搭建前，我还需要确认几项关键信息", confidence=0.95),
        turn_id="t2",
        merged_slots={},
    )
    assert plan.seeded_continuation is False
    assert plan.task is not None
    assert plan.task.goal == "买雨伞好还是买雨衣好"
    assert plan.task.continuation_source == "t_umb"


def test_resume_answer_without_domain_keyword_keeps_task():
    """续答识别：用户只回「平台托管」，无域触发词、无 site 槽，仍必须留在收集态。

    这正是收集流程半途失灵的路径——旧逻辑此时降级成闲聊，task 就此失联。
    """
    prev = SirState(
        task=ActiveTask(
            id="task_t1", phase=TaskPhase.COLLECTING,
            created_turn_id="t1", updated_turn_id="t1",
        ),
        slots={"site.name": "天气助手"},
    )
    plan = plan_round(
        prev,
        "平台托管",
        _understanding([]),  # 无任何解析出的意图
        None,
        turn_id="t2",
        merged_slots={"site.name": "天气助手", "site.deploy_target": "平台托管"},
    )
    assert plan.action == "collect"
    assert plan.task is not None and plan.task.id == "task_t1"  # 沿用，不另起
    assert plan.task.updated_turn_id == "t2"
    # 已答的两项不再问，剩下的照问。
    assert _collect_keys(plan) == {"site.theme", "site.brief", SITE_TYPE_KEY}


def test_all_slots_filled_goes_execute():
    """必填齐备（含 site.type）→ READY / execute。"""
    prev = SirState(
        task=ActiveTask(
            id="task_t1", phase=TaskPhase.COLLECTING,
            created_turn_id="t1", updated_turn_id="t1",
        )
    )
    plan = plan_round(
        prev, "都填好了", _understanding([_site_intent()]), None,
        turn_id="t3", merged_slots=dict(ALL_SITE_SLOTS),
    )
    assert plan.action == "execute"
    assert plan.phase is TaskPhase.READY
    assert [a.action for a in plan.agenda] == ["execute"]


def test_high_risk_goes_confirm():
    """信息齐备但含高危动作 → AWAITING_APPROVAL / confirm。"""
    plan = plan_round(
        SirState(), "发布并删掉旧的", _understanding([_site_intent()]), None,
        turn_id="t4", merged_slots=dict(ALL_SITE_SLOTS), high_risk=True,
    )
    assert plan.action == "confirm"
    assert plan.phase is TaskPhase.AWAITING_APPROVAL


def test_low_confidence_continuation_asks_to_clarify():
    """低置信承接 → agenda 首项是 clarify，追问带确认语气而非断言。

    关键前提：必须有未填槽（blocking 非空），否则会走 execute 分支把 clarify 顶掉。
    构造「无 merged_slots + 低置信承接」即满足：建站必填全缺 + 承接待确认。
    """
    plan = plan_round(
        SirState(), "做个网站", _understanding([_site_intent()]), _cont(confidence=0.7),
        turn_id="t5", merged_slots={},
    )
    assert plan.agenda[0].action == "clarify"
    assert "是承接它来做的吗" in plan.agenda[0].prompt


def test_topic_switch_keeps_task_but_chats():
    """用户切走话题（删项目）→ 本轮 chat，但 task 保留，随时可回来续。"""
    prev = SirState(
        task=ActiveTask(
            id="task_t1", phase=TaskPhase.COLLECTING,
            created_turn_id="t1", updated_turn_id="t1",
        )
    )
    plan = plan_round(
        prev, "把项目A删了", _understanding([_project_intent()]), None,
        turn_id="t6", merged_slots={},
    )
    assert plan.action == "chat"
    assert plan.task is not None and plan.task.id == "task_t1"


def test_edit_mode_skips_required_gate():
    """编辑既有站「改成浅色风格」→ 直接 execute，不得反过来追问站名/主题。"""
    plan = plan_round(
        SirState(), "改成浅色风格", _understanding([_site_intent(SpeechAct.EDIT)]), None,
        turn_id="t7", merged_slots={}, project_bound=True, edit_mode=True,
    )
    assert plan.action == "execute"
    assert plan.task is not None and plan.task.kind == "site_edit"
    assert _collect_keys(plan) == set()


def test_project_bound_skips_site_name():
    """已绑定项目 → site.name 由 Project.name 兜底，不追问。"""
    plan = plan_round(
        SirState(), "再加个联系我板块", _understanding([_site_intent()]), None,
        turn_id="t8", merged_slots={}, project_bound=True,
    )
    assert "site.name" not in _collect_keys(plan)


def test_agenda_to_pending_mirrors_collect_items_only():
    """pending 镜像只含未完成的 collect 项（S2 续答抽槽靠它零改动工作）。"""
    agenda = [
        AgendaItem(action="clarify", label="承接确认", prompt="?"),
        AgendaItem(action="collect", slot_key="site.theme", label="样式风格", prompt="想要什么风格"),
        AgendaItem(action="collect", slot_key="site.name", label="网站名", prompt="叫什么", done=True),
        AgendaItem(action="execute", label="执行建站"),
    ]
    pending = agenda_to_pending(agenda)
    assert pending == [
        {"key": "site.theme", "label": "样式风格", "prompt_hint": "想要什么风格"}
    ]


def test_migrate_legacy_sir_synthesizes_collecting_task():
    """v1 快照（只有 pending、无 task）升格后必须处于 COLLECTING，否则续答识别失效。"""
    legacy = SirState(
        slots={"site.name": "天气助手"},
        pending=[{"key": "site.deploy_target", "label": "部署目标", "prompt_hint": "部署到哪"}],
    )
    migrated = migrate_legacy_sir(legacy, origin_turn_id="t_old")
    assert migrated.task is not None
    assert migrated.task.phase is TaskPhase.COLLECTING
    assert migrated.task.id.startswith("legacy_")
    assert [a.slot_key for a in migrated.agenda] == ["site.deploy_target"]

    # 迁移后立刻续答：必须沿用迁移出来的 task，而不是另起一个。
    plan = plan_round(
        migrated, "平台托管", _understanding([]), None,
        turn_id="t_new",
        merged_slots={**ALL_SITE_SLOTS},
    )
    assert plan.action == "execute"
    assert plan.task is not None and plan.task.id == migrated.task.id


def test_migrate_legacy_sir_noop_when_idle():
    """无待办的老快照保持 task=None（等价 IDLE），不该凭空造任务。"""
    legacy = SirState(slots={"site.name": "x"})
    assert migrate_legacy_sir(legacy, origin_turn_id="t_old").task is None


def test_continuation_seeds_project_exp_memory_hint():
    """承接播种 → memory_hints 含一条 project_exp（lineage 结构化信号，供 S7 落 memories）。"""
    plan = plan_round(
        SirState(), "做一个网站，根据天气推荐今天用哪个",
        _understanding([_site_intent()]), _cont(),
        turn_id="t1", merged_slots={},
    )
    exps = [h for h in plan.memory_hints if h.get("kind") == "project_exp"]
    assert exps, "承接播种应产出 project_exp 记忆提示"
    assert exps[0]["payload"]["continuation_source"] == "t_umb"
    assert "买雨伞" in exps[0]["body"]


def test_new_site_type_emits_user_pref_hint_once():
    """本轮新指定 site.type → 沉淀 user_pref 提示（site_type 标签，靠 (user_id,tag) 幂等）。"""
    plan = plan_round(
        SirState(), "做个电商网站",
        _understanding([_site_intent()]), None,
        turn_id="t9", merged_slots={"site.type": "电商"},
    )
    prefs = [h for h in plan.memory_hints if h.get("kind") == "user_pref"]
    assert prefs, "新指定网站类型应产出 user_pref 记忆提示"
    assert prefs[0]["tag"] == "site_type"
    assert "电商" in prefs[0]["content"]

    # 反例：上一轮基态已有 site.type → 不再重复提示（避免跨轮累积重放）。
    prev = SirState(slots={"site.type": "电商"})
    plan2 = plan_round(
        prev, "再加个会员板块", _understanding([_site_intent()]), None,
        turn_id="t10", merged_slots={"site.type": "电商", "site.sections": ["会员"]},
    )
    assert not [h for h in plan2.memory_hints if h.get("kind") == "user_pref"]


def _collecting_site_task(turn_id: str = "t_build") -> SirState:
    """构造一个「正在收集态」的建站 task 基态（模拟用户已说『帮我做个网站』、还差几项）。"""
    task = ActiveTask(
        id=f"task_{turn_id}",
        kind="site_build",
        domain=Domain.SITE,
        phase=TaskPhase.COLLECTING,
        created_turn_id=turn_id,
        updated_turn_id=turn_id,
    )
    agenda = [
        AgendaItem(action="collect", slot_key="site.theme", label="样式风格", prompt="想要什么风格？"),
        AgendaItem(action="collect", slot_key="site.deploy_target", label="部署目标", prompt="部署到哪里？"),
    ]
    return SirState(task=task, agenda=agenda, slots={"site.name": "天气助手"})


def test_off_topic_chat_during_collection_keeps_task():
    """场景 A：建站收集中插进的离题闲聊（『今天天气真好』）应走 chat，
    但**保留 task 与未完成 agenda**（不推进、不销毁），闲聊后用户能回来续建站。

    用等价的 CHAT 段构造（与 ``understand()`` 对无触发词片段兜底成 Domain.CHAT 的
    输出字段一致），避免该测试拉起 ``app.router.intent`` → ``app.llm.client`` → openai
    的 import 链（测试环境无 openai）。判定前件是「本轮确有 CHAT 段」，此处手搓一个即可。
    """
    base = _collecting_site_task()
    chat = IntentItem(
        id="i1", domain=Domain.CHAT, speech_act=SpeechAct.ASK, intent_id="chat_ask",
        confidence=0.6, executable=False, raw_segment="今天天气真好",
    )
    # 纯口语、无建站触发词、无 site.* 槽、无建站域答案词 → 判定为离题闲聊。
    plan = plan_round(
        base, "今天天气真好", _understanding([chat]), None,
        turn_id="t_int", merged_slots=base.slots,
    )
    assert plan.action == "chat"
    # task 原样保留（相位仍是 COLLECTING，不被推进到 READY）。
    assert plan.task is not None
    assert plan.task.id == base.task.id
    assert plan.task.phase is TaskPhase.COLLECTING
    # 未完成的收集项原样带出（用户回来继续建站时 SIR 仍能看到还差什么）。
    assert {a.slot_key for a in plan.agenda if not a.done} == {"site.theme", "site.deploy_target"}


def test_plain_answer_in_collection_still_resumes_not_chat():
    """防回归：建站收集中回一句无触发词但确是收集答案的『平台托管』必须续答，
    不能被误判成离题闲聊放走（否则部署目标永远追问不到）。用户痛点原文。

    故意构造一个 CHAT 段（若只简单传空 intents，『平台托管』会因 resuming 直接续答，
    测不到 off_topic_chat 判定被**正确否决**）；含建站域答案词 → _has_build_vocab 拦住。
    """
    base = _collecting_site_task()
    chat = IntentItem(
        id="i1", domain=Domain.CHAT, speech_act=SpeechAct.ASK, intent_id="chat_ask",
        confidence=0.6, executable=False, raw_segment="平台托管",
    )
    plan = plan_round(
        base, "平台托管", _understanding([chat]), None,
        turn_id="t_resume", merged_slots={"site.name": "天气助手", "site.deploy_target": "平台托管"},
    )
    assert plan.action == "collect"  # 续答：仍走收集（补齐 site.theme / site.type），而非 chat
    assert plan.task is not None and plan.task.id == base.task.id
    assert "site.deploy_target" not in _collect_keys(plan)  # 答案已吸收，不再追问


def test_chat_intent_in_collection_with_site_vocab_resumes():
    """防回归 2：用户在闲聊里顺嘴提到建站要素（如『蓝色挺好的』），
    即使话落进 CHAT 段也不该放走——必须续答。靠 _has_build_vocab 兜底识别。

    同样故意构造 CHAT 段激活 off_topic_chat 判定，验证其被 _has_build_vocab 正确否决。
    """
    base = _collecting_site_task()
    chat = IntentItem(
        id="i1", domain=Domain.CHAT, speech_act=SpeechAct.ASK, intent_id="chat_ask",
        confidence=0.6, executable=False, raw_segment="蓝色挺好的",
    )
    plan = plan_round(
        base, "蓝色挺好的", _understanding([chat]), None,
        turn_id="t_blue", merged_slots=base.slots,
    )
    assert plan.action == "collect"  # 识别为续答（用户在回答主题/风格），不放走为 chat
    assert plan.task is not None and plan.task.id == base.task.id


def test_continuation_hint_text_renders_reference():
    from app.core.continuation import continuation_hint_text

    hint = continuation_hint_text(_cont(summary="买雨伞好还是买雨衣好", confidence=0.9))
    assert hint is not None
    assert "买雨伞好还是买雨衣好" in hint
    assert "承接" in hint


def test_continuation_hint_text_none_when_independent():
    from app.core.continuation import continuation_hint_text

    assert continuation_hint_text(None) is None
    assert continuation_hint_text(Continuation()) is None  # relation=independent 默认
    assert continuation_hint_text(Continuation(relation="references", summary="")) is None
