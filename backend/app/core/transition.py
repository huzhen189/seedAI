"""SIR 轮转策略 —— 把「基于上一轮判断这一轮做什么」收口成**唯一的确定性纯函数**。

## 为什么需要它

改造前，"这轮该做什么"散落在三个阶段里各算各的：
  - S2 读 ``sir_base.pending`` 决定要不要走续答抽槽；
  - S5 用 ``_site_missing_required`` 现算缺失必填、现拼追问文案、顺手往 ``pending`` 写；
  - S3 又在合并后把"已填槽对应的 pending"自清一遍。
三处逻辑互相耦合又谁都不完整，于是出现了用户反馈的两个症状：
建站追问永远只问「样式风格 / 部署目标」那套模板、且**完全不接前文**。

现在统一为：``plan_round(prev, message, understanding, continuation) -> RoundPlan``。
输入是「上一轮状态 + 本轮消息 + 本轮理解 + 本轮承接」，输出是「下一轮状态 + 本轮动作」。
纯函数、零 LLM、零 I/O，因此**可单测、可回放、可回滚**。

## 承接（Continuation）在这里的位置

承接**不是槽位补丁，是状态机的输入信号**。它在此处只做两件事：
  1. **播种 task.goal**（首次承接时把前情摘要写进任务目标，并记 ``continuation_source``）；
  2. **免问 brief**（既然目标已由前情给定，就别再追问"网站要做什么"）。
幂等由 ``continuation.already_seeded`` 保证：一个 task 只吸收一次承接，
不会像 v1 那样每轮往 ``site.brief`` 后面追加 ``（承接：…）`` 滚雪球。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.continuation import Continuation, already_seeded
from app.core.contracts import (
    ActiveTask,
    AgendaItem,
    Domain,
    SirState,
    TaskPhase,
    UnderstandingResult,
)
from app.slots.layers import L0_OPTIONAL, L0_REQUIRED, SlotDef, SlotStack

# 建站必填闸门键（与 domains/site/workflow.build_spec 消费的 SIR 键一一对应）。
SITE_REQUIRED_SLOT_KEYS: tuple[str, ...] = (
    "site.name", "site.theme", "site.brief", "site.deploy_target",
)
# 「网站类型」参与阻塞：用户明确反馈过"没问我要做什么类型的网站"。
# 它在 layers 里是 L0_OPTIONAL（半自由：给推荐值 + 允许自定义），
# 但在建站闸门里按**必问**对待——否则永远问不到，用户诉求落空。
SITE_TYPE_KEY = "site.type"

_SLOT_DEFS: dict[str, SlotDef] = {s.key: s for s in [*L0_REQUIRED, *L0_OPTIONAL]}
# 承接断言 vs 追问的置信度分界：≥ 阈值直接断言承接，< 阈值先问一句确认。
CONTINUATION_ASSERT_THRESHOLD = 0.8

Action = Literal["chat", "clarify", "collect", "execute", "confirm"]


@dataclass(slots=True)
class RoundPlan:
    """一轮转移的完整决策结果（S3 产出，S5 执行，S7 固化）。"""

    task: ActiveTask | None
    agenda: list[AgendaItem]
    action: Action
    followup_text: str = ""
    # 本轮是否首次吸收了承接（S3 据此把承接摘要幂等写入 slots["site.brief"]）。
    seeded_continuation: bool = False
    # 批次 B 预留：本轮值得沉淀为长期记忆的线索（S7 喂给 persist_and_extract）。
    memory_hints: list[dict[str, Any]] = field(default_factory=list)

    @property
    def phase(self) -> TaskPhase:
        return self.task.phase if self.task else TaskPhase.IDLE


# ────────────────────────────────────────────────────────────── 内部判定原语


def _site_intent(understanding: UnderstandingResult) -> bool:
    return any(r.domain == Domain.SITE for r in understanding.resolved_intents)


def _other_executable_intent(understanding: UnderstandingResult) -> bool:
    """存在**非建站**的可执行意图（如删除项目 / 发布）——用户已切走话题。"""
    return any(
        r.executable and r.domain != Domain.SITE for r in understanding.resolved_intents
    )


def _delta_has_site_slots(understanding: UnderstandingResult) -> bool:
    return any(k.startswith("site.") for k in understanding.sir_delta.slots)


def _chat_intent(understanding: UnderstandingResult) -> bool:
    """本轮**任何**段被归为 CHAT 域（含「无触发词兜底桶」「社交寒暄」）。

    这是区分「续答」与「建站收集中插进的离题闲聊」的关键抓手：
    ``understand()`` 对无法归域的片段必然落进 CHAT 兜底桶
    （intent.py 的 _classify_segment 返回 None → Domain.CHAT），
    所以只要有闲聊，``resolved_intents`` 必含 CHAT 标签。
    """
    return any(r.domain == Domain.CHAT for r in understanding.resolved_intents)


def _mentions_site_topic(understanding: UnderstandingResult) -> bool:
    """用户本轮是否**主动提到建站话题**（显式建站意图 / 抽到 site.* 槽）。

    与 ``resuming`` 互补：``resuming`` 是「上一轮在收集态、本轮没切走」的粗信号；
    本函数用于**防回归**——当本轮虽有 CHAT、但用户话里明显在复述/点出站点要素时，
    不该被当成离题闲聊放走（例如用户虽用了口语，却在回答「网站叫什么」）。
    """
    return _site_intent(understanding) or _delta_has_site_slots(understanding)


# 建站域「答案词汇」兜底（防回归用）：用户回一句无触发词但确是收集答案时，
# 话里通常含这些词（部署目标 / 风格 / 类型）。S2 没抽成 site.* 槽时，靠它识别为
# 「真·答案」而非离题闲聊。刻意向「部署类」倾斜——这类词在 casual 闲聊里几乎不出现，
# 误判（把真闲聊当答案）成本最低；风格/类型词偏常见，仅作辅助、不作为唯一依据。
_BUILD_VOCAB: tuple[str, ...] = (
    "托管", "部署", "服务器", "云部署", "静态托管", "自托管",
    "vercel", "netlify", "阿里云", "腾讯云", "华为云", "aws",
    "官网", "电商", "博客", "社区", "落地页", "工具站", "展示站",
    "简约", "现代", "复古", "科技", "国风", "手绘", "像素",
    "蓝色", "红色", "绿色", "黑金", "配色", "主色",
)


def _has_build_vocab(message: str) -> bool:
    """话里是否含建站域答案词汇（确定性字符串匹配，零 LLM）。详见 ``_BUILD_VOCAB``。"""
    low = (message or "").lower()
    return any(v in low for v in _BUILD_VOCAB)


def _required_defs(understanding: UnderstandingResult) -> list[SlotDef]:
    """取本轮 slot_stack 的建站必填定义；栈缺失时回落 L0_REQUIRED 全集。

    回落很关键：续答轮（用户只回一句「平台托管」）常常没有域触发词，
    ``understand()`` 不会重算 slot_stack，此时若直接返回空，
    闸门会误判"必填已齐"而放行建站——正是收集流程半途失灵的隐藏路径。
    """
    ss = understanding.slot_stack
    if isinstance(ss, dict) and ss.get("slots"):
        try:
            stack = SlotStack.model_validate(ss)
            required = [s for s in stack.required if s.key in SITE_REQUIRED_SLOT_KEYS]
            if required:
                return required
        except Exception:  # noqa: BLE001 — 栈解析失败不得中断转移，回落全集
            pass
    return list(L0_REQUIRED)


def _slot_def(key: str) -> SlotDef | None:
    return _SLOT_DEFS.get(key)


def _migrate_pending_to_agenda(prev: SirState) -> list[AgendaItem]:
    """旧快照兼容：仅有 ``pending`` 的老会话，首次进入状态机时迁成 agenda。"""
    items: list[AgendaItem] = []
    for p in prev.pending:
        if not isinstance(p, dict) or not p.get("key"):
            continue
        items.append(
            AgendaItem(
                action="collect",
                slot_key=str(p["key"]),
                label=str(p.get("label") or p["key"]),
                prompt=str(p.get("prompt_hint") or ""),
            )
        )
    return items


def migrate_legacy_sir(state: SirState, *, origin_turn_id: str) -> SirState:
    """把 v1 快照（只有 ``slots``/``pending``）就地升格为 v2 状态机快照。

    由 S1 在加载基态后调用一次。不迁的后果很具体：老会话正卡在"等用户回答
    部署目标"，v2 上线后 ``task`` 为 ``None`` → ``plan_round`` 的**续答识别**
    （依赖 ``prev_task.phase ∈ {COLLECTING, CLARIFYING}``）不成立 → 用户回一句
    「平台托管」会被判成闲聊，收集流程当场断在升级那一刻。

    规则：``task`` 已存在则原样返回；否则若还有待收集项，合成一个 COLLECTING
    态的建站 task（id 带 ``legacy_`` 前缀，便于事后区分是迁移产物）。
    无待办则保持 ``task=None``（等价 IDLE），由 ``plan_round`` 按需新建。
    """
    if state.task is not None:
        return state
    agenda = state.agenda or _migrate_pending_to_agenda(state)
    if not agenda:
        return state
    origin = (origin_turn_id or "unknown")[:48]
    return state.model_copy(
        update={
            "agenda": agenda,
            "task": ActiveTask(
                id=f"legacy_{origin}",
                kind="site_build",
                domain=Domain.SITE,
                phase=TaskPhase.COLLECTING,
                created_turn_id=origin,
                updated_turn_id=origin,
            ),
        }
    )


# ────────────────────────────────────────────────────────────── 主策略


def plan_round(
    prev: SirState,
    message: str,
    understanding: UnderstandingResult,
    continuation: Continuation | None = None,
    *,
    turn_id: str,
    merged_slots: dict[str, Any] | None = None,
    project_bound: bool = False,
    high_risk: bool = False,
    edit_mode: bool = False,
) -> RoundPlan:
    """基于上一轮 SIR 计算本轮动作与下一轮状态。

    Args:
        prev: 上一轮结束时的 SIR（S1 按 ``conversations.canonical_sir_snapshot_id`` 精确加载）。
        message: 本轮 clean_message（当前仅用于日志/未来扩展，判定不依赖它，保持确定性）。
        understanding: S2 理解结果（多意图 + sir_delta + slot_stack）。
        continuation: S2 解析出的承接边（确定性、零 LLM）。
        turn_id: 本轮 turn_id，用于生成/更新 task 的稳定 id 与时间戳字段。
        merged_slots: S3 合并后的槽位（判"已填"的唯一真相）。
        project_bound: 会话是否已绑定既有项目（``site.name`` 可由项目名兜底）。
        high_risk: 本轮意图是否含需审批的高危动作。
        edit_mode: 本轮是对**既有站点**的受控编辑（回溯改样式 / EDIT 意图），
            此时必填闸门不适用——站已经存在，不该反过来追问"网站叫什么、要做什么"。

    Returns:
        RoundPlan —— 含下一轮 task/agenda 与本轮 action + 现成 followup 文案。
    """
    slots = dict(merged_slots or prev.slots)
    prev_task = prev.task
    prev_agenda = prev.agenda or _migrate_pending_to_agenda(prev)
    # 本轮值得沉淀为长期记忆的确定性线索（批次 B：S7 经 persist_and_extract 落库）。
    # 仅放「状态机比 LLM 抽自由文本更确定的结构化事实」，不重复 LLM 已能捕获的弱信号。
    memory_hints: list[dict[str, Any]] = []

    # ── 1. 本轮是否与"建站这件事"有关 ────────────────────────────────
    #    三种信号任一成立即视为相关：
    #      a) 显式识别出 SITE 意图；
    #      b) 本轮抽到了 site.* 槽位（如「简约风格」）；
    #      c) 上一轮正处在收集态且用户没有切走话题 —— **续答识别**。
    #    (c) 是状态机相对旧实现的关键增益：用户回一句「平台托管」，
    #    既无域触发词也抽不出槽，旧逻辑直接降级成闲聊，收集流程就此断掉。
    resuming = bool(
        prev_task
        and prev_task.status == "active"
        and prev_task.phase in (TaskPhase.COLLECTING, TaskPhase.CLARIFYING)
        and not _other_executable_intent(understanding)
    )

    # ── 0.5 建站收集中插入的离题闲聊（场景 A 对称处理）────────────────
    #    ``resuming`` 是「上一轮在收集态、本轮没切走」的**粗**信号，会把任何收集态下的
    #    消息都硬抬成续答——于是用户插一句「今天天气真好」这类离题闲聊会被当答案吞掉，
    #    聊天主链路（S6 chat 分支）触达不到，闲聊「接不住」。
    #    判定「真·离题闲聊」需**四个条件同时成立**，否则安全回落续答（宁吞错也不丢任务）：
    #      a) 正在 resuming（有活着的收集态建站 task）；
    #      b) 本轮确有 CHAT 段（说明存在无法归域的闲聊内容）；
    #      c) 用户**没主动提到建站话题**（无 SITE 意图 / 没抽 site.* 槽）；
    #      d) 话里**不含任何建站域答案词**（「托管/部署/官网/简约/蓝色…」）—— 这是防回归
    #         兜底：用户回「平台托管」这类无触发词但确是收集答案的句子，绝不能放走。
    #    该分支返回 action="chat" 但**保留 prev_task 与未完成 agenda**（不推进不销毁），
    #    闲聊结束后用户回到建站，task 相位仍 COLLECTING，未完成收集项原样留着。
    off_topic_chat = bool(
        resuming
        and _chat_intent(understanding)
        and not _mentions_site_topic(understanding)
        and not _has_build_vocab(message)
    )

    site_signal = _site_intent(understanding) or _delta_has_site_slots(understanding) or resuming

    if not site_signal:
        # 与建站无关：任务原样保留（不销毁，用户随时可回来续），本轮走聊天。
        return RoundPlan(
            task=prev_task,
            agenda=[a for a in prev_agenda if not a.done],
            action="chat",
        )

    if off_topic_chat:
        # 建站收集中插进的离题闲聊：保 task、保未完成 agenda，仅本轮走 chat 分支。
        # 注意返回的是「当前**未完成的** collect 项」（带上 done 过滤），
        # S3 写入 SirState 时它就是下一轮回看 SIR 时看到的 agenda（task 相位不变）。
        return RoundPlan(
            task=prev_task,
            agenda=[a for a in prev_agenda if not a.done],
            action="chat",
        )

    # ── 2. 新建 vs 沿用 task ─────────────────────────────────────────
    kind = "site_edit" if edit_mode else "site_build"
    if prev_task and prev_task.kind in ("site_build", "site_edit") and prev_task.status == "active":
        task = prev_task.model_copy(
            update={"updated_turn_id": turn_id, **({"kind": "site_edit"} if edit_mode else {})}
        )
    else:
        task = ActiveTask(
            id=f"task_{turn_id}",
            kind=kind,
            domain=Domain.SITE,
            phase=TaskPhase.COLLECTING,
            created_turn_id=turn_id,
            updated_turn_id=turn_id,
        )

    # 编辑既有站：必填闸门整段不适用（站已存在，追问"叫什么/做什么"是荒谬的）。
    # 直接判定为可执行；高危仍走审批。这条守卫必须在算 agenda 之前短路——
    # 否则新加的"slot_stack 缺失回落 L0_REQUIRED"会把回溯改样式误拦成新建收集。
    if edit_mode:
        if high_risk:
            task = task.model_copy(update={"phase": TaskPhase.AWAITING_APPROVAL})
            return RoundPlan(task=task, agenda=[], action="confirm")
        task = task.model_copy(update={"phase": TaskPhase.READY})
        return RoundPlan(
            task=task,
            agenda=[AgendaItem(action="execute", label="执行编辑")],
            action="execute",
        )

    # ── 3. 承接播种（幂等，一个 task 只吸收一次）──────────────────────
    cont = (
        continuation
        if continuation and continuation.relation == "references" and continuation.summary
        else None
    )
    seeded = False
    if cont and not already_seeded(cont, task):
        goal = cont.summary if not task.goal else f"{task.goal}；承接：{cont.summary}"
        task = task.model_copy(
            update={"goal": goal[:2048], "continuation_source": cont.source_turn_id}
        )
        seeded = True
        # 承接播种的**结构化 lineage** 是 LLM 抽文本未必能还原的事实：
        # 本任务派生自哪段前情。记为 project_exp 提示，S7 落 memories（确定性，不靠 LLM 是否听懂）。
        memory_hints.append(
            {
                "kind": "project_exp",
                "title": "建站承接前情",
                "body": f"用户承接此前关于「{cont.summary}」的讨论发起/继续建站。目标：{task.goal}",
                "payload": {"continuation_source": cont.source_turn_id},
            }
        )

    # ── 4. 已填集合 ─────────────────────────────────────────────────
    filled: set[str] = set(slots.keys())
    if project_bound:
        # 已绑定项目时站名由 Project.name 兜底，不该再追问。
        filled.add("site.name")
    if seeded or task.continuation_source:
        # 目标已由前情给定 —— 这正是"承接"要解决的问题：不再傻问"网站要做什么"。
        filled.add("site.brief")

    # 本轮用户**新指定**了网站类型 → 沉淀为站点风格软偏好（确定性事实，不必依赖 LLM 抽）。
    # 仅当上一轮基态尚无该槽时记录，避免跨轮累积重放（同一偏好靠 (user_id, tag) 幂等更新）。
    if SITE_TYPE_KEY in slots and SITE_TYPE_KEY not in (prev.slots or {}):
        memory_hints.append(
            {
                "kind": "user_pref",
                "tag": "site_type",
                "content": f"偏好网站类型：{slots[SITE_TYPE_KEY]}",
                "weight": 60,
            }
        )

    # ── 5. 计算 agenda ──────────────────────────────────────────────
    agenda: list[AgendaItem] = []
    # 低置信承接 → 先放一条 clarify，让追问带上"是不是承接它"的确认语气。
    if cont and cont.confidence < CONTINUATION_ASSERT_THRESHOLD:
        agenda.append(
            AgendaItem(
                action="clarify",
                label="承接确认",
                prompt=f"我注意到你前面在讨论「{cont.summary}」——这个网站是承接它来做的吗？",
            )
        )
    missing_defs = [s for s in _required_defs(understanding) if s.key not in filled]
    for s in missing_defs:
        agenda.append(
            AgendaItem(action="collect", slot_key=s.key, label=s.label, prompt=s.prompt_hint)
        )
    type_missing = SITE_TYPE_KEY not in filled
    if type_missing:
        td = _slot_def(SITE_TYPE_KEY)
        agenda.append(
            AgendaItem(
                action="collect",
                slot_key=SITE_TYPE_KEY,
                label=td.label if td else "网站类型",
                prompt=td.prompt_hint if td else "网站大致属于哪种类型",
            )
        )

    blocking = [a for a in agenda if a.action == "collect"]

    # ── 6. 相位与动作 ───────────────────────────────────────────────
    if blocking:
        task = task.model_copy(update={"phase": TaskPhase.COLLECTING})
        return RoundPlan(
            task=task,
            agenda=agenda,
            action="collect",
            followup_text=_compose_followup(cont, seeded, missing_defs, type_missing),
            seeded_continuation=seeded,
            memory_hints=memory_hints,
        )

    if high_risk:
        task = task.model_copy(update={"phase": TaskPhase.AWAITING_APPROVAL})
        return RoundPlan(
            task=task, agenda=[], action="confirm",
            seeded_continuation=seeded, memory_hints=memory_hints,
        )

    task = task.model_copy(update={"phase": TaskPhase.READY})
    return RoundPlan(
        task=task,
        agenda=[AgendaItem(action="execute", label="执行建站")],
        action="execute",
        followup_text=_compose_assertion(cont, seeded),
        seeded_continuation=seeded,
        memory_hints=memory_hints,
    )


# ────────────────────────────────────────────────────────────── 文案合成


def _compose_assertion(cont: Continuation | None, seeded: bool) -> str:
    """高置信承接时的一句"我知道你在接着上文说"断言。"""
    if cont and seeded and cont.confidence >= CONTINUATION_ASSERT_THRESHOLD:
        return f"这个网站会承接你之前关于「{cont.summary}」的讨论来做。"
    return ""


def _compose_followup(
    cont: Continuation | None,
    seeded: bool,
    missing: list[SlotDef],
    type_missing: bool,
) -> str:
    """收集态追问文案：承接说明 + 缺失必填 + 网站类型（全部在此一次拼好）。

    S5 只负责下发，不再自己拼 —— 文案与状态在同一处产生，才不会再次跑偏。
    """
    parts: list[str] = []
    if cont:
        if seeded and cont.confidence >= CONTINUATION_ASSERT_THRESHOLD:
            parts.append(f"这个网站会承接你之前关于「{cont.summary}」的讨论来做。")
        else:
            parts.append(
                f"我注意到你前面在讨论「{cont.summary}」——这个网站是承接它来做的吗？"
            )
    if missing:
        labels = "、".join(s.label for s in missing)
        parts.append(f"在动手搭建前，我还需要确认几项关键信息（{labels}）。")
        questions = [s.prompt_hint for s in missing if s.prompt_hint]
        if questions:
            parts.append("；".join(questions) + "。")
    if type_missing:
        parts.append(
            "另外，这个网站大致属于哪种类型？"
            "（如：展示官网 / 电商 / 工具-决策辅助 / 社区 / 个人 / 落地页，也可自定义描述）"
        )
    return "".join(parts)


def agenda_to_pending(agenda: list[AgendaItem]) -> list[dict[str, Any]]:
    """把 agenda 的 collect 项镜像成旧 ``pending`` 结构（向后兼容）。

    S2 的「续答 LLM 抽槽」仍读 ``sir_base.pending``；由 S3 统一镜像后，
    该路径零改动继续工作，也避免了 S5 再各写一份造成两处真相。
    """
    return [
        {"key": a.slot_key, "label": a.label, "prompt_hint": a.prompt}
        for a in agenda
        if a.action == "collect" and a.slot_key and not a.done
    ]


__all__ = [
    "RoundPlan",
    "SITE_REQUIRED_SLOT_KEYS",
    "SITE_TYPE_KEY",
    "agenda_to_pending",
    "migrate_legacy_sir",
    "plan_round",
]
