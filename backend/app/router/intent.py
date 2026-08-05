"""S2/S4 的确定性优先 + 多意图 + 分句 + LLM 升级 结构化意图理解。

设计目标（闭合所有分支，避免「过度裁剪 / 错盘漏盘」）：
  - 方案② 分句：按标点 / 连词把句子切段，每段独立识别，天然解决跨段误并。
  - 方案① 多信号采集：全句扫描所有命中的词类，每个产出一个候选意图；
            不再 if/elif first-match 即 return。
  - CHAT 兜底桶（additive）：任何没被其它明确域消费的文本都归到 CHAT，
            保证「闲聊 + 建站」这类复合句不被整句丢弃。
  - 方案③ LLM 升级：当无法用规则稳妥分解（多候选低置信 / 零命中 / 歧义）时，
            单次 LLM 全局推理输出结构化意图 + 依赖，带 JSON 自愈环；
            LLM 失败 / 不可用则安全降级回规则结果。

所有副作用都落在返回的 UnderstandResult 上；intent 识别是确定性优先，LLM 仅作兜底。
"""

from __future__ import annotations

import json
import logging
import re

from app.core.contracts import (
    ActionItem,
    BoundedPlan,
    Domain,
    IntentBundle,
    IntentCandidate,
    IntentItem,
    IntentMethod,
    RiskLevel,
    SirDelta,
    SpeechAct,
    TargetRef,
    TargetType,
    UnderstandingResult,
    UtteranceFrame,
)
from app.llm import LLMError, chat_completion

logger = logging.getLogger(__name__)

# 触发词表 / 槽位映射：真相源在 intent_config.json（改词热更新，无需改代码重启）。
# 详见 intent_config.py —— 这里只引用，不重新定义。
from .intent_config import (  # noqa: E402
    SITE_WORDS, RESEARCH_WORDS, PUBLISH_WORDS, PURGE_WORDS, RESTORE_WORDS,
    TRASH_WORDS, EDIT_WORDS, CREATE_WORDS, SOCIAL_WORDS,
    _THEME_MAP, _SITE_TYPE_MAP, _SECTION_MAP, _DEPLOY_MAP, _STYLE_WORDS,
)
from app.prompts import INTENT_ESCALATION_PROMPT as _ESCALATION_PROMPT  # 提示词集中于 app/prompts
from app.config import settings
from app.slots import compose, detect_dynamic_slots, detect_industry  # A 方案：分层槽位体系（确定性拼装）
# 运行期软上限（可配置），硬护栏仍是 contracts.MAX_ACTION_ITEMS（数据模型 max_length）。
_MAX_ACTION_ITEMS = settings.max_action_items
from app.ragstore import (
    retrieve as _rag_retrieve,
    safe_upsert_bg as _rag_upsert_bg,
    format_hits_for_prompt as _fmt_hits,
)
# 治理判定真相源：S4 标 has_gated 必须与 S5 闸门同源，否则会出现
# 「计划说不用审批、S5 却挂起」的口径分裂（前端执行计划与实际行为对不上）。
from app.core.governance import action_requires_approval  # noqa: E402


# 分段标点（直接切断）。
_SEG_PUNCT_RE = re.compile(r"[！!。．\\.\n;；]+")
# 分句连词：在其「之前」切断（连词本身作为前段残片被丢弃，后段成为独立子句）。
# 注意：把「再 / 然后 / 之后」等时序连词放在这里，才能正确地把
# 「删掉旧站，再新建博客」拆成「删掉旧站」+「新建博客」两段，避免后段被吞。
_SEG_CONJ_RE = re.compile(r"(?:并且|另外|同时|还有|以及|顺带|而且|再加上|另外再|并且要|然后再?|之后|然后|再)")


def _segment(message: str) -> list[str]:
    """方案②：把复合句切段。

    两步切分：
      1. 标点直接切断；
      2. 在时序/并列连词（再/然后/之后/另外/同时…）「之前」切断，
         使「删掉旧站，再新建博客」正确拆成「删掉旧站」+「新建博客」。
    空段、纯连词残片丢弃；保留原始段顺序。无切点则整句作为单一段。
    """
    raw = message.strip()
    if not raw:
        return []
    # 先标点切
    parts = _SEG_PUNCT_RE.split(raw)
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 再连词切（在连词之前切）
        sub = _SEG_CONJ_RE.split(part)
        for s in sub:
            s = s.strip()
            if s and not _SEG_CONJ_RE.fullmatch(s):
                out.append(s)
    return out or [raw]


