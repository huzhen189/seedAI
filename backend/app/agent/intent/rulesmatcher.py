"""规则匹配器(混合级联第①步: 强信号直路由, v1.2.0)。

load_rules(): 加载 rules_catalog.json(带缓存)。
match_rules(text): 返回命中规则列表, 按 strength(strong 优先) + 模式长度(更具体优先)排序。

注意: 命中即视为『强信号』, 由 cascade.py 决定是否触发向量 super-fast 直通(跳过 LLM)。
规则目录与意图目录解耦: 规则只指向 intent_id, 不直接写 skill/level, 保持单一事实来源。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("ai_service.intent.rules")

_RULES_PATH = Path(__file__).resolve().parent / "rules_catalog.json"

# strength → 置信度基准
_STRENGTH_CONF = {"strong": 0.95, "medium": 0.82}


@dataclass
class RuleMatch:
    rule_id: str
    intent_id: str
    strength: str
    confidence: float
    pattern: str = ""          # 命中的具体 pattern(调试用)
    required_slots: list = field(default_factory=list)


@lru_cache(maxsize=1)
def load_rules() -> list[dict]:
    """加载并缓存规则目录。"""
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        rules = data.get("rules", [])
        logger.info("[规则] 加载规则目录: %d 条 (version=%s)", len(rules), data.get("version", "?"))
        return rules
    except Exception as e:  # pragma: no cover
        logger.error("[规则] 加载失败: %s", e)
        return []


def match_rules(text: str) -> list[RuleMatch]:
    """匹配用户输入与规则目录, 返回有序命中列表(strong 优先, 长 pattern 优先)。"""
    if not text or not text.strip():
        return []
    hits: list[RuleMatch] = []
    for rule in load_rules():
        patterns = rule.get("patterns", [])
        matched_pat = next((p for p in patterns if p in text), None)
        if matched_pat is None:
            continue
        strength = rule.get("strength", "medium")
        conf = _STRENGTH_CONF.get(strength, 0.8)
        hits.append(RuleMatch(
            rule_id=rule.get("id", ""),
            intent_id=rule.get("target", ""),
            strength=strength,
            confidence=conf,
            pattern=matched_pat,
            required_slots=rule.get("required_slots", []),
        ))
    # 排序: strong 在前; 同 strength 按命中 pattern 长度降序(更具体优先)
    hits.sort(key=lambda h: (0 if h.strength == "strong" else 1, -len(h.pattern)))
    return hits
