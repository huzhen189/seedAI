"""安全模块: 权限检查 + 风险检测 + 二次确认判断。

输出 SafetyResult {risk_level, requires_confirm, permissions_ok, block_reason, risk_tags}
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 风险关键词表
CRITICAL_KEYWORDS = {
    "删除", "清空", "drop", "rm ", "remove", "del ", "delete",
    "支付", "付款", "充值", "订单", "交易", "转账",
    "密码", "密钥", "token", "api_key", "secret",
}

HIGH_KEYWORDS = {
    "发布", "上线", "deploy", "publish",
    "管理", "admin", "后台",
    "修改权限", "更改角色",
}

MEDIUM_KEYWORDS = {
    "修改", "改", "modify", "update", "更新",
    "新增", "添加", "add", "create",
}


@dataclass
class SafetyResult:
    risk_level: str = "low"          # "low"|"medium"|"high"|"critical"
    requires_confirm: bool = False
    permissions_ok: bool = True
    block_reason: str = ""
    risk_tags: list[str] = field(default_factory=list)


def run_safety(messages: list[dict]) -> SafetyResult:
    """安全模块入口: 检测用户输入中的风险。"""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last:
        return SafetyResult()

    t = last.lower()
    risk_tags = []
    risk_level = "low"

    # critical 检测
    for kw in CRITICAL_KEYWORDS:
        if kw in t:
            risk_tags.append(kw)
    if risk_tags:
        return SafetyResult(
            risk_level="critical",
            requires_confirm=True,
            permissions_ok=False,
            block_reason=f"检测到高风险关键词: {', '.join(risk_tags)}",
            risk_tags=risk_tags,
        )

    # high 检测
    for kw in HIGH_KEYWORDS:
        if kw in t:
            risk_tags.append(kw)
    if risk_tags:
        return SafetyResult(
            risk_level="high",
            requires_confirm=True,
            permissions_ok=True,
            block_reason=f"需要二次确认: {', '.join(risk_tags)}",
            risk_tags=risk_tags,
        )

    # medium 检测
    for kw in MEDIUM_KEYWORDS:
        if kw in t:
            risk_tags.append(kw)
    if risk_tags:
        return SafetyResult(risk_level="medium", risk_tags=risk_tags)

    return SafetyResult()