def _split_social(message: str) -> tuple[str, str]:
    """剥离社交前缀。返回 (社交前缀, 余下文本)。"""
    low = message.lower()
    prefix = ""
    rest = message
    for w in SOCIAL_WORDS:
        if w in low:
            # 取第一个社交词之前的内容作为寒暄（含该词）
            idx = low.find(w) + len(w)
            prefix = message[:idx]
            rest = message[idx:]
            break
    return prefix.strip(), rest.strip()


# 单类别识别的纯函数：给定一段文本，返回命中的 (domain, speech, executable, risk, skill, signals)。
# 返回 None 表示这段文本没有命中任何明确域（应归 CHAT 兜底）。
def _classify_segment(seg: str) -> dict | None:
    text = seg.lower()
    sig: list[str] = []

    def hit(words: tuple[str, ...]) -> bool:
        for w in words:
            if w in text:
                sig.append(w)
                return True
        return False

    if hit(PURGE_WORDS):
        return {"domain": Domain.PROJECT, "speech": SpeechAct.PURGE, "executable": True,
                "risk": RiskLevel.CRITICAL, "skill": None}
    if hit(RESTORE_WORDS):
        return {"domain": Domain.PROJECT, "speech": SpeechAct.RESTORE, "executable": True,
                "risk": RiskLevel.LOW, "skill": None}
    if hit(PUBLISH_WORDS):
        return {"domain": Domain.PROJECT, "speech": SpeechAct.PUBLISH, "executable": True,
                "risk": RiskLevel.CRITICAL, "skill": None}
    if hit(RESEARCH_WORDS):
        return {"domain": Domain.RESEARCH, "speech": SpeechAct.ASK, "executable": True,
                "risk": RiskLevel.LOW, "skill": None}
    # 「删除/移除/回收 + 网站/官网/站点」→ 删除项目(TRASH)，优先级高于 SITE 的 EDIT 兜底。
    # 例：「删除我的官网」「把那个门户回收」应判删除而非改站；否则会落入下方 SITE 分支被当 EDIT。
    # 注意：仅当同段同时命中删词与站点词才提升，避免「删除联系我们板块」这类纯板块操作被误升。
    if hit(TRASH_WORDS) and hit(SITE_WORDS):
        return {"domain": Domain.PROJECT, "speech": SpeechAct.TRASH, "executable": True,
                "risk": RiskLevel.HIGH, "skill": None}
    if hit(SITE_WORDS):
        if hit(EDIT_WORDS):
            return {"domain": Domain.SITE, "speech": SpeechAct.EDIT, "executable": True,
                    "risk": RiskLevel.LOW, "skill": "site"}
        if hit(CREATE_WORDS):
            return {"domain": Domain.SITE, "speech": SpeechAct.CREATE, "executable": True,
                    "risk": RiskLevel.LOW, "skill": "site"}
        # 命中建站语境但无明确动词：视作对已落站的「细化/讨论」，至少可执行一次 EDIT。
        return {"domain": Domain.SITE, "speech": SpeechAct.EDIT, "executable": True,
                "risk": RiskLevel.LOW, "skill": "site"}
    if hit(TRASH_WORDS):
        return {"domain": Domain.PROJECT, "speech": SpeechAct.TRASH, "executable": True,
                "risk": RiskLevel.HIGH, "skill": None}
    return None


def _build_item(seg_idx: int, seg: str, info: dict) -> IntentItem:
    dom: Domain = info["domain"]
    sp: SpeechAct = info["speech"]
    return IntentItem(
        id=f"i{seg_idx}",
        domain=dom,
        speech_act=sp,
        intent_id=f"{dom.value}_{sp.value}",
        target=TargetRef(type=TargetType.PROJECT if dom in (Domain.SITE, Domain.PROJECT) else TargetType.NONE),
        arguments={"message": seg, "skill": info.get("skill")},
        confidence=0.9,  # 规则确定性置信；LLM 升级路径会覆盖。
        executable=bool(info["executable"]),
        risk_hint=info["risk"],
        method=IntentMethod.RULE,
        raw_segment=seg[:2048],
        skill=info.get("skill"),
    )


# ----------------------------------------------------------------- 槽位抽取（DST 增量）
# 槽位映射（_THEME_MAP / _SITE_TYPE_MAP / _SECTION_MAP / _STYLE_WORDS）已在文件顶部
# 从 intent_config 导入，真相源见 intent_config.json（改词热更新，无需改代码重启）。


