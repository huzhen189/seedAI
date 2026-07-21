"""安全模块: 权限检查 + 风险检测 + 二次确认判断。

输出 SafetyResult {risk_level, requires_confirm, permissions_ok, block_reason, risk_tags}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .common import (
    SAFETY_CRITICAL_KEYWORDS,
    SAFETY_HIGH_KEYWORDS,
    SAFETY_MEDIUM_KEYWORDS,
)

logger = logging.getLogger("ai_service.intent.safety")

# 单一来源: 关键词集定义在 intent/common.py, 防与 INTENT_SYSTEM 漂移(Tier 3)
CRITICAL_KEYWORDS = SAFETY_CRITICAL_KEYWORDS
HIGH_KEYWORDS = SAFETY_HIGH_KEYWORDS
MEDIUM_KEYWORDS = SAFETY_MEDIUM_KEYWORDS


@dataclass
class SafetyResult:
    risk_level: str = "low"
    requires_confirm: bool = False
    permissions_ok: bool = True
    block_reason: str = ""
    risk_tags: list[str] = field(default_factory=list)


def run_safety(messages: list[dict], project_constraints: list[str] | None = None) -> SafetyResult:
    """安全模块入口: 检测风险。

    project_constraints: 项目级结构化禁用词(Tier 2), 命中即 critical 拦截。
    """
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last:
        return SafetyResult()

    t = last.lower()
    risk_tags = []

    # 项目级硬约束(Tier 2): 命中结构化 --forbid 词 → 直接拦截(不可绕过)
    if project_constraints:
        hits = [kw for kw in project_constraints if kw and kw in t]
        if hits:
            logger.warning("[安全] 🔴 项目约束命中=%s → 拦截", hits)
            return SafetyResult(
                risk_level="critical", requires_confirm=True, permissions_ok=False,
                block_reason=f"项目禁止: {', '.join(hits)}", risk_tags=hits,
            )

    for kw in CRITICAL_KEYWORDS:
        if kw in t:
            risk_tags.append(kw)
    if risk_tags:
        logger.warning("[安全] 🔴 critical 关键词=%s → 拦截", risk_tags)
        return SafetyResult(risk_level="critical", requires_confirm=True,
                           permissions_ok=False,
                           block_reason=f"高风险: {', '.join(risk_tags)}",
                           risk_tags=risk_tags)

    for kw in HIGH_KEYWORDS:
        if kw in t:
            risk_tags.append(kw)
    if risk_tags:
        logger.info("[安全] 🟡 high 关键词=%s → 需二次确认", risk_tags)
        return SafetyResult(risk_level="high", requires_confirm=True,
                           block_reason=f"需确认: {', '.join(risk_tags)}",
                           risk_tags=risk_tags)

    for kw in MEDIUM_KEYWORDS:
        if kw in t:
            risk_tags.append(kw)
    if risk_tags:
        logger.info("[安全] 🟠 medium 关键词=%s", risk_tags)
        return SafetyResult(risk_level="medium", risk_tags=risk_tags)

    logger.info("[安全] 🟢 low 无风险")
    return SafetyResult()
