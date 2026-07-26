"""安全模块: 权限检查 + 风险检测 + 二次确认判断。

输出 SafetyResult {risk_level, requires_confirm, permissions_ok, block_reason, risk_tags}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .common import (
    SAFETY_HARD_KEYWORDS,
    SAFETY_SOFT_CRITICAL,
    SAFETY_SOFT_HIGH,
    SAFETY_SOFT_MEDIUM,
    CONSTRUCTIVE_LEADS,
    STRICT_LEADS,
    SAFETY_UI_CONTEXT,
)

logger = logging.getLogger("ai_service.intent.safety")

# 单一来源: 关键词集定义在 intent/common.py, 防与 INTENT_SYSTEM 漂移(Tier 3)
HARD_KEYWORDS = SAFETY_HARD_KEYWORDS
SOFT_CRITICAL = SAFETY_SOFT_CRITICAL
SOFT_HIGH = SAFETY_SOFT_HIGH
SOFT_MEDIUM = SAFETY_SOFT_MEDIUM

# 语境窗口: 关键词前多少字符内找「建设性前导」; 关键词前后多少字符内找「UI/代码语境」
_LEAD_WINDOW = 12
_CTX_WINDOW = 10


@dataclass
class SafetyResult:
    risk_level: str = "low"
    requires_confirm: bool = False
    permissions_ok: bool = True
    block_reason: str = ""
    risk_tags: list[str] = field(default_factory=list)
    neutralized_tags: list[str] = field(default_factory=list)


def _is_neutralized(text: str, kw: str, severity: str) -> bool:
    """SOFT 关键词是否处于「建设性/UI 语境」(应中和, 不拦截)。

    - 关键词前 _LEAD_WINDOW 字内出现建设性前导;
      high 级只用 STRICT_LEADS(做功能/页面类), 避免"帮我删除用户"被误中和;
      critical/medium 用完整 CONSTRUCTIVE_LEADS(含"帮我/请帮我")。
    - 或关键词前后 _CTX_WINDOW 字内出现 UI/代码语境词(页面/按钮/输入框/注释…)
    """
    pos = text.find(kw)
    if pos < 0:
        return False
    leads = STRICT_LEADS if severity == "high" else CONSTRUCTIVE_LEADS
    before = text[max(0, pos - _LEAD_WINDOW):pos]
    if any(lead in before for lead in leads):
        return True
    around = text[max(0, pos - _CTX_WINDOW):pos + len(kw) + _CTX_WINDOW]
    if any(ctx in around for ctx in SAFETY_UI_CONTEXT):
        return True
    return False


def run_safety(messages: list[dict], project_constraints: list[str] | None = None) -> SafetyResult:
    """安全模块入口: 检测风险(短语/上下文感知, v0.8.3)。

    - HARD 关键词命中 → critical 硬拦截(不受语境影响)。
    - SOFT 关键词命中 → 若处于建设性/UI 语境则中和(不拦截), 否则按严重度处理。
    - project_constraints: 项目级结构化禁用词(Tier 2), 命中即 critical 拦截。
    """
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last:
        return SafetyResult()

    t = last.lower()

    # 项目级硬约束(Tier 2): 命中结构化 --forbid 词 → 直接拦截(不可绕过)
    if project_constraints:
        hits = [kw for kw in project_constraints if kw and kw in t]
        if hits:
            logger.warning("[安全] 🔴 项目约束命中=%s → 拦截", hits)
            return SafetyResult(
                risk_level="critical", requires_confirm=True, permissions_ok=False,
                block_reason=f"项目禁止: {', '.join(hits)}", risk_tags=hits,
            )

    # HARD 始终拦截(破坏性/滥用短语)
    hard = [kw for kw in HARD_KEYWORDS if kw in t]
    if hard:
        logger.warning("[安全] 🔴 硬性危险短语=%s → 拦截", hard)
        return SafetyResult(risk_level="critical", requires_confirm=True,
                           permissions_ok=False,
                           block_reason=f"高风险: {', '.join(hard)}", risk_tags=hard)

    # SOFT: 收集各严重度命中, 并中和建设性语境
    crit_hits: list[str] = []
    high_hits: list[str] = []
    med_hits: list[str] = []
    neutralized: list[str] = []

    for kw in SOFT_CRITICAL:
        if kw in t:
            (neutralized if _is_neutralized(t, kw, "critical") else crit_hits).append(kw)
    for kw in SOFT_HIGH:
        if kw in t:
            (neutralized if _is_neutralized(t, kw, "high") else high_hits).append(kw)
    for kw in SOFT_MEDIUM:
        if kw in t:
            (neutralized if _is_neutralized(t, kw, "medium") else med_hits).append(kw)

    # 重叠降级: SOFT critical 裸词若是某 HIGH/MEDIUM 短语的子串, 不升级
    # (如"删除"遇"删除用户"→ 归 high, 不再判 critical)
    for bare in list(crit_hits):
        if any(bare in hw for hw in high_hits) or any(bare in mw for mw in med_hits):
            crit_hits.remove(bare)
            high_hits.append(bare)

    if neutralized:
        logger.info("[安全] 🟦 建设性语境中和(不拦截): %s", neutralized)

    if crit_hits:
        logger.warning("[安全] 🔴 soft critical=%s → 拦截", crit_hits)
        return SafetyResult(risk_level="critical", requires_confirm=True,
                           permissions_ok=False,
                           block_reason=f"高风险: {', '.join(crit_hits)}",
                           risk_tags=crit_hits, neutralized_tags=neutralized)

    if high_hits:
        logger.info("[安全] 🟡 high=%s → 需二次确认", high_hits)
        return SafetyResult(risk_level="high", requires_confirm=True,
                           block_reason=f"需确认: {', '.join(high_hits)}",
                           risk_tags=high_hits, neutralized_tags=neutralized)

    if med_hits:
        logger.info("[安全] 🟠 medium=%s", med_hits)
        return SafetyResult(risk_level="medium", risk_tags=med_hits,
                           neutralized_tags=neutralized)

    logger.info("[安全] 🟢 low 无风险")
    return SafetyResult(neutralized_tags=neutralized)