def _extract_slots(message: str, resolved: list[IntentItem]) -> SirDelta:
    """把本轮自然语言抽成**结构化 SIR 增量**（DST 的真正输入）。

    此前 ``UnderstandingResult.sir_delta`` 全链路从未被赋值，永远是空 SirDelta，
    导致 S3 恒 NO_OP、快照永不落库、S1 加载的基态永远为空——即"多轮对话没有记忆、
    回溯改站丢失上下文"的根因。这里做确定性抽取（零 LLM 成本）：

      - ``site.theme`` / ``site.type`` / ``site.style``：单值槽位，本轮命中即覆盖；
      - ``site.sections``：多值槽位，S3 按并集合并（"再加联系我"不会抹掉已有板块）；
      - ``site.subject``：站点主体（从首个 site 意图的原始分句里取名词性前缀）。

    只在存在 site 域意图时抽取站点槽位，避免污染纯闲聊轮的状态。
    """
    text = message.lower()
    slots: dict[str, object] = {}
    site_items = [r for r in resolved if r.domain == Domain.SITE]

    for words, canonical in _THEME_MAP:
        if any(w in text for w in words):
            slots["site.theme"] = canonical
            break
    for words, canonical in _SITE_TYPE_MAP:
        if any(w in text for w in words):
            slots["site.type"] = canonical
            break
    # 部署目标：此前完全没有抽取逻辑，导致「平台托管」这类回答永远填不上 deploy_target，
    # 与 theme 一起成为 needs_info 死槽。补确定性映射（platform/custom/local），零 LLM 成本。
    for words, canonical in _DEPLOY_MAP:
        if any(w in text for w in words):
            slots["site.deploy_target"] = canonical
            break
    sections = [key for words, key in _SECTION_MAP if any(w in text for w in words)]
    if sections:
        slots["site.sections"] = sections
    styles = [w for w in _STYLE_WORDS if w in text]
    if styles:
        slots["site.style"] = styles

    # 站点主体：仅当该 site 意图是「新建」时回填——回溯改站(EDIT)不应把"改成浅色风格"
    # 这类修改指令当成站点简介覆盖掉上一轮沉淀的 brief（否则关于页会变成一句改站指令）。
    if site_items and site_items[0].speech_act == SpeechAct.CREATE and site_items[0].raw_segment:
        slots["site.brief"] = site_items[0].raw_segment[:512]

    if not site_items:
        # 非建站轮不写 site.* 槽位，只保留通用的最近话题（供跨轮引用消解）。
        slots = {k: v for k, v in slots.items() if not k.startswith("site.")}

    constraints: list[dict[str, object]] = []
    if slots.get("site.theme"):
        constraints.append({"kind": "theme", "value": slots["site.theme"], "source": "user_utterance"})
    return SirDelta(slots=slots, constraints=constraints)


def _build_slot_stack(message: str, resolved: list[IntentItem]) -> dict | None:
    """按行业/类型确定性拼装分层槽位栈（A 方案），序列化为 dict 挂到 UnderstandingResult。

    无 SITE 意图的轮次返回 ``None``（纯闲聊不需要槽位引导）。行业用 ``detect_industry``
    关键词命中；类型复用已有的 ``_SITE_TYPE_MAP`` 抽取（与 ``_extract_slots`` 一致）。
    槽位真相源是 ``app.slots`` 注册表，零 LLM、零网络。
    """
    if not any(r.domain == Domain.SITE for r in resolved):
        return None
    industry = detect_industry(message)
    text = message.lower()
    site_types: list[str] = []
    for words, canonical in _SITE_TYPE_MAP:
        if any(w in text for w in words):
            site_types.append(canonical)
            break
    # L3 动态业务槽：命中触发词即加入待收集栈（用户提及的业务概念 → 后续收集 + S3 持久化）
    dynamic = detect_dynamic_slots(message)
    stack = compose(industry, site_types, dynamic=dynamic)
    return stack.model_dump()


def recompute_slots(message: str, understanding: UnderstandingResult) -> UnderstandingResult:
    """S2 回溯域继承之后重算 ``sir_delta`` 与分层槽位栈（A 方案）。

    为什么必须独立出来：`understand()`` 在 ``inherit_retro_domain`` **之前**运行，
    对回溯改站这种"指令不含任何域触发词"的轮次，原始 `resolved_intents` 全是 CHAT，
    于是 ``_extract_slots`` 抽到的 ``site.theme``/``site.sections`` 等槽位值会被末尾的
    `if not site_items: 丢弃 site.* `` 逻辑整批抹掉——结果 S3 合并为零变更(NO_OP)，
    既不落快照、也不改 spec。域继承把 CHAT 提升成 SITE **之后**，必须用提升后的
    `resolved_intents` 重新跑一遍槽位抽取，才能把"改成浅色 + 加联系我"沉淀进 DST。
    槽位栈同理：域提升后才确定行业/类型，才能拼出正确的待收集槽位。
    """
    stack = _build_slot_stack(message, understanding.resolved_intents)
    return understanding.model_copy(update={
        "sir_delta": _extract_slots(message, understanding.resolved_intents),
        "slot_stack": stack,
    })


