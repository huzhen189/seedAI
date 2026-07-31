"""SIR_delta 抽取器(DST/SIR 重构 #3) —— LLM 只产出『本轮状态变化量』。

职责(方案 §4):
  - `SIR_SYSTEM`: 对话状态解析器 system prompt(只产 SIR_delta, 不产完整 SIR / 不决策)。
  - `build_sir_user_prompt`: 注入强先验 —— 当前最可能 active_intent、已有 SIR 槽位快照、
    active_intent 的 required_slots —— 让 LLM 专注『状态理解』而非重新分类。
  - `_extract_sir_delta`: 调 LLM, 解析+强校验为 SIRDelta; 失败降级(返回空 delta + 记 warning),
    **绝不**产生脏状态。

与 `_llm_rule` 的关系(方案 §4.3):
  - `_llm_rule` 保留『intent 选择 + 来源信号』(生成 meta.active_intent 先验, 强规则/向量来源)。
  - `_extract_sir_delta` 专注『槽位/约束/意图稳定性』抽取, 二者分工清晰。
  - 本模块仅在 LLM 终判路径 + PM 粘性路径调用; 规则/向量捷径分支直接构造确定性
    SIR_delta(走 dst.build_sir_for_shortcut), 零额外 LLM。
"""

from __future__ import annotations

import json
import logging
import re
import time

from ..analytics import record_llm_call
from ..providers import get_chat_model, resolve_fallback_order
from .catalog import required_slots_of
from .dst import SIRDelta, parse_sir_delta


logger = logging.getLogger("app.agent.intent.sir_prompt")


