"""混合级联意图识别核心(classify_v3, v1.2.5)。

单意图分类: _classify_segment(步骤 ①~⑪)
  ① 选项选择短路(resolve_selection)
  ② 删除信号短路(→ agent_delete)
  ③ RESET 信号短路(→ 闲聊)
  ④ 规则强信号(match_rules)
  ⑤ 向量召回 top5(retrieve_intents, asyncio.to_thread)
  ⑥ 向量 super-fast 直通(强规则 + top1 对齐 + ≥0.9 → 跳过 LLM)
  ⑦ 新奇度兜底(全低 → 闲聊)
  ⑧ 安全拦截(critical → block)
  ⑨ LLM 有界终判(_llm_rule, 仅从候选选)
  ⑩ 置信门控(route/clarify)
  ⑪ 槽位累积 + 工具映射(run_tools) + 持久化

多意图拆分: classify_v3 顶层
  单意图分类完成后, 若 decision==route, 调用 multi_intent.recognize_intents
  做 A+B 路由(方案B 混合分层优先, 必要时升级方案A LLM 深拆)。

设计要点(vs 早期版本):
  - _classify_segment 不含多意图步骤, 供 multi_intent.split_hybrid 逐段复用,
    彻底移除 classify_v3(skip_split=True) 的递归规避 hack。
  - 阻塞 I/O(save_slots/load_slots/run_safety/observe_record) 统一 asyncio.to_thread 隔离,
    避免事件循环冻结。
  - 技能映射唯一来源 = intent_catalog.json(catalog.skill_for), 移除硬编码 INTENT_SKILL_MAP。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field, replace

from ..providers import get_chat_model, resolve_fallback_order
from ..registry import SkillRegistry
from ..analytics import record_intent_classify, record_safety, record_llm_call
from ..config import settings
from .catalog import catalog_for_llm, get_intent, skill_whitelist
from .common import (
    normalize_industry,
    _GENERIC_Q,
    _MISSING_Q,
    _BUILD_TRIGGER,
    _has_conversation_requirement,
    last_user_message,
)
from .observation import record as observe_record
from .models import PipelineResult
from .rulesmatcher import match_rules
from .safety import SafetyResult, run_safety
from .selection import clear_pending_options, resolve_selection
from .signals import is_delete_signal, is_reset_signal
from .store import load_slots, reset_slots, save_slots
from .tools import SkillCandidate, ToolResult, run_tools
from .vector_store import retrieve_intents
from .multi_intent import recognize_intents

logger = logging.getLogger("ai_service.intent.cascade")

# ── 阈值(单一来源: config.settings.intent_*) ──
SUPER_FAST = settings.intent_super_fast
NOVELTY = settings.intent_novelty
COMMIT = settings.intent_commit
CLARIFY_LO = settings.intent_clarify_lo
CLARIFY_MAX_ROUNDS = settings.intent_clarify_max_rounds

# 兜底闲聊意图(目录中存在 chat_casual 条目)
_CHAT_CASUAL = "chat_casual"


@dataclass
class RulingResult:
    intent_id: str = _CHAT_CASUAL
    confidence: float = 0.4
    industry: str = "other"
    missing_slots: list = field(default_factory=list)
    collected_slots: dict = field(default_factory=dict)
    questions: list = field(default_factory=list)
    # 结构化澄清选项(前端浮动卡片用): 最多 3 个候选, 每个 {label, recommended}
    options: list = field(default_factory=list)
    multi: bool = False                 # 选项是否多选
    free_text_hint: str = ""            # 自由输入框提示语
    reason: str = ""


RULE_SYSTEM = (
    "你是智能建站助手小胡的『意图终判器』。下面给出用户最新输入, 以及向量召回的候选意图"
    "(按相似度排序)。你必须且仅能从候选意图里选一个最贴切的(不要自创意图)。\n\n"
    "综合判断依据:\n"
    "1) 用户最新输入语义; 2) 已提供的上下文与任务态; 3) 已收集槽位(不要对已知信息再追问);\n"
    "4) 业务规则(如建完整站前需先有需求文档); 5) 可用技能列表。\n\n"
    "输出严格 JSON(不要多余文字):\n"
    '{"intent_id":"...","confidence":0.0~1.0,"industry":"...",'
    '"missing_slots":["slot_key",...],"collected_slots":{"slot_key":"value",...},'
    '"questions":["自然语言追问(若缺槽位, 最多2条)"],'
    '"options":[{"label":"候选A","recommended":false}],"multi":false,'
    '"free_text_hint":"可补充其他要求","reason":"简短裁决理由"}\n\n'
    "industry(14选1 或 other/none): restaurant|ecommerce|gov|edu|health|finance|game|"
    "personal|corp|tech|media|travel|other|none\n"
    "confidence 准则: 明确匹配且信息充足 ≥0.8; 有匹配但缺关键信息 0.5~0.8; 模糊或像闲聊 <0.5。\n"
    "如果候选置信都低且不像任何建站/咨询意图 → 选 chat_casual, confidence 给 0.3~0.5。\n"
    "若用户意图模糊或缺少关键规格(如风格/行业/页面类型/技术栈), 你可在 options 中给出 "
    "2-3 个具体候选选项(每个含 label 与 recommended 标记, 仅一个 recommended:true 表示系统推荐), "
    "multi 表示是否允许多选(默认 false 单选)。free_text_hint 是可选的开放输入框提示语。\n"
    "若无需选项则省略 options/multi/free_text_hint(仅用 questions 自然语言追问)。\n"
)


def _build_user_prompt(
    text: str,
    candidates: list[dict],
    *,
    context_hint: str,
    project_status: str,
    has_requirement_doc: bool,
    prior_intent_id: str,
    collected: dict,
) -> str:
    cand_text = catalog_for_llm([(c["intent_id"], c["score"]) for c in candidates])
    req_doc = "是" if has_requirement_doc else "否"
    prior = prior_intent_id or "无"
    collected_txt = json.dumps(collected, ensure_ascii=False) if collected else "无"
    return (
        f"候选意图(向量召回, 按相似度):\n{cand_text}\n\n"
        f"可用技能: {skill_whitelist()}\n\n"
        f"任务态: 项目状态={project_status}, 是否已有需求文档={req_doc}, "
        f"断点上下文={context_hint or '无'}, 上一轮意图={prior}\n"
        f"已收集槽位: {collected_txt}\n\n"
        f"用户最新输入: {text[:500]}"
    )


async def _llm_rule(
    text: str,
    candidates: list[dict],
    *,
    model_id: str,
    context_hint: str,
    project_status: str,
    has_requirement_doc: bool,
    prior_intent_id: str,
    collected: dict,
) -> RulingResult:
    """LLM 有界终判: 仅从候选里选 + 任务态 + 工具列表 + 已收集槽位 → 结构化。"""
    if not candidates and not text.strip():
        return RulingResult()
    order = resolve_fallback_order(model_id)
    last_e: Exception | None = None
    for mid in order:
        t0m = time.monotonic()  # 单次 LLM 终判耗时(用于 Provider 统计)
        try:
            chat = get_chat_model(mid, streaming=False)
            resp = await chat.ainvoke([
                {"role": "system", "content": RULE_SYSTEM},
                {"role": "user", "content": _build_user_prompt(
                    text, candidates, context_hint=context_hint,
                    project_status=project_status, has_requirement_doc=has_requirement_doc,
                    prior_intent_id=prior_intent_id, collected=collected)},
            ])
            # 提取 Token 用量(OpenAI 兼容协议 response_metadata.usage), 缺省为 0
            usage = getattr(resp, "response_metadata", {}) or {}
            usage = usage.get("usage") or {}
            tin = int(usage.get("prompt_tokens", 0) or 0)
            tout = int(usage.get("completion_tokens", 0) or 0)
            await record_llm_call(
                mid, True, (time.monotonic() - t0m) * 1000,
                tokens_in=tin, tokens_out=tout,
            )
            raw = (resp.content or "").strip()
            logger.info("[级联] LLM终判 model=%s raw=%.200s", mid, raw)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            iid = str(data.get("intent_id", "")).strip()
            # 必须是合法候选(有目录条目); 否则退回 top1 / 闲聊
            if get_intent(iid) is None:
                if candidates:
                    iid = candidates[0]["intent_id"]
                    logger.info("[级联] LLM返回非法 intent_id=%s, 退回 top1=%s", iid, candidates[0]["intent_id"])
                else:
                    iid = _CHAT_CASUAL
            conf = float(data.get("confidence", 0.5))
            conf = max(0.0, min(1.0, conf))
            industry = normalize_industry(data.get("industry"))
            missing = [str(x) for x in (data.get("missing_slots") or []) if x]
            collected_slots = data.get("collected_slots") or {}
            if not isinstance(collected_slots, dict):
                collected_slots = {}
            questions = [str(x) for x in (data.get("questions") or []) if x][:2]
            # 结构化澄清选项(防御式解析: 最多 3 个, 每个需有非空 label)
            raw_opts = data.get("options") or []
            options = []
            if isinstance(raw_opts, list):
                for o in raw_opts[:3]:
                    if isinstance(o, dict) and str(o.get("label", "")).strip():
                        options.append({
                            "label": str(o["label"]).strip(),
                            "recommended": bool(o.get("recommended", False)),
                        })
            multi = bool(data.get("multi", False))
            free_text_hint = str(data.get("free_text_hint", "") or "").strip()
            return RulingResult(
                intent_id=iid, confidence=conf, industry=industry,
                missing_slots=missing, collected_slots=collected_slots,
                questions=questions, options=options, multi=multi,
                free_text_hint=free_text_hint, reason=str(data.get("reason", "")),
            )
        except Exception as e:
            last_e = e
            # Provider 统计: 失败也要记(错误类型归类, 反映模型可用性)
            await record_llm_call(
                mid, False, (time.monotonic() - t0m) * 1000,
                error_type=type(e).__name__,
            )
            logger.warning("[级联] LLM终判模型%s失败: %s", mid, e)
            continue
    # 全部失败 → 降级: 用 top1 或闲聊
    logger.error("[级联] LLM终判全部失败, 降级: %s", last_e)
    if candidates:
        return RulingResult(intent_id=candidates[0]["intent_id"], confidence=max(candidates[0]["score"], 0.5))
    return RulingResult(intent_id=_CHAT_CASUAL, confidence=0.4)


def _build_questions(missing: list[str], ruling_questions: list[str]) -> list[str]:
    """构建最少必要追问(≤2)。优先用 LLM 给的, 否则按槽位模板合成。"""
    if ruling_questions:
        return ruling_questions[:2]
    qs: list[str] = []
    for k in missing:
        q = _MISSING_Q.get(k)
        if q and q not in qs:
            qs.append(q)
        if len(qs) >= 2:
            break
    if not qs:
        qs = _GENERIC_Q[:2]
    return qs


def _emit_route(
    intent: dict,
    confidence: float,
    *,
    decision: str,
    selected_skill: str,
    industry: str,
    questions: list | None = None,
    rounds: int = 0,
    reason: str = "",
    evidence: dict | None = None,
    safety: SafetyResult | None = None,
    sub_tasks: list | None = None,
    split_reason: str = "",
    request_id: str = "",
    clarify_options: list | None = None,
    clarify_multi: bool = False,
    clarify_allow_free_text: bool = True,
    clarify_free_text_hint: str = "",
) -> PipelineResult:
    l1 = intent.get("level1", "chat")
    l2 = intent.get("level2", "casual")
    return PipelineResult(
        intent={"level1": l1, "level2": l2, "confidence": confidence, "industry": industry},
        plan=[{"action": decision, "skill": selected_skill, "confidence": confidence, "reason": reason}],
        risk=safety or SafetyResult(),
        tools=ToolResult(skills=[SkillCandidate(name=selected_skill, confidence=confidence, reason=reason)]),
        evidence=evidence or {},
        decision=decision,
        selected_skill=selected_skill,
        clarify_questions=questions or [],
        clarify_rounds=rounds,
        clarify_options=clarify_options or [],
        clarify_multi=clarify_multi,
        clarify_allow_free_text=clarify_allow_free_text,
        clarify_free_text_hint=clarify_free_text_hint,
        request_id=request_id,
        sub_tasks=sub_tasks or [],
        split_reason=split_reason,
    )


async def _classify_segment(
    messages: list[dict],
    model_id: str = "deepseek",
    *,
    conversation_id: int | None = None,
    context_hint: str = "",
    project_status: str = "draft",
    project_constraints: list[str] | None = None,
    user_id: int | None = None,
    project_id: int | None = None,
    has_requirement_doc: bool = False,
    has_site_artifact: bool = False,
) -> PipelineResult:
    """单意图分类核心(步骤 ①~⑪, 不含多意图拆分)。

    供 classify_v3 顶层调用; 也被 multi_intent.split_hybrid 逐段复用(无递归)。
    阻塞 I/O(save/load_slots, run_safety, observe_record) 统一 asyncio.to_thread 隔离。
    """
    t0 = time.time()
    req_id = uuid.uuid4().hex
    current_user_msg = last_user_message(messages)
    has_conv_req = _has_conversation_requirement(messages)

    logger.info("[级联][%s] [开始] conv=%s %d条消息 model=%s project=%s doc=%s",
                req_id, conversation_id, len(messages), model_id, project_status,
                "有" if has_requirement_doc else "无")

    # ── [0] 选项选择短路(用户在回复待选项 / 显式指定 skill) ──
    _sel = resolve_selection(
        messages, conversation_id,
        skill_exists=lambda n: SkillRegistry.get(n) is not None,
        known_skills=set(SkillRegistry.names()),
    )
    if _sel is not None:
        _chosen, _cands = _sel
        logger.info("[级联] [0] 命中选项选择 → 短路 skill=%s", _chosen)
        clear_pending_options(conversation_id)
        reset_slots(conversation_id, user_id, project_id)
        await record_intent_classify("route", "selection", (time.time() - t0) * 1000)
        return _emit_route(
            {"level1": "chat", "level2": "casual"}, 1.0, decision="route",
            selected_skill=_chosen, industry="other", reason="用户选择/指定",
            evidence={"selection": {"chosen": _chosen, "candidates": _cands}},
            request_id=req_id,
        )

    # ── [+0] 删除操作 → 路由到 agent_delete skill ──
    if is_delete_signal(current_user_msg):
        logger.info("[级联][%s] 删除操作 → 路由 agent_delete %s", req_id, current_user_msg[:40])
        reset_slots(conversation_id, user_id, project_id)
        await record_intent_classify("route", "delete", (time.time() - t0) * 1000)
        return _emit_route(
            get_intent(_CHAT_CASUAL) or {"level1": "chat", "level2": "casual"}, 0.98, decision="route",
            selected_skill="agent_delete", industry="other",
            reason="delete", request_id=req_id,
            evidence={"delete_op": True},
        )

    # ── RESET: 用户显式退出建站/澄清 ──
    if is_reset_signal(current_user_msg):
        logger.info("[级联][%s] RESET 信号 → 闲聊", req_id)
        reset_slots(conversation_id, user_id, project_id)
        await record_intent_classify("route", "reset", (time.time() - t0) * 1000)
        return _emit_route(
            get_intent(_CHAT_CASUAL) or {"level1": "chat", "level2": "casual"}, 0.3,
            decision="route", selected_skill="agent_chat", industry="other",
            reason="reset", request_id=req_id,
            evidence={"reset": True},
        )

    # ── [+1] 上下文闸门 D(#486): 已落地建站产物 → 追问直路由 build_modify(网站迭代)。
    #   用户原话: "html 里按钮点不动/打不开/不工作" 这种对成品的反馈进不了修改流程。
    #   规则: 命中站点修改词 + 非纯闲聊/非 delete/reset(已被上面短路) → 置信 0.9 直路由。
    #   明显纯夸赞/无修改意图 → 排除走普通分类(交给 LLM 或 novelty 兜底)。
    #   拿不准(命中修改词但句子很模糊)→ 不强行路由, 落回下方 LLM 终判。
    if has_site_artifact:
        _mod_kw = ("修改", "改一下", "调整", "优化", "换个", "换掉", "换成", "改成",
                   "加一个", "加个", "去掉", "删掉", "删除", "修复", "修一下", "不能点",
                   "点不动", "点不了", "无法点击", "打不开", "打不开", "不工作", "没反应",
                   "失效", "坏了", "错位", "重叠", "样式", "配色", "颜色", "布局", "导航",
                   "按钮", "链接", "页面", "首页", "排版", "间距", "字体", "动画", "交互",
                   "响应式", "移动端", "手机", "对齐", "标题", "文案", "图片", "图标")
        _praise_kw = ("好看", "漂亮", "喜欢", "不错", "厉害", "棒", "完美", "谢谢", "感谢", "赞", "好用", "真棒")
        _hit_mod = sum(1 for kw in _mod_kw if kw in current_user_msg)
        _hit_praise = sum(1 for kw in _praise_kw if kw in current_user_msg)
        _is_obvious_praise = _hit_praise >= 1 and _hit_mod == 0
        if _hit_mod >= 1 and not _is_obvious_praise:
            _conf = 0.9 if _hit_mod >= 2 else 0.78
            _mod_intent = get_intent("build_modify") or {
                "level1": "build", "level2": "modify", "skill": "agent_build",
                "id": "build_modify",
            }
            logger.info("[级联][%s] 上下文闸门命中: 已落站 + 命中修改词(%d) → 直路由 build_modify conf=%.2f",
                        req_id, _hit_mod, _conf)
            reset_slots(conversation_id, user_id, project_id)
            await asyncio.to_thread(observe_record, request_id=req_id, conversation_id=conversation_id,
                                    user_id=user_id, raw_input=current_user_msg,
                                    llm_intent="build/modify", llm_confidence=_conf,
                                    rules_triggered=["ctx_gate_modify"],
                                    belief_before=0.0, belief_after=_conf, decision="route",
                                    latency_ms=(time.time() - t0) * 1000, tokens_used=0,
                                    specialist_routed=_mod_intent["skill"], outcome="pending",
                                    extra={"source": "ctx_gate_modify"})
            await record_intent_classify("route", "ctx_gate_modify", (time.time() - t0) * 1000, confidence=_conf)
            return _emit_route(
                _mod_intent, _conf, decision="route",
                selected_skill=_mod_intent["skill"], industry="other",
                reason=f"ctx_gate: 已落站产物 + 命中修改词×{_hit_mod}",
                request_id=req_id,
                evidence={"ctx_gate_modify": True, "mod_hits": _hit_mod, "site_artifact": True},
            )
        elif _is_obvious_praise:
            logger.info("[级联][%s] 上下文闸门: 仅纯夸赞(无修改词) → 不强制路由, 落回普通分类", req_id)

    # ── [8.5] PM 粘性(D/#504 修正, 须置于 [1-β] 之前) ──
    # 失败用例: 小白首问"我想做一个网站"→ PM; 答"个人博客"后第二答"风格简约清新"
    # 被 [1-β] 漏判(no verb+noun)且 novelty 兜底误路由到 chat_design, PM 链路断裂。
    # 修复: 若上一轮处于 PM 模式(路由到 agent_requirement)且当前仍无需求文档/未落站
    # → 维持 PM 继续采集, 不被 [1-β]/novelty/chat 抢占; 显式建站触发语(_BUILD_TRIGGER)
    # 仍放行直冲生成器(由下游 [10] 门控放行)。
    _prior = await asyncio.to_thread(load_slots, conversation_id, user_id, project_id)
    _prior_was_pm = (
        (_prior.get("intent_id") or "") == "build_requirement"
        or (_prior.get("selected_skill") or "") == "agent_requirement"
    )
    if _prior_was_pm and not has_requirement_doc and not has_site_artifact:
        _is_explicit_build = any(t in current_user_msg for t in _BUILD_TRIGGER)
        if not _is_explicit_build:
            _pm_intent = get_intent("build_requirement") or {
                "level1": "build", "level2": "requirement", "skill": "agent_requirement",
                "id": "build_requirement",
            }
            logger.info("[级联][%s] PM 粘性: 上一轮 PM 且仍无需求文档 → 维持 agent_requirement (msg=%.30s)",
                        req_id, current_user_msg)
            await asyncio.to_thread(save_slots, conversation_id, {
                "intent_id": "build_requirement",
                "slots": _prior.get("slots", {}) or {},
                "clarify_rounds": int(_prior.get("clarify_rounds", 0) or 0),
                "confidence": 0.8, "selected_skill": "agent_requirement",
            }, user_id, project_id)
            await asyncio.to_thread(observe_record, request_id=req_id, conversation_id=conversation_id,
                                    user_id=user_id, raw_input=current_user_msg,
                                    llm_intent="build/requirement", llm_confidence=0.8,
                                    rules_triggered=["pm_sticky"], belief_before=0.0, belief_after=0.8,
                                    decision="route", latency_ms=(time.time() - t0) * 1000, tokens_used=0,
                                    specialist_routed="agent_requirement", outcome="pending",
                                    extra={"source": "pm_sticky"})
            await record_intent_classify("route", "pm_sticky", (time.time() - t0) * 1000, confidence=0.8)
            return _emit_route(
                _pm_intent, 0.8, decision="route",
                selected_skill="agent_requirement", industry="other",
                reason="PM 粘性: 维持需求采集(无需求文档)", request_id=req_id,
                evidence={"pm_sticky": True, "prior_intent": _prior.get("intent_id")},
            )

    # ── [1-β] 建站意图共现启发式(修复白名单短板: 纯子串匹配无法覆盖
    #    "帮我做一个个人博客网站"/"生成公司官网" 这类泛化表达)。
    #    规则: 命中「建站动词」且(命中「站点名词」或「网站修饰词」)→ 直路由 build_site。
    #    排除干扰: 纯"做个/写个 X" 但 X 不是站点(如"做个冷笑话")不触发;
    #    文档/设计/搜索/评审等技能由各自规则或下方 LLM 终判接手, 此处不抢占。
    if not is_reset_signal(current_user_msg):
        _build_verbs = ("帮我建", "帮我生成", "帮我做", "帮我搭", "帮我开发", "帮我弄",
                        "做一个", "生成一个", "建一个", "搭一个", "开发", "搭建", "生成",
                        "建站", "做网站", "建网站", "搞个网站", "弄个网站", "给我做", "给我建",
                        "我想做", "我想要", "想做一个", "需要做一个", "来一个", "做一个网站")
        # 仅无歧义站点名词触发; "页面/单页/网页" 等留给 build_page / design 各自规则或 LLM。
        # v3 修正(#18 漏召): 合并 multi_intent._SITE_NOUNS 的「平台类站点语义」扩展
        # (在线教育/教育平台/旅游平台/旅游小程序 等), 否则"做一个在线教育平台"这类
        # 不含"网站"字样的建站诉求会漏过直路由、回退到 chat(实测 #18 误路由 agent_chat)。
        try:
            from .multi_intent import _SITE_NOUNS as _MI_SITE_NOUNS
        except Exception:
            _MI_SITE_NOUNS = ()
        _site_nouns = ("网站", "官网", "门户网站", "博客", "blog", "主页", "首页", "落地页",
                       "h5", "小程序官网", "站点", "公司官网", "企业官网", "个人网站",
                       "电商网站", "商城", "平台官网", "官网页面", "web页面") + tuple(_MI_SITE_NOUNS)
        _has_verb = any(v in current_user_msg for v in _build_verbs)
        _has_noun = any(n in current_user_msg for n in _site_nouns)
        if _has_verb and _has_noun:
            _site_intent = get_intent("build_site") or {
                "level1": "build", "level2": "site", "skill": "agent_generate_site", "id": "build_site",
            }
            # ── 建站需求门控(D/#501): 用户显式要建站但「既无需求文档、对话中也读不到可读需求」
            #    → 先转产品经理(agent_requirement)采集规格, 而不是直冲生成器(否则产空壳站 / 卡 await_confirm)。
            #    注意: 此启发式在 run_tools 之前短路返回, 所以必须在这里自己加门控,
            #    不能指望下游 run_tools 的 doc-gate(它只作用于 LLM 终判路径)。
            if not has_requirement_doc and not has_conv_req:
                _sel_skill = "agent_requirement"
                _conf = 0.9
                _reason = "建站共现启发式: 动词+站点名词(无需求文档→先转产品经理采集规格)"
                logger.info("[级联][%s] 建站共现启发式命中 但无需求 → 转 PM conf=%.2f (verb=%s noun=%s)",
                            req_id, _conf, _has_verb, _has_noun)
            else:
                _sel_skill = _site_intent["skill"]
                _conf = 0.92
                _reason = "建站共现启发式: 动词+站点名词"
                logger.info("[级联][%s] 建站共现启发式命中 → 直路由 conf=%.2f skill=%s (verb=%s noun=%s)",
                            req_id, _conf, _sel_skill, _has_verb, _has_noun)
            # 记录 PM 模式(供 PM 粘性 [8.5] 识别续答; 即使走 [1-β] 也持久化,
            # 否则 prior_intent_id 为空会导致续答漏判)。PM 分支不 reset, 保留标记。
            await asyncio.to_thread(save_slots, conversation_id, {
                "intent_id": "build_requirement" if _sel_skill == "agent_requirement" else _site_intent["id"],
                "slots": {}, "clarify_rounds": 0, "confidence": _conf,
                "selected_skill": _sel_skill,
            }, user_id, project_id)
            if _sel_skill != "agent_requirement":
                reset_slots(conversation_id, user_id, project_id)
            await asyncio.to_thread(observe_record, request_id=req_id, conversation_id=conversation_id,
                                    user_id=user_id, raw_input=current_user_msg,
                                    llm_intent="build/site", llm_confidence=_conf,
                                    rules_triggered=["heur_build_site"],
                                    belief_before=0.0, belief_after=_conf, decision="route",
                                    latency_ms=(time.time() - t0) * 1000, tokens_used=0,
                                    specialist_routed=_sel_skill, outcome="pending",
                                    extra={"source": "heur_build_site"})
            await record_intent_classify("route", "heur_build_site", (time.time() - t0) * 1000, confidence=_conf)
            return _emit_route(
                _site_intent, _conf, decision="route",
                selected_skill=_sel_skill, industry="other",
                reason=_reason,
                request_id=req_id,
                evidence={"heur_build_site": True, "verb": _has_verb, "noun": _has_noun,
                          "routed_to_pm": _sel_skill == "agent_requirement"},
            )

    # ── [1] 规则强信号 ──
    rule_hits = match_rules(current_user_msg)
    strong_rule = next((h for h in rule_hits if h.strength == "strong"), None)

    # ── [2] 向量召回 top-k(R2 修复: 原硬编码 5, 现由 settings.intent_top_k 控制) ──
    candidates = await asyncio.to_thread(retrieve_intents, current_user_msg, settings.intent_top_k)
    top_score = candidates[0]["score"] if candidates else 0.0
    top_intent_id = candidates[0]["intent_id"] if candidates else ""
    # 精细日志:打印本次召回的全部向量(意图 + 相似度),便于排查召回质量
    _cand_dump = [(c.get("intent_id"), round(float(c.get("score", 0.0)), 3)) for c in candidates]
    logger.info("[级联][%s] 召回向量 top_k=%d 条: %s", req_id, settings.intent_top_k, _cand_dump)

    # ── [3] 向量 super-fast 直通: 强规则 + top1 高相似且对齐 → 跳过 LLM ──
    if strong_rule and candidates and top_intent_id == strong_rule.intent_id and top_score >= SUPER_FAST:
        intent = candidates[0]["intent"]
        logger.info("[级联] super-fast 直通: 规则=%s 向量top1=%.2f → 跳过LLM", strong_rule.rule_id, top_score)
        await asyncio.to_thread(save_slots, conversation_id, {
            "intent_id": intent["id"], "slots": {}, "clarify_rounds": 0,
            "confidence": strong_rule.confidence,
        }, user_id, project_id)
        await asyncio.to_thread(observe_record, request_id=req_id, conversation_id=conversation_id,
                                user_id=user_id, raw_input=current_user_msg,
                                llm_intent=f"{intent['level1']}/{intent['level2']}",
                                llm_confidence=strong_rule.confidence,
                                rules_triggered=[strong_rule.rule_id],
                                belief_before=0.0, belief_after=strong_rule.confidence,
                                decision="route", latency_ms=(time.time() - t0) * 1000,
                                tokens_used=0, specialist_routed=intent["skill"], outcome="pending",
                                extra={"source": "superfast"})
        await record_intent_classify("route", "superfast", (time.time() - t0) * 1000,
                                      confidence=strong_rule.confidence)
        return _emit_route(
            intent, strong_rule.confidence, decision="route",
            selected_skill=intent["skill"], industry="other",
            reason=f"super-fast: 规则{strong_rule.rule_id}+向量{top_score:.2f}",
            request_id=req_id,
            evidence={"rule": [h.rule_id for h in rule_hits],
                      "vector_top": [(c["intent_id"], round(c["score"], 3)) for c in candidates[:5]],
                      "source": "superfast"},
        )

    # ── [3.5] 强规则直路由(OPTIMIZE_PLAN §2: 即便向量分低, 规则也要能直路由)。
    #    `strong` 规则是人工高精模式(置信 0.95), 命中即直接提交到目标意图,
    #    不依赖向量 top1 对齐 / 不降级到 LLM 终判。修复 10 号"写/文档"此前漏召回。
    if strong_rule:
        intent = get_intent(strong_rule.intent_id)
        if intent:
            logger.info("[级联] 强规则直路由: 规则=%s → intent=%s/%s (跳过向量/LLM 终判)",
                        strong_rule.rule_id, intent["level1"], intent["level2"])
            reset_slots(conversation_id, user_id, project_id)
            await asyncio.to_thread(observe_record, request_id=req_id, conversation_id=conversation_id,
                                    user_id=user_id, raw_input=current_user_msg,
                                    llm_intent=f"{intent['level1']}/{intent['level2']}",
                                    llm_confidence=strong_rule.confidence,
                                    rules_triggered=[strong_rule.rule_id],
                                    belief_before=0.0, belief_after=strong_rule.confidence,
                                    decision="route", latency_ms=(time.time() - t0) * 1000,
                                    tokens_used=0, specialist_routed=intent["skill"], outcome="pending",
                                    extra={"source": "strong_rule"})
            await record_intent_classify("route", "strong_rule", (time.time() - t0) * 1000,
                                          confidence=strong_rule.confidence)
            return _emit_route(
                intent, strong_rule.confidence, decision="route",
                selected_skill=intent["skill"], industry="other",
                reason=f"strong_rule: {strong_rule.rule_id} 命中模式『{strong_rule.pattern}』",
                request_id=req_id,
                evidence={"rule": [h.rule_id for h in rule_hits],
                          "vector_top": [(c["intent_id"], round(c["score"], 3)) for c in candidates[:5]],
                          "source": "strong_rule"},
            )

    # ── [8] 安全优先(critical 拦截, 可短路) ──
    safety_result = SafetyResult()
    try:
        safety_result = await asyncio.to_thread(run_safety, messages, project_constraints=project_constraints)
    except Exception as e:
        logger.warning("[级联] run_safety 异常: %s", e)
    if safety_result.risk_level == "critical":
        logger.warning("[级联][%s] 安全检查→拦截 reason=%s risk=%s", req_id, safety_result.block_reason, safety_result.risk_level)
        reset_slots(conversation_id, user_id, project_id)
        await asyncio.to_thread(observe_record, request_id=req_id, conversation_id=conversation_id,
                                user_id=user_id, raw_input=current_user_msg, llm_intent="chat/casual",
                                llm_confidence=0.0, rules_triggered=["critical_safety"],
                                belief_before=0.0, belief_after=0.0, decision="block",
                                latency_ms=(time.time() - t0) * 1000, tokens_used=0,
                                specialist_routed=None, outcome="blocked")
        await record_safety(safety_result.risk_level, blocked=True, reason=safety_result.block_reason or "critical")
        await record_intent_classify("block", "block", (time.time() - t0) * 1000, confidence=0.0)
        return _emit_route(
            {"level1": "chat", "level2": "casual"}, 0.0, decision="block",
            selected_skill="agent_chat", industry="other",
            reason=safety_result.block_reason, safety=safety_result, request_id=req_id,
            evidence={"safety": {"risk_level": "critical", "tags": safety_result.risk_tags}},
        )

    # ── [9] LLM 有界终判 ──
    prior = await asyncio.to_thread(load_slots, conversation_id, user_id, project_id)
    prior_intent_id = prior.get("intent_id", "") or ""
    prior_collected = prior.get("slots", {}) or {}


    # ── [7→9] 新奇度兜底(R3 修复: 必须放在 load_slots 之后,
    #    否则跨轮 memory/prior slots 尚未加载就被闲聊短路, 丢失上下文):
    #    无规则命中 且 向量全低 → 闲聊 ──
    if not rule_hits and top_score < NOVELTY:
        logger.info("[级联] 新奇度兜底: top_score=%.2f < %.2f → 闲聊", top_score, NOVELTY)
        chat_intent = get_intent(_CHAT_CASUAL) or {"level1": "chat", "level2": "casual", "skill": "agent_chat"}
        await asyncio.to_thread(save_slots, conversation_id, {
            "intent_id": _CHAT_CASUAL, "slots": {}, "clarify_rounds": 0,             "confidence": top_score,
        }, user_id, project_id)
        await asyncio.to_thread(observe_record, request_id=req_id, conversation_id=conversation_id,
                                user_id=user_id, raw_input=current_user_msg, llm_intent="chat/casual",
                                llm_confidence=top_score, rules_triggered=[],
                                belief_before=0.0, belief_after=top_score, decision="route",
                                latency_ms=(time.time() - t0) * 1000, tokens_used=0,
                                specialist_routed="agent_chat", outcome="pending",
                                extra={"source": "novelty"})
        await record_intent_classify("route", "novelty", (time.time() - t0) * 1000,
                                      confidence=top_score)
        return _emit_route(
            chat_intent, max(top_score, 0.3), decision="route",
            selected_skill="agent_chat", industry="other", reason="novelty_fallback",
            request_id=req_id,
            evidence={"vector_top": [(c["intent_id"], round(c["score"], 3)) for c in candidates[:5]],
                      "source": "novelty"},
        )

    ruling = await _llm_rule(
        current_user_msg, candidates, model_id=model_id, context_hint=context_hint,
        project_status=project_status, has_requirement_doc=has_requirement_doc,
        prior_intent_id=prior_intent_id, collected=prior_collected,
    )

    intent = get_intent(ruling.intent_id) or (
        candidates[0]["intent"] if candidates else
        get_intent(_CHAT_CASUAL) or {"level1": "chat", "level2": "casual", "skill": "agent_chat"}
    )
    conf = ruling.confidence
    industry = ruling.industry or "other"

    # 槽位累积(历史 + 本次 LLM 抽取)
    merged = dict(prior_collected)
    merged.update(ruling.collected_slots or {})
    # 仍缺失的槽位(历史+本次都已收集的, 不再追问)
    still_missing = [s for s in ruling.missing_slots if s not in merged]

    # ── [10] 置信门控 ──
    clarify_rounds = int(prior.get("clarify_rounds", 0) or 0)
    new_rounds = clarify_rounds

    # 建站意图 + 已具备需求(需求文档或对话上下文)→ 直接提交生成, 不再追问。
    if intent.get("level1") == "build" and (has_requirement_doc or has_conv_req):
        logger.info("[级联] 门控: 建站意图且需求已具备 → 强制提交生成(跳过追问) intent=%s/%s",
                    intent.get("level1"), intent.get("level2"))
        decision = "route"
    elif conf >= COMMIT:
        decision = "route"
    elif intent.get("level1") == "chat":
        # 闲聊类: 即使低置信也不追问, 直接闲聊
        decision = "route"
    elif clarify_rounds >= CLARIFY_MAX_ROUNDS:
        # 追问耗尽 → 提交最佳猜测
        logger.info("[级联] 澄清轮次耗尽(%d) → 提交 intent=%s", clarify_rounds, intent["id"])
        decision = "route"
    elif still_missing:
        decision = "clarify"
        new_rounds = clarify_rounds + 1
    else:
        # 无缺失槽位但不够自信 → 仍澄清(让模型确认意图)
        decision = "clarify"
        new_rounds = clarify_rounds + 1

    # 工具映射(尊重 doc-gating)
    tools = run_tools(
        intent["level1"], intent["level2"], conf, industry=industry,
        project_status=project_status, has_requirement_doc=has_requirement_doc,
        has_conversation_requirement=has_conv_req,
    )
    selected_skill = tools.skills[0].name if tools.skills else intent["skill"]

    questions: list[str] = []
    clarify_options: list = []
    clarify_multi = False
    clarify_free_text_hint = ""
    if decision == "clarify":
        questions = _build_questions(still_missing, ruling.questions)
        # 结构化选项(若 LLM 提供了候选); 否则前端回退为纯自然语言追问 + 自由输入
        if ruling.options:
            clarify_options = ruling.options
            clarify_multi = ruling.multi
            clarify_free_text_hint = ruling.free_text_hint

    # ── [11] 持久化槽位 ──
    await asyncio.to_thread(save_slots, conversation_id, {
        "intent_id": intent["id"],
        "slots": merged,
        "clarify_rounds": new_rounds if decision == "clarify" else 0,
        "confidence": conf,
    }, user_id, project_id)

    evidence = {
        "rule": [h.rule_id for h in rule_hits],
        "vector_top": [(c["intent_id"], round(c["score"], 3)) for c in candidates[:5]],
        "llm_ruling": {
            "intent_id": ruling.intent_id, "confidence": conf, "industry": industry,
            "missing_slots": still_missing, "collected_slots": merged,
            "questions": questions, "reason": ruling.reason,
        },
        "slots": {"rounds": new_rounds, "prior_intent": prior_intent_id},
        "source": "llm_ruling",
    }
    logger.info("[级联][%s] 门控: conf=%.2f intent=%s/%s decision=%s rounds=%d",
                req_id, conf, intent["level1"], intent["level2"], decision, new_rounds)

    await asyncio.to_thread(observe_record, request_id=req_id, conversation_id=conversation_id,
                            user_id=user_id, raw_input=current_user_msg,
                            llm_intent=f"{intent['level1']}/{intent['level2']}",
                            llm_confidence=conf, rules_triggered=[h.rule_id for h in rule_hits],
                            belief_before=prior.get("confidence", 0.0), belief_after=conf,
                            decision=decision, latency_ms=(time.time() - t0) * 1000, tokens_used=0,
                            specialist_routed=selected_skill if decision in ("route", "clarify") else None,
                            outcome="pending",
                            extra={"source": evidence["source"], "missing": still_missing})

    result = _emit_route(
        intent, conf, decision=decision, selected_skill=selected_skill, industry=industry,
        questions=questions, rounds=new_rounds, reason=ruling.reason, evidence=evidence,
        safety=safety_result, sub_tasks=[], split_reason="", request_id=req_id,
        clarify_options=clarify_options, clarify_multi=clarify_multi,
        clarify_allow_free_text=True, clarify_free_text_hint=clarify_free_text_hint,
    )

    # 收尾摘要日志
    dur_ms = (time.time() - t0) * 1000
    logger.info("[级联][%s] 完成 decision=%s source=%s intent=%s/%s conf=%.2f 耗时=%.1fms sub_tasks=%d",
                req_id, result.decision, evidence["source"], intent["level1"], intent["level2"],
                conf, dur_ms, len(result.sub_tasks))
    await record_intent_classify(result.decision, evidence["source"], dur_ms, confidence=conf)
    return result


async def classify_v3(
    messages: list[dict],
    model_id: str = "deepseek",
    *,
    conversation_id: int | None = None,
    context_hint: str = "",
    project_status: str = "draft",
    project_constraints: list[str] | None = None,
    user_id: int | None = None,
    project_id: int | None = None,
    has_requirement_doc: bool = False,
    has_site_artifact: bool = False,
) -> PipelineResult:
    """混合级联意图识别(v1.2.6)。

    先单意图分类(_classify_segment), 若 decision==route 且轻量门控命中 ≥2 意图大类,
    再做多意图拆分(A+B 路由: recognize_intents)。
    """
    result = await _classify_segment(
        messages, model_id,
        conversation_id=conversation_id, context_hint=context_hint,
        project_status=project_status, project_constraints=project_constraints,
        user_id=user_id, project_id=project_id, has_requirement_doc=has_requirement_doc,
        has_site_artifact=has_site_artifact,
    )

    # ── [6] 多意图拆分(A+B 路由: 方案B 混合分层优先, 必要时升级方案A LLM 深拆) ──
    # 轻量门控在 recognize_intents 内部执行: 未命中多意图则原样返回 is_multi=False。
    if result.decision == "route":
        try:
            split = await recognize_intents(
                messages, model_id, base_industry=result.intent["industry"],
                project_status=project_status, has_requirement_doc=has_requirement_doc,
                project_constraints=project_constraints, conversation_id=conversation_id,
                user_id=user_id, project_id=project_id,
            )
            if split.is_multi and split.sub_tasks:
                result = replace(
                    result, decision="split", sub_tasks=split.sub_tasks,
                    split_reason=f"[{split.source}] {split.split_reason}",
                    selected_skill=split.sub_tasks[0].selected_skill,
                )
                logger.info("[级联] 多意图拆分 decision=split source=%s tasks=%d",
                            split.source, len(split.sub_tasks))
        except Exception as e:  # noqa: BLE001
            logger.warning("[级联] 多意图拆分异常, 降级单意图: %s", e)

    return result