# ----------------------------------------------------------------- 续答 LLM 抽槽（待收集清单回填）
# 触发条件：上一轮 S5 挂起收集（SIR pending 非空），且本轮确定性抽取仍缺失部分待收集槽位。
# 把「用户本轮自由文本回答 + 待收集槽定义 + 已填信息」交给一次 LLM，产出结构化槽位值回填
# sir_delta，使「手绘的卡通像素风格，平台托管」这类自由描述能真正补齐必填槽，避免反复追问。
_FILL_SYSTEM = (
    "你是建站需求收集助手。用户正在回答上一轮系统提出的问题（待收集槽位）。"
    "请根据用户原话，把对应的待收集槽位填充为结构化值，只输出一个 JSON 对象。\n"
    "规则：\n"
    "1. 键为待收集槽位 key，值为填充结果；用户原话未提及的槽位不要输出。\n"
    "2. site.deploy_target 必须是以下之一：platform（平台托管/线上托管）、"
    "custom（自有域名/自定义域名）、local（本地预览/本地部署）。\n"
    "3. site.theme（整体视觉风格，如简约/科技感/手绘卡通等）值为字符串，例如 \"手绘卡通像素风格\"。\n"
    "4. site.name 为网站名称（简短，如 \"花间集\"）。\n"
    "5. site.brief 为网站内容主题/主要目的（简短一句话）。\n"
    "6. 不要编造未提及的信息，不要输出 JSON 以外的文本。"
)


def _coerce_fill_value(key: str, value: object) -> object | None:
    """把 LLM 回填值归一化为合法 SIR 槽值；无法归一化返回 None（下一轮重新 ask）。"""
    if value is None:
        return None
    if key == "site.deploy_target":
        if value in ("platform", "custom", "local"):
            return value
        text = str(value).lower()
        for words, canonical in _DEPLOY_MAP:
            if any(w in text for w in words):
                return canonical
        return None
    if key == "site.style":
        if isinstance(value, list):
            out = [str(x) for x in value if str(x).strip()]
            return out or None
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return None
    # site.theme / site.name / site.brief：接受非空字符串
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def fill_await_slots(
    message: str, pending_defs: list[dict], base_slots: dict
) -> dict:
    """续答抽槽：把待收集槽位的自由文本回答解析为结构化 slot 值。

    Args:
        message: 用户本轮原话（回答）。
        pending_defs: 上一轮 S5 写入 SIR 的待收集清单（每项含 key/label/prompt_hint）。
        base_slots: 当前 SIR 基态已填槽位（供 LLM 参考、避免重复填充）。
    Returns:
        仅含「成功归一化」的 slot→value 字典（只含待收集 key，不发明无关槽）。
    """
    if not pending_defs:
        return {}
    pend_lines = "\n".join(
        f"- {p.get('key')}：{p.get('label')}（{p.get('prompt_hint') or ''}）"
        for p in pending_defs if isinstance(p, dict) and p.get("key")
    )
    if not pend_lines:
        return {}
    user_prompt = (
        f"待收集槽位：\n{pend_lines}\n\n"
        f"当前已填信息：{json.dumps(base_slots, ensure_ascii=False)}\n\n"
        f"用户原话：{message}\n\n请填充上述待收集槽位，只输出 JSON。"
    )
    try:
        text = await chat_completion(
            [{"role": "system", "content": _FILL_SYSTEM},
             {"role": "user", "content": user_prompt}],
            temperature=0.1, max_tokens=512, timeout=30.0, purpose="fill",
        )
        data = _extract_json(text)
        if not isinstance(data, dict):
            return {}
        out: dict[str, object] = {}
        for k, v in data.items():
            if not isinstance(k, str) or not k.startswith("site."):
                continue
            norm = _coerce_fill_value(k, v)
            if norm is not None:
                out[k] = norm
        return out
    except Exception as exc:  # LLMError / 解析失败 → 安全降级为空（不阻断主链路）
        logger.warning("[intent] 续答抽槽 LLM 失败(降级为空): %s", exc)
        return {}