def _response_text(content: object) -> str:
    """把 LangChain 多形态消息内容稳定转换为纯文本。

    ChatOpenAI 的 content 可能是字符串，也可能是由文本块或字典组成的列表。
    未识别块保守转成字符串，确保 JSON 提取阶段始终得到确定的文本输入。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text_value = item.get("text") or item.get("content")
                if text_value is not None:
                    parts.append(str(text_value))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


SIR_SYSTEM = (
    "你是『对话状态解析器(SIR delta)』。请根据用户最新输入, 输出 SIR_delta —— "
    "**本轮对话相对于上一轮的状态变化量**, 不要输出完整 SIR, 也不要替系统决定『要不要追问』或『调用哪个执行技能』(那是代码的工作)。\n\n"
    "输出字段(JSON, 严格, 不要多余文字):\n"
    "{\n"
    '  "meta": {"active_intent": "最可能的意图id(若与先验不同才写)", "intent_stability": "high|medium|low"},\n'
    '  "slots": { "<槽位名>": {"value": "任意值或 null(取消)", "confidence": 0.0~1.0, "status": "confirmed|dontcare|deleted"} },\n'
    '  "constraints": [ {"type": "exclude|include|limit", "key": "约束键", "value": "约束值或 null(删除该约束)"} ],\n'
    '  "pending": ["待系统确认/可能但未明确的槽位名"]\n'
    "}\n\n"
    "规则:\n"
    "1. 用户**没提**的槽位**不要**输出(CARRYOVER, 沿用旧值); 只输出本轮用户明确新增/修改/取消的槽位。\n"
    "2. 用户取消某槽 → 该槽写 `value:null` 或 `status:\"deleted\"`(DELETE)。\n"
    "3. 用户说『随便/无所谓/都可以』 → 该槽 `status:\"dontcare\"`(DONTCARE), 值置 null。\n"
    "4. 低置信(<0.6)但你又觉得可能是某槽 → 仍输出(以便系统渲染待确认卡片), 系统会自动把它放入 pending 而不覆盖已确认值。\n"
    "5. 仅输出 JSON, 不要解释。槽位名必须来自『可用槽位』清单, 不要自创槽名。\n"
)


def build_sir_user_prompt(
    text: str,
    *,
    prior_sir: dict,
    active_intent_candidate: str,
    prior_intent_id: str = "",
) -> str:
    """构造让 LLM 专注『状态理解』的 user prompt, 注入强先验。

    prior_sir: 上一轮 SIR 根(已 normalize_sir), 用于快照已有槽位 + 现 pending。
    active_intent_candidate: 本轮最可能意图(来自 _llm_rule / 向量 top1 / 规则), 作为强先验。
    prior_intent_id: 上一轮 active_intent(若切换须让 LLM 显式写 meta.active_intent)。
    """
    # 已有槽位快照(省略 source/updated_at 等内部戳, 仅给语义)
    prior_slots_view = {
        k: {"value": v.get("value"), "status": v.get("status")}
        for k, v in (prior_sir.get("slots") or {}).items()
    }
    prior_pending = list(prior_sir.get("pending") or [])
    prior_stab = prior_sir.get("meta", {}).get("intent_stability", "unstable")

    # 本意图 required_slots 作为『可用槽位』强先验
    req = required_slots_of(active_intent_candidate) if active_intent_candidate else []
    slot_hint = "、".join(req) if req else "（无显式必填槽, 按需抽取）"

    # 意图切换提示
    switch_hint = ""
    if prior_intent_id and active_intent_candidate and prior_intent_id != active_intent_candidate:
        switch_hint = (
            f"\n注意: 当前最可能意图已由『{prior_intent_id}』切换为『{active_intent_candidate}』"
            f"(意图切换会清掉非『{active_intent_candidate}』拥有且非跨意图常驻的槽)。"
        )

    return (
        f"当前最可能意图(强先验, 不要自创): {active_intent_candidate or '未定'}\n"
        f"该意图可用槽位(只能从这些里填): {slot_hint}{switch_hint}\n\n"
        f"上一轮 SIR 槽位快照(沿用, 勿重复): {json.dumps(prior_slots_view, ensure_ascii=False) or '空'}\n"
        f"上一轮 pending(待确认): {json.dumps(prior_pending, ensure_ascii=False) or '无'}\n"
        f"上一轮意图稳定性: {prior_stab}\n\n"
        f"用户最新输入: {text[:500]}\n\n"
        "请仅输出本轮 SIR_delta JSON:"
    )


async def _extract_sir_delta(
    text: str,
    *,
    model_id: str,
    prior_sir: dict,
    active_intent_candidate: str,
    prior_intent_id: str = "",
) -> SIRDelta:
    """调用 LLM 解析用户最新输入, 产出经强校验的 SIRDelta。

    失败时降级: 返回空 SIRDelta(仅带 active_intent 先验), 让 DST 维持原状态,
    **不产生脏状态**。返回对象总合法(parse_sir_delta 已校验枚举/类型)。

    调用方将返回的 delta 交给 `apply_delta`，由调用点显式声明来源优先级。
    """
    order = resolve_fallback_order(model_id)
    last_e: Exception | None = None
    for mid in order:
        t0 = time.monotonic()
        try:
            chat = get_chat_model(mid, streaming=False)
            resp = await chat.ainvoke([
                {"role": "system", "content": SIR_SYSTEM},
                {"role": "user", "content": build_sir_user_prompt(
                    text, prior_sir=prior_sir,
                    active_intent_candidate=active_intent_candidate,
                    prior_intent_id=prior_intent_id,
                )},
            ])
            usage = getattr(resp, "response_metadata", {}) or {}
            usage = usage.get("usage") or {}
            tin = int(usage.get("prompt_tokens", 0) or 0)
            tout = int(usage.get("completion_tokens", 0) or 0)
            await record_llm_call(
                mid, True, (time.monotonic() - t0) * 1000,
                tokens_in=tin, tokens_out=tout,
            )
            raw = _response_text(resp.content).strip()
            logger.info("[SIR] LLM 解析 delta model=%s raw=%.300s", mid, raw)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            delta, err = parse_sir_delta(data)
            if err:
                logger.warning("[SIR] delta 校验失败, 降级空 delta: %s", err)
                empty = SIRDelta(meta={"active_intent": active_intent_candidate})
                return empty
            # 把 active_intent 先验补进 delta.meta(若 LLM 没给则用候选)
            if not delta.active_intent:
                delta.meta = dict(delta.meta or {})
                delta.meta["active_intent"] = active_intent_candidate
            return delta
        except Exception as e:  # noqa: BLE001
            last_e = e
            await record_llm_call(
                mid, False, (time.monotonic() - t0) * 1000,
                error_type=type(e).__name__,
            )
            logger.warning("[SIR] LLM delta 模型%s失败: %s", mid, e)
            continue
    # 全部失败 → 降级空 delta(保留 active_intent 先验)
    logger.error("[SIR] 全部模型失败, 降级空 delta: %s", last_e)
    return SIRDelta(meta={"active_intent": active_intent_candidate})
