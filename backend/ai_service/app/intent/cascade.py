"""混合级联意图识别核心(分类 v1.2.0, classify_v3)。

四步链路(见文档《意图识别-链路流程对比-规则优先级联vsSIR》混合级联方案):
  ① 规则强信号直路由(match_rules) —— 命中强信号进入候选。
  ② 向量召回 top5(retrieve_intents, Chroma 优先, 离线 bigram 兜底)。
  ③ 向量 super-fast 直通: 强规则命中 且 向量 top1 相似度≥0.9 且对齐 → 跳过 LLM, 直接路由。
  ④ LLM 有界终判(仅从 top5 候选里选 + 任务态 + 工具列表 + 已收集槽位 → 结构化 JSON)。
  ⑤ 置信门控: 高→route; 低/缺关键参数→clarify(≤2 轮); 新奇度兜底(top5 全<0.45→chat)。
  ⑥ 多意图拆分: route 且 build 类且轻量门控命中 → maybe_split。

相对 SIR(v1.1.0)的优化:
  - 删掉易出 bug 的 update_belief 粘性算术, 多轮一致性靠「显式上下文 + 持久化槽位(store.py)」。
  - 分类与拆分共用 intent_catalog.json(单一来源), 根治 SKILL_WHITELIST 两处维护的 R3 风险。
  - 产出统一 PipelineResult 契约(intent/models.py), router/queue/worker 零改动。

外部依赖: knowledge.chroma(向量) / analytics._get_redis(槽位) / providers(LLM) 均已就绪可复用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from ..core.models import SubTask
from ..providers import get_chat_model, resolve_fallback_order
from ..registry import SkillRegistry
from ..analytics import record_intent_classify, record_safety, record_llm_call
from ..config import settings
from .catalog import catalog_for_llm, get_intent, skill_whitelist
from .common import (
    normalize_industry,
    _GENERIC_Q,
    _MISSING_Q,
    _has_conversation_requirement,
    _last_user_message,
)
from .observation import record as observe_record
from .models import PipelineResult
from .rulesmatcher import match_rules
from .safety import SafetyResult, run_safety
from .selection import clear_pending_options, resolve_selection
from .signals import is_delete_signal, is_reset_signal
from .splitter import _lightweight_multi_check, maybe_split
from .store import load_slots, reset_slots, save_slots
from .tools import SkillCandidate, ToolResult, run_tools
from .vector_store import retrieve_intents

logger = logging.getLogger("ai_service.intent.cascade")

# ── 阈值(单一来源: config.settings.intent_*) ──
SUPER_FAST = settings.intent_super_fast
NOVELTY = settings.intent_novelty
COMMIT = settings.intent_commit
CLARIFY_LO = settings.intent_clarify_lo
CLARIFY_MAX_ROUNDS = settings.intent_clarify_max_rounds

# 兜底闲聊意图
_CHAT_CASUAL = "chat_casual"


@dataclass
class RulingResult:
    intent_id: str = _CHAT_CASUAL
    confidence: float = 0.4
    industry: str = "other"
    missing_slots: list = field(default_factory=list)
    collected_slots: dict = field(default_factory=dict)
    questions: list = field(default_factory=list)
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
    '"questions":["自然语言追问(若缺槽位, 最多2条)"],"reason":"简短裁决理由"}\n\n'
    "industry(13选1): restaurant|ecommerce|gov|edu|health|finance|game|personal|corp|tech|media|travel|other\n"
    "confidence 准则: 明确匹配且信息充足 ≥0.8; 有匹配但缺关键信息 0.5~0.8; 模糊或像闲聊 <0.5。\n"
    "如果候选置信都低且不像任何建站/咨询意图 → 选 chat_casual, confidence 给 0.3~0.5。\n"
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
            return RulingResult(
                intent_id=iid, confidence=conf, industry=industry,
                missing_slots=missing, collected_slots=collected_slots,
                questions=questions, reason=str(data.get("reason", "")),
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
        request_id=request_id,
        sub_tasks=sub_tasks or [],
        split_reason=split_reason,
    )


async def classify_v3(
    messages: list[dict],
    model_id: str = "deepseek",
    *,
    conversation_id: int | None = None,
    context_hint: str = "",
    project_status: str = "draft",
    project_constraints: list[str] | None = None,
    checkpoint_info: dict | None = None,
    user_id: int | None = None,
    project_id: int | None = None,
    has_requirement_doc: bool = False,
    site_generated: bool = False,
) -> PipelineResult:
    """混合级联意图识别 v1.2.0(统一契约见 intent/models.PipelineResult)。"""
    t0 = time.time()
    req_id = uuid.uuid4().hex
    current_user_msg = _last_user_message(messages)
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
        reset_slots(conversation_id)
        await record_intent_classify("route", "selection", (time.time() - t0) * 1000)
        return _emit_route(
            {"level1": "chat", "level2": "casual"}, 1.0, decision="route",
            selected_skill=_chosen, industry="other", reason="用户选择/指定",
            evidence={"selection": {"chosen": _chosen, "candidates": _cands}},
            request_id=req_id,
        )

    # ── [+0] 删除操作 → 路由到 agent_delete skill ──
    #   skill 内部处理: 分析删除目标 → 确认弹窗 → 执行 → 反馈结果。
    #   关键词来源: intent/signals.py(load_control_signals, 单一来源)。
    if is_delete_signal(current_user_msg):
        logger.info("[级联][%s] 删除操作 → 路由 agent_delete %s", req_id, current_user_msg[:40])
        reset_slots(conversation_id)
        await record_intent_classify("route", "delete", (time.time() - t0) * 1000)
        return _emit_route(
            get_intent("manage_delete") or {
                "level1": "manage", "level2": "delete", "skill": "agent_delete", "id": "manage_delete",
            },
            0.98, decision="route",
            selected_skill="agent_delete", industry="other",
            reason="delete", request_id=req_id,
            evidence={"delete_op": True},
        )

    # ── RESET: 用户显式退出建站/澄清(关键词来源: intent/signals.py) ──
    if is_reset_signal(current_user_msg):
        logger.info("[级联][%s] RESET 信号 → 闲聊", req_id)
        reset_slots(conversation_id)
        await record_intent_classify("route", "reset", (time.time() - t0) * 1000)
        return _emit_route(
            get_intent(_CHAT_CASUAL) or {"level1": "chat", "level2": "casual"}, 0.3,
            decision="route", selected_skill="agent_chat", industry="other",
            reason="reset", request_id=req_id,
            evidence={"reset": True},
        )

    # ── [1] 规则强信号 ──
    rule_hits = match_rules(current_user_msg)
    strong_rule = next((h for h in rule_hits if h.strength == "strong"), None)

    # ── [2] 向量召回 top5 ──
    # 🔧 P0 修复: retrieve_intents 内部含 Chroma 同步阻塞查询(col.query),
    #   若直接在 async 事件循环内调用会冻结整个 loop, 导致外层
    #   asyncio.wait_for(timeout=35.0) 永远无法触发(现象: 首条消息永久卡在[3/6])。
    #   改用 asyncio.to_thread 把阻塞 I/O 隔离到线程池, 事件循环保持响应,
    #   超时保护与并发请求均恢复正常。
    candidates = await asyncio.to_thread(retrieve_intents, current_user_msg, 5)
    top_score = candidates[0]["score"] if candidates else 0.0
    top_intent_id = candidates[0]["intent_id"] if candidates else ""

    # ── [3] 向量 super-fast 直通: 强规则 + top1 高相似且对齐 → 跳过 LLM ──
    if strong_rule and candidates and top_intent_id == strong_rule.intent_id and top_score >= SUPER_FAST:
        intent = candidates[0]["intent"]
        logger.info("[级联] super-fast 直通: 规则=%s 向量top1=%.2f → 跳过LLM", strong_rule.rule_id, top_score)
        save_slots(conversation_id, {
            "intent_id": intent["id"], "slots": {}, "clarify_rounds": 0,
            "confidence": strong_rule.confidence,
        })
        observe_record(request_id=req_id, conversation_id=conversation_id, user_id=user_id,
                       raw_input=current_user_msg, llm_intent=f"{intent['level1']}/{intent['level2']}",
                       llm_confidence=strong_rule.confidence, rules_triggered=[strong_rule.rule_id],
                       belief_before=0.0, belief_after=strong_rule.confidence, decision="route",
                       latency_ms=(time.time() - t0) * 1000, tokens_used=0,
                       specialist_routed=intent["skill"], outcome="pending",
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

    # ── 新奇度兜底: 无规则命中 且 向量全低 → 闲聊 ──
    if not rule_hits and top_score < NOVELTY:
        logger.info("[级联] 新奇度兜底: top_score=%.2f < %.2f → 闲聊", top_score, NOVELTY)
        chat_intent = get_intent(_CHAT_CASUAL) or {"level1": "chat", "level2": "casual", "skill": "agent_chat"}
        save_slots(conversation_id, {
            "intent_id": _CHAT_CASUAL, "slots": {}, "clarify_rounds": 0, "confidence": top_score,
        })
        observe_record(request_id=req_id, conversation_id=conversation_id, user_id=user_id,
                       raw_input=current_user_msg, llm_intent="chat/casual",
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

    # ── 安全优先(critical 拦截, 可短路) ──
    safety_result = SafetyResult()
    try:
        safety_result = run_safety(messages, project_constraints=project_constraints)
    except Exception as e:
        logger.warning("[级联] run_safety 异常: %s", e)
    if safety_result.risk_level == "critical":
        logger.warning("[级联][%s] 安全检查→拦截 reason=%s risk=%s", req_id, safety_result.block_reason, safety_result.risk_level)
        reset_slots(conversation_id)
        observe_record(request_id=req_id, conversation_id=conversation_id, user_id=user_id,
                       raw_input=current_user_msg, llm_intent="chat/casual",
                       llm_confidence=0.0, rules_triggered=["critical_safety"],
                       belief_before=0.0, belief_after=0.0, decision="block",
                       latency_ms=(time.time() - t0) * 1000, tokens_used=0,
                       specialist_routed=None, outcome="blocked")
        # 安全网关统计: 记录本次拦截(风险等级 + 原因)
        await record_safety(safety_result.risk_level, blocked=True, reason=safety_result.block_reason or "critical")
        await record_intent_classify("block", "block", (time.time() - t0) * 1000, confidence=0.0)
        return _emit_route(
            {"level1": "chat", "level2": "casual"}, 0.0, decision="block",
            selected_skill="agent_chat", industry="other",
            reason=safety_result.block_reason, safety=safety_result, request_id=req_id,
            evidence={"safety": {"risk_level": "critical", "tags": safety_result.risk_tags}},
        )

    # ── [4] LLM 有界终判 ──
    prior = load_slots(conversation_id)
    prior_intent_id = prior.get("intent_id", "") or ""
    prior_collected = prior.get("slots", {}) or {}
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

    # ── [5] 置信门控 ──
    clarify_rounds = int(prior.get("clarify_rounds", 0) or 0)
    new_rounds = clarify_rounds

    # 🔧 建站意图 + 已具备需求(需求文档或对话上下文)→ 直接提交生成, 不再追问。
    #   否则 build/site / build/page / build/modify / build/fix 等中置信意图(0.6~0.7)会
    #   落入 clarify 分支, 导致「开始生成网站吧」及所有建站迭代永不真正触发生成(建站全流程断裂)。
    if intent.get("level1") == "build" and (has_requirement_doc or has_conv_req):
        logger.info("[级联] 门控: 建站意图且需求已具备 → 强制提交生成(跳过追问) intent=%s/%s",
                    intent.get("level1"), intent.get("level2"))
        decision = "route"
    elif conf >= COMMIT:
        decision = "route"
    elif intent.get("level1") == "chat":
        # 闲聊类: 即使低置信也不追问, 直接闲聊(避免对"你好"反复质问)
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

    # 工具映射(尊重 doc-gating: 无需求文档且对话无需求 → 改道 requirement)
    tools = run_tools(
        intent["level1"], intent["level2"], conf, industry=industry,
        project_status=project_status, has_requirement_doc=has_requirement_doc,
        has_conversation_requirement=has_conv_req,
    )
    selected_skill = tools.skills[0].name if tools.skills else intent["skill"]

    questions: list[str] = []
    if decision == "clarify":
        questions = _build_questions(still_missing, ruling.questions)

    # ── 持久化槽位 ──
    save_slots(conversation_id, {
        "intent_id": intent["id"],
        "slots": merged,
        "clarify_rounds": new_rounds if decision == "clarify" else 0,
        "confidence": conf,
    })

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

    # ── [6] 多意图拆分(build 类且轻量门控命中) ──
    sub_tasks: list = []
    split_reason = ""
    if decision == "route" and intent.get("level1") == "build" and _lightweight_multi_check(messages):
        try:
            split = await maybe_split(messages, model_id, base_industry=industry)
            if split.is_multi and split.sub_tasks:
                decision = "split"
                sub_tasks = split.sub_tasks
                split_reason = split.split_reason
                logger.info("[级联] 多意图拆分 decision=split tasks=%d", len(split.sub_tasks))
        except Exception as e:
            logger.warning("[级联] 多意图拆分异常, 降级单意图: %s", e)

    observe_record(request_id=req_id, conversation_id=conversation_id, user_id=user_id,
                   raw_input=current_user_msg, llm_intent=f"{intent['level1']}/{intent['level2']}",
                   llm_confidence=conf, rules_triggered=[h.rule_id for h in rule_hits],
                   belief_before=prior.get("confidence", 0.0), belief_after=conf,
                   decision=decision, latency_ms=(time.time() - t0) * 1000, tokens_used=0,
                   specialist_routed=selected_skill if decision in ("route", "clarify", "split") else None,
                   outcome="pending",
                   extra={"source": evidence["source"], "missing": still_missing})

    result = _emit_route(
        intent, conf, decision=decision, selected_skill=selected_skill, industry=industry,
        questions=questions, rounds=new_rounds, reason=ruling.reason, evidence=evidence,
        safety=safety_result, sub_tasks=sub_tasks, split_reason=split_reason, request_id=req_id,
    )
    # 收尾摘要日志: 一条即可复盘整次分类(决策/来源/意图/置信/耗时/子任务数)
    dur_ms = (time.time() - t0) * 1000
    logger.info("[级联][%s] 完成 decision=%s source=%s intent=%s/%s conf=%.2f 耗时=%.1fms sub_tasks=%d",
                req_id, result.decision, evidence["source"], intent["level1"], intent["level2"],
                conf, dur_ms, len(sub_tasks))
    await record_intent_classify(result.decision, evidence["source"], dur_ms, confidence=conf)
    return result