def understand(message: str) -> UnderstandingResult:
    """确定性优先的多意图理解（方案①+②）。

    返回 UnderstandingResult：
      - resolved_intents: 1..N 个已解析意图（CHAT 兜底必含）；
      - utterance_frame: 兼容旧字段，取 primary（第一个可执行或第一个命中）意图；
      - social_prefix: 社交寒暄前缀（若有）。
    不在此调用 LLM（保持零额外 LLM 成本与确定性）；升级交给 escalate_if_needed。
    """
    logger.info("[intent] understand 入口 msg=%.80s", message)
    social, rest = _split_social(message)
    segments = _segment(rest) if rest else []

    resolved: list[IntentItem] = []
    for idx, seg in enumerate(segments, start=1):
        info = _classify_segment(seg)
        if info is not None:
            resolved.append(_build_item(idx, seg, info))
        else:
            # 该段无明确域 → CHAT 兜底桶（additive，不被丢弃）
            resolved.append(IntentItem(
                id=f"i{idx}",
                domain=Domain.CHAT,
                speech_act=SpeechAct.ASK,
                intent_id="chat_ask",
                target=TargetRef(type=TargetType.NONE),
                arguments={"message": seg},
                confidence=0.6,
                executable=False,
                risk_hint=RiskLevel.LOW,
                method=IntentMethod.RULE,
                raw_segment=seg[:2048],
                skill=None,
            ))

    # 防御：没有任何段（纯空格）也至少补一个 CHAT，避免下游无意图分支崩溃。
    if not resolved:
        resolved.append(IntentItem(
            id="i1", domain=Domain.CHAT, speech_act=SpeechAct.ASK, intent_id="chat_ask",
            arguments={"message": message}, confidence=0.6, executable=False,
            raw_segment=message[:2048],
        ))

    # primary：优先取第一个可执行意图，否则第一个。
    primary = next((r for r in resolved if r.executable), resolved[0])

    frame = UtteranceFrame(
        domain_hint=primary.domain,
        speech_act=primary.speech_act,
        target=primary.target,
        executable=primary.executable,
        social_prefix=social,
        confidence=primary.confidence,
    )
    candidates = [
        IntentCandidate(
            intent_id=r.intent_id, confidence=r.confidence, method=r.method,
            raw_segment=r.raw_segment, signals=[],
        )
        for r in resolved
    ]
    logger.info(
        "[intent] understand 解析完成: segments=%d resolved=%d primary=%s confidence=%.2f",
        len(segments), len(resolved), primary.intent_id, float(primary.confidence or 0.0),
    )
    slot_stack = _build_slot_stack(message, resolved)
    return UnderstandingResult(
        utterance_frame=frame,
        sir_delta=_extract_slots(message, resolved),
        intent_candidates=candidates,
        resolved_intents=resolved,
        social_prefix=social,
        slot_stack=slot_stack,
        needs_clarification=False,
    )


# ----------------------------------------------------------------- 方案③ LLM 升级
# 升级提示词集中于 app/prompts/intent_escalation.py（含 few-shot + 硬约束），
# 通过顶部 `from app.prompts import INTENT_ESCALATION_PROMPT as _ESCALATION_PROMPT` 引用。


