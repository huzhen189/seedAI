"""治理联动：以 ``ToolMeta`` 为审批/风险真相源（规范 §9.2 + docs/06 §3 Phase 4）。

S5（审批闸门）与 S6（执行）原来各自硬编码 ``{"publish","purge","trash"}`` 判定
``gated`` / ``risk``，与 ``ToolMeta.requires_approval`` / ``risk`` 脱节
（例如 ``project_recycle`` 是 MID 不要求审批，却被硬编码当成高危拦截）。

本模块统一收敛：
- 已知可对应到原子工具的动作，优先读该 ``ToolMeta`` 的 ``requires_approval`` / ``risk``；
- 无映射（如项目 ``publish`` 部署走 ``project_ops.publish``、非单一 tool）时，退回
  旧的 speech_act 兜底规则，**并集语义**——既保留既有拦截行为（零回归），
  又自动兜住 ``site_deploy``(critical)/``site_delete``(high) 等曾被遗漏的高危动作。

这样治理判定只剩「一处真相」，S5/S6 不再各写一份魔法集合。
"""

from __future__ import annotations

from app.core.contracts import RiskLevel
from app.tools._registry import get_registry

# speech_act → 治理归属的 tool_id。仅列「可明确对应到某个原子工具」的动作；
# 项目 ``publish`` 部署故意不映射（走 project_ops.publish，非单一 tool 审批），
# 由下方旧规则兜底，确保仍被 S5 闸门拦截。
ACTION_TOOL_MAP: dict[str, str] = {
    "trash": "project_recycle",
    "purge": "project_purge",
    "deploy": "site_deploy",
    "delete": "site_delete",
}

# 旧规则兜底的高危动作集合（与历史行为一致）。
_LEGACY_GATED = frozenset({"publish", "purge", "trash"})

_RISK_TO_LABEL = {
    RiskLevel.CRITICAL: "critical",
    RiskLevel.HIGH: "high",
    RiskLevel.MID: "mid",
    RiskLevel.LOW: "low",
}

# risk 标签的偏序，用于取「上界」——统一治理**只允许升级、不允许降级**风险等级。
# 例：project_recycle 的 ToolMeta.risk=MID，但历史 S5/S6 把 trash 审计成 high；
# 若直接改读 ToolMeta 会把审计风险降级，属安全回退，故取 max(meta, legacy)。
_RISK_ORDER = {"low": 0, "mid": 1, "high": 2, "critical": 3}


def _legacy_risk_label(speech_act: str) -> str:
    """历史硬编码推导（与改造前 S5:63 / S6:223 完全一致）。"""
    if speech_act in {"publish", "purge"}:
        return "critical"
    if speech_act == "trash":
        return "high"
    return "low"


def _tool_meta(speech_act: str):
    tool_id = ACTION_TOOL_MAP.get(speech_act)
    if not tool_id:
        return None
    try:
        return get_registry().get(tool_id)
    except KeyError:
        return None


def action_requires_approval(speech_act: str) -> bool:
    """该动作是否需要人工审批。

    并集语义：``ToolMeta.requires_approval`` **或** 旧规则兜底，任一为真即拦截。
    返回 True 时 S5 必须挂起审批，S6 必须拒绝直执行。
    """
    meta = _tool_meta(speech_act)
    tool_req = bool(meta and meta.requires_approval)
    legacy = speech_act in _LEGACY_GATED
    return tool_req or legacy


def action_risk_label(speech_act: str) -> str:
    """审计用 risk 标签（low/mid/high/critical）。

    取 ``max(ToolMeta.risk, 历史推导)``：
    - 有工具映射时能自动兜住 ``site_deploy``(critical)/``site_delete``(high) 等旧规则漏判；
    - 同时保证不会把历史已判定的等级降下来（``trash`` 恒 ≥ high），审计只升不降。
    """
    legacy = _legacy_risk_label(speech_act)
    meta = _tool_meta(speech_act)
    if meta is None:
        return legacy
    from_meta = _RISK_TO_LABEL.get(meta.risk, "low")
    return from_meta if _RISK_ORDER[from_meta] > _RISK_ORDER[legacy] else legacy


def governance_basis(speech_act: str) -> str:
    """返回可读的治理依据串，供 S5/S6 结构化日志审计。

    形如 ``tool=project_purge(requires_approval=True,risk=critical)+legacy(gated=True)``，
    出问题时一眼看出闸门是被工具元数据触发还是被历史规则兜底触发。
    """
    legacy_gated = speech_act in _LEGACY_GATED
    meta = _tool_meta(speech_act)
    if meta is None:
        return f"speech_act_fallback(gated={legacy_gated})"
    return (
        f"tool={meta.tool_id}(requires_approval={meta.requires_approval},"
        f"risk={meta.risk.value})+legacy(gated={legacy_gated})"
    )
