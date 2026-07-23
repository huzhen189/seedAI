"""语义模块: LLM 深度意图分类(AI调用, ~2s)。

输出 SemanticResult {level1, level2, industry, confidence, checkpoint_relation, raw_output, latency_ms}
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from ..providers import get_chat_model, resolve_fallback_order
from .common import (
    VALID_LEVEL1, VALID_LEVEL2, VALID_INDUSTRIES, OLD_TO_LEVELS,
    UNSUPPORTED_HINT,
)

logger = logging.getLogger("ai_service.intent.semantic")

INTENT_SYSTEM = (
    "你是智能建站助手小胡的意图分类器。根据用户输入, 只返回 JSON, 不要额外文字。\n"
    '{"level1": "...", "level2": "...", "confidence": 0.0~1.0, "industry": "..."}\n\n'
    "level1(3选1): chat|build|unsupported\n\n"
    "level2(只选对应 level1 下的):\n"
    "chat→casual(打招呼/闲聊/情感表达)|explain(解释概念/答疑)|compare(技术对比)|search(搜索查资料)|design(UI设计/配色/布局咨询)|translate(翻译)\n"
    "build→requirement(需求分析/规划方案)|site(建完整网站)|page(单页/落地页)|modify(修改已有代码)|fix(修Bug)|review(代码评审)|game(互动小游戏)\n"
    "unsupported→无子类\n"
    f"{UNSUPPORTED_HINT}\n\n"
    "industry(13选1, build/requirement 时必填, 其他填 none):\n"
    "restaurant(餐饮)|ecommerce(电商)|gov(政务)|edu(教育)|health(医疗)\n"
    "|finance(金融)|game(游戏)|personal(个人)|corp(企业)|tech(科技)|media(媒体)|travel(旅游)|other\n\n"
    "checkpoint_relation(5选1, 仅当存在断点时返回, 否则填 none):\n"
    "- resume: 用户要继续上次的工作(如'继续''接着做')\n"
    "- correct: 用户在旧基础上改(如'改导航''换个颜色')\n"
    "- override: 用户要重来(如'重新做一个')\n"
    "- unrelated: 说另一件事,和断点无关\n"
    "- unclear: 无法判断\n"
    "- none: 不存在断点或不需要判断\n\n"
    "裁决规则(你必须自行决断, 不要把选择推给用户 —— 下游系统会自动选最具体的技能):\n"
    "1. build 但不确定 site/page/modify 时: 默认选 'site'(完整站); 仅当用户明确说'单页/一个页面'才选 page, '改一下/修改已有'才选 modify, '小游戏/游戏'才选 game。\n"
    "2. chat 但不确定子类时: 默认选 'explain'(解释说明); 仅当明确是报错排查才 debug(归 explain), 技术对比才 compare, UI设计才 design, 搜索查资料才 search。\n"
    "3. 需求/方案类('帮我做网站''做什么功能''规划一下''需求')→ build/requirement(状态路由会先走需求分析); 打招呼/闲聊('你好''谢谢''你是谁')→ chat/casual。\n"
    "4. 不要并列输出多个 level2 让系统二选一; 只选一个最贴切的。\n"
    "5. 置信度: 只要你给出了合理 level1/level2, 置信度给 >=0.6; 只有完全无法归类时才给 low 并 level1=unsupported。\n\n"
    "用户输入: "
)


@dataclass
class SemanticResult:
    level1: str = "learn"
    level2: str = "casual"
    industry: str = "other"
    confidence: float = 0.5
    checkpoint_relation: str = "none"
    raw_output: str = ""
    latency_ms: float = 0.0


async def run_semantic(messages: list[dict], model_id: str = "deepseek",
                       context_hint: str = "",
                       checkpoint_info: dict | None = None) -> SemanticResult:
    """语义模块入口: 异步 LLM 分类。"""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last.strip():
        return SemanticResult()

    if context_hint:
        last = f"用户输入: {last}\n上下文: {context_hint}"

    sys_prompt = INTENT_SYSTEM
    if checkpoint_info:
        ck = checkpoint_info
        sys_prompt = INTENT_SYSTEM.replace(
            "用户输入: ",
            f"断点: 阶段={ck.get('stage','?')} 进度={ck.get('pct',0)}% 标题=\"{ck.get('title','')}\"\n"
            f"用户输入: ",
        )

    t0 = time.time()
    order = resolve_fallback_order(model_id)
    last_e = None
    for mid in order:
        try:
            chat = get_chat_model(mid, streaming=False)
            resp = await chat.ainvoke([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": last[:500]},
            ])
            raw = (resp.content or "").strip()
            elapsed = (time.time() - t0) * 1000
            logger.info("[语义] LLM返回 model=%s 耗时=%.0fms raw=%.200s", mid, elapsed, raw)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            l1 = data.get("level1", data.get("intent", ""))
            l2 = data.get("level2", "")
            confidence = float(data.get("confidence", 0.5))
            industry = data.get("industry", "none") or "none"
            ck_rel = data.get("checkpoint_relation", "none") or "none"

            if industry not in VALID_INDUSTRIES:
                industry = "other"
            if ck_rel not in ("resume", "correct", "override", "unrelated", "unclear", "none"):
                ck_rel = "none"

            if l1 in VALID_LEVEL1 and l2 in VALID_LEVEL2:
                return SemanticResult(level1=l1, level2=l2, industry=industry,
                                     confidence=confidence, checkpoint_relation=ck_rel,
                                     raw_output=raw, latency_ms=elapsed)
            old = data.get("intent", "")
            if old in OLD_TO_LEVELS:
                l1, l2 = OLD_TO_LEVELS[old]
                return SemanticResult(level1=l1, level2=l2, industry=industry,
                                     confidence=confidence, checkpoint_relation=ck_rel,
                                     raw_output=raw, latency_ms=elapsed)
            break
        except Exception as e:
            last_e = e
            logger.warning("[语义] 模型%s调用失败: %s", mid, e)
            continue

    # 所有模型失败 → 降级
    if last_e:
        raise last_e
    return SemanticResult(raw_output="", latency_ms=(time.time() - t0) * 1000)