async def escalate_if_needed(message: str, current: UnderstandingResult) -> UnderstandingResult:
    """方案③：当规则无法稳妥分解时，单次 LLM 全局推理升级。

    触发条件（避免无谓 LLM 调用，保持确定性优先）：
      - resolved_intents 为空（零命中，纯无法归类），或
      - 多候选且存在低置信（<0.85）且无明确可执行意图，或
      - 句长超过阈值且命中 >2 个意图（复合句歧义）。
    任何失败都安全降级回规则结果（current），绝不抛错中断 pipeline。
    """
    n = len(current.resolved_intents)
    # 优化: 规则已识别出高置信可执行意图 → 直接采用, 不跑 LLM。
    # 避免无谓的 30~42s 延迟(实测长句被连词切出闲聊段致 n>1 误触发升级),
    # 也避免 LLM 把明确意图过度纠正成闲聊(16:53 case)。下游域继承/回溯仍会兜底。
    has_confident_executable = any(r.executable and (r.confidence or 0) >= 0.7 for r in current.resolved_intents)
    if has_confident_executable:
        logger.debug(
            "[intent] 高置信可执行意图已识别, 跳过 LLM 升级: primary=%s",
            next((r.intent_id for r in current.resolved_intents if r.executable), "?"),
        )
        return current
    # 纯闲聊 / 纯疑问句保护：规则层把所有分句都归为 CHAT 兜底(无任何非 CHAT 域意图)时，
    # 直接跳过 LLM 升级——否则「你是谁？干啥的？」被切两段→升级→LLM 返回 2 个 CHAT 意图
    # → ambiguous(len>1)→误判 needs_clarification=True + 降置信到 0.6，把正常闲聊/自我介绍
    # 当成"意图不清需澄清"。规则在此类场景下置信(0.6)与 executable=False 已准确表达"闲聊"，
    # 无需 LLM 介入。仅当存在任一非 CHAT 域信号(可能需纠正/升级)才进入升级路径。
    has_real_domain_signal = any(r.domain != Domain.CHAT for r in current.resolved_intents)
    if not has_real_domain_signal:
        logger.debug(
            "[intent] 纯闲聊(全 CHAT 兜底, 无域信号), 跳过 LLM 升级 n=%d: %s",
            n, next((r.intent_id for r in current.resolved_intents), "?"),
        )
        return current
    # 升级触发条件（按用户要求收窄）：
    #   ① 多候选 且 存在低置信(<0.85) 且 无明确可执行意图 → 规则层不确定，交 LLM 升级；
    #   ② 句长超过阈值 且 命中 >2 个意图（复合句歧义）→ 交 LLM 升级。
    # 注意：上方「纯闲聊全 CHAT」护栏(行380)仍先于本段生效，
    # 否则 ① 会重新把「你是谁？干啥的？」这类纯闲聊复合句送进升级 → 误判 needs_clarification。
    has_low_conf = any((r.confidence or 1.0) < 0.85 for r in current.resolved_intents)
    multi_low = n > 1 and has_low_conf and not has_confident_executable
    long_and_many = len(message) > 24 and n > 2
    if not (multi_low or long_and_many):
        # 规则已能稳妥分解 → 不升级
        logger.debug("[intent] 规则已稳妥分解, 跳过 LLM 升级 (n=%d multi_low=%s long_and_many=%s)", n, multi_low, long_and_many)
        return current

    # 超时自适应：文字长短不一、处理耗时也不同。短文字给 30s，长文字给 120s，
    # 避免一刀切——qwen 偶发抖动时，长句（复合句/多意图）本就需要更多生成时间，
    # 过短的超时只会白白触发 deepseek 兜底重算（既慢又丢规则层已识别好的意图）。
    char_count = len(message)
    escalation_timeout = 30.0 if char_count <= 80 else 120.0
    logger.info("[intent] 触发 LLM 升级: n=%d multi_low=%s long_compound=%s msg_len=%d timeout=%.0fs",
                n, multi_low, long_compound, len(message), escalation_timeout)
    try:
        # RAG 增强：检索 intents 知识库中语义相近的既有意图示例，注入升级提示词，
        # 让 LLM 兜底分类时对齐"系统既有的意图语义"，减少漂移（fail-soft：无结果不注入）。
        few_shot = await _rag_retrieve(
            settings.chroma_collection_intents, message, top_k=3
        )
        rag_ctx = _fmt_hits(few_shot, label="既有意图示例") if few_shot else ""
        augmented = _ESCALATION_PROMPT + (("\n" + rag_ctx + "\n") if rag_ctx else "")
        text = await chat_completion(
            [{"role": "user", "content": augmented + message}],
            temperature=0.2, max_tokens=512, timeout=escalation_timeout,
            purpose="intent",
        )
    except LLMError as exc:
        logger.warning("[intent] LLM 升级调用失败，降级规则结果: %s", exc)
        return current

    try:
        data = _extract_json(text)
        intents = data.get("intents") or []
        if not intents:
            return current
        resolved: list[IntentItem] = []
        # 规则层是否已发现非闲聊域（site/research/project）——用于检测 LLM 是否过度纠正成闲聊。
        rule_had_nonchat = any(r.domain != Domain.CHAT for r in current.resolved_intents)
        for idx, it in enumerate(intents[:_MAX_ACTION_ITEMS], start=1):
            dom = _safe_domain(it.get("domain"))
            sp = _safe_speech(it.get("speech"))
            seg = str(it.get("text") or message)[:2048]
            risk = RiskLevel.CRITICAL if sp in {SpeechAct.PUBLISH, SpeechAct.PURGE} else \
                RiskLevel.HIGH if sp == SpeechAct.TRASH else RiskLevel.LOW
            item = IntentItem(
                id=f"i{idx}", domain=dom, speech_act=sp,
                intent_id=f"{dom.value}_{sp.value}",
                target=TargetRef(type=TargetType.PROJECT if dom in (Domain.SITE, Domain.PROJECT) else TargetType.NONE),
                arguments={"message": seg, "skill": "site" if dom == Domain.SITE else None},
                confidence=0.85, executable=sp != SpeechAct.ASK,
                risk_hint=risk, method=IntentMethod.LLM, raw_segment=seg,
                skill="site" if dom == Domain.SITE else None,
            )
            dep = it.get("depends_on")
            if isinstance(dep, int) and 1 <= dep < idx:
                item.depends_on = [f"i{dep}"]
            resolved.append(item)
        if not resolved:
            return current
        # 置信校准 + 澄清判定（方案③带来的真实置信，不再写死 0.85）：
        #   - LLM 把本有 site/research/project 信号的句子错判成 chat_ask（过度纠正）→ 低置信 + 需澄清；
        #   - 返回多个互相竞争、无明确主意图的意图 → 视为歧义，需澄清。
        # 正常高置信规则流（0.9）从不进入本函数，更不会置 needs_clarification，故不受影响。
        llm_primary = next((r for r in resolved if r.executable), resolved[0])
        over_to_chat = rule_had_nonchat and llm_primary.domain == Domain.CHAT
        ambiguous = len(resolved) > 1
        needs_clarify = bool(over_to_chat or ambiguous)
        if needs_clarify:
            for r in resolved:
                r.confidence = min(r.confidence, 0.6)
        primary = llm_primary
        frame = UtteranceFrame(
            domain_hint=primary.domain, speech_act=primary.speech_act,
            target=primary.target, executable=primary.executable,
            social_prefix=current.social_prefix, confidence=primary.confidence,
        )
        logger.info("[intent] LLM 升级完成: %d 意图 primary=%s needs_clarify=%s",
                    len(resolved), primary.intent_id, needs_clarify)
        return current.model_copy(update={
            "resolved_intents": resolved,
            "utterance_frame": frame,
            # 升级路径同样要重算 sir_delta：LLM 可能把域从 chat 纠正为 site，
            # 若沿用规则期的 delta，站点槽位会被整轮丢掉。
            "sir_delta": _extract_slots(message, resolved),
            "intent_candidates": [IntentCandidate(intent_id=r.intent_id, confidence=r.confidence,
                                                  method=IntentMethod.LLM, raw_segment=r.raw_segment)
                                  for r in resolved],
            "escalated": True,
            "needs_clarification": needs_clarify,
            # 回填 LLM 升级原文，与 [S2] 日志整合（便于回放为什么升级 / 为什么澄清）。
            "escalation_llm_response": text,
        })
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("[intent] LLM 返回无法解析，降级规则结果: %s", exc)
        return current


async def record_intent_example(message: str, intent_id: str) -> None:
    """把一条已确认的意图示例后台沉淀进 ``intents`` 集合（随生产进行补充知识库）。

    仅沉淀可执行意图（site/research/project 等非闲聊）；纯 CHAT 不写入，
    避免污染意图语义索引。幂等：同内容 hash 为 id，重复消息只更新不新增。
    """
    if not message or not message.strip():
        return
    if not intent_id or intent_id.startswith("chat_"):
        return
    await _rag_upsert_bg(
        settings.chroma_collection_intents,
        [message],
        metadatas=[{"kind": "example", "intent_id": intent_id, "source": "auto"}],
        id_prefix="ex",
    )


def _extract_json(text: str) -> dict:
    """从 LLM 文本中尽力抽取首个 JSON 对象。"""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no json object found")
    return json.loads(text[start:end + 1])


def _safe_domain(v: object) -> Domain:
    try:
        return Domain(str(v).lower())
    except ValueError:
        return Domain.CHAT


def _safe_speech(v: object) -> SpeechAct:
    try:
        return SpeechAct(str(v).lower())
    except ValueError:
        return SpeechAct.ASK


def inherit_retro_domain(
    understanding: UnderstandingResult, prior_domain: Domain | None
) -> UnderstandingResult:
    """回溯控制的**域继承**：把 CHAT 兜底意图提升为上一轮的域。

    为什么必须有这一步：`correct`/`supplement` 的语义天然是"改上一轮的产物"，
    而用户的修改指令往往不含域触发词——「改成浅色风格，并加一个联系我的板块」
    一个 SITE_WORDS 都不命中，规则层只能落 CHAT 兜底，结果回溯改站被降级成闲聊追问
    （实测复现：artifact_refs=[] ，AI 反过来问用户"您要做什么类型的网站"）。

    继承规则（保守，不误伤）：
      - 仅当上一轮域为可执行域（SITE/PROJECT/RESEARCH）时才继承；
      - 仅提升 **CHAT 兜底且不可执行** 的意图，已被规则明确识别为其它域的意图不动
        （用户在回溯里说"顺便帮我查下资料"仍走 research，不会被强行吞成改站）；
      - 提升后 speech_act 一律为 EDIT（回溯即修改），并绑定 skill，使 S6 走
        site_service.create_or_edit → 同 project version+1 的受控 edit。
    """
    if prior_domain is None or prior_domain in (Domain.CHAT,):
        return understanding
    resolved = list(understanding.resolved_intents)
    if not resolved:
        return understanding
    promoted: list[IntentItem] = []
    changed = False
    for it in resolved:
        if it.domain == Domain.CHAT and not it.executable:
            speech = SpeechAct.EDIT if prior_domain == Domain.SITE else it.speech_act
            skill = "site" if prior_domain == Domain.SITE else it.skill
            promoted.append(it.model_copy(update={
                "domain": prior_domain,
                "speech_act": speech,
                "intent_id": f"{prior_domain.value}_{speech.value}",
                "target": TargetRef(type=TargetType.PROJECT),
                "arguments": {**(it.arguments or {}), "skill": skill},
                "executable": True,
                "skill": skill,
                "confidence": 0.8,  # 继承而非直接识别，置信略低于规则命中(0.9)
            }))
            changed = True
        else:
            promoted.append(it)
    if not changed:
        return understanding
    logger.info("[intent] 回溯域继承: CHAT 兜底 -> %s (%d 个意图)", prior_domain.value, len(promoted))
    primary = next((r for r in promoted if r.executable), promoted[0])
    frame = UtteranceFrame(
        domain_hint=primary.domain, speech_act=primary.speech_act, target=primary.target,
        executable=primary.executable, social_prefix=understanding.social_prefix,
        confidence=primary.confidence,
    )
    return understanding.model_copy(update={"resolved_intents": promoted, "utterance_frame": frame})


def classify(message: str, understanding: UnderstandingResult, prior_turn_id: str | None = None) -> tuple[IntentBundle, BoundedPlan]:
    """S4：把已解析意图直接映射为 IntentBundle + BoundedPlan（运行期上限跟随 settings.max_action_items，硬护栏为 contracts.MAX_ACTION_ITEMS）。

    依赖推断：后继意图若显式 depends_on 则保留其依赖边；否则默认串行（BoundedPlan.serial）。
    has_gated / max_risk 用于 S5 闸门与 S9 收口。
    prior_turn_id：回溯控制(correct/supplement)时非空，会把 site 域 action 绑定上一轮 turn，
    使 S6 锁定原 project 做受控 edit（而非另起新站）。
    """
    items = understanding.resolved_intents[:_MAX_ACTION_ITEMS]
    bundle_items: list[IntentItem] = []
    actions: list[ActionItem] = []
    max_risk = RiskLevel.LOW
    has_gated = False
    for it in items:
        # 回溯 turn：site 域动作继承 prior_turn_id 绑定（Chat/其它域不强绑）。
        it_prior = prior_turn_id if (prior_turn_id and it.domain == Domain.SITE) else it.prior_turn_id
        if it_prior is not None:
            it = it.model_copy(update={"prior_turn_id": it_prior})
        bundle_items.append(it)
        # 关键：CHAT(ask) 虽 executable=False，也必须成为 action 被 S6 执行，
        # 否则「闲聊 + 建站」复合句里的闲聊会被静默丢弃（即旧版过度裁剪的根因）。
        is_action = it.executable or it.domain == Domain.CHAT
        if is_action and len(actions) < _MAX_ACTION_ITEMS:
            actions.append(ActionItem(
                id=it.id, intent_id=it.intent_id, domain=it.domain,
                speech_act=it.speech_act, target=it.target, arguments=it.arguments,
                depends_on=list(it.depends_on), dependency_kind="hard",
                prior_turn_id=it_prior,
            ))
        if it.risk_hint in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            max_risk = RiskLevel.CRITICAL if it.risk_hint == RiskLevel.CRITICAL else RiskLevel.HIGH
        # has_gated 判定收敛到 governance（这是同语义的第 4 份硬编码拷贝，现已消除）。
        # 与 S5 共用 action_requires_approval，S4 计划标注与 S5 实际闸门永远一致。
        if action_requires_approval(it.speech_act.value):
            has_gated = True

    # primary 取第一个可执行，否则第一个
    primary = next((b.id for b in bundle_items if b.executable), (bundle_items[0].id if bundle_items else None))
    bundle = IntentBundle(
        primary_id=primary,
        social_prefix=understanding.social_prefix,
        items=bundle_items,
        needs_clarification=understanding.needs_clarification,
    )
    plan = BoundedPlan(
        action_items=actions,
        max_risk=max_risk,
        has_gated=has_gated,
    )
    return bundle, plan
