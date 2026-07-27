"""§4 角色重构 · 强 Schema 交接物(方案 B-轻 · 叶子模块)。

本模块是 roles 包的「叶子」,只依赖标准库,任何模块(runner / orchestrator / role agents)
都可安全 import,不会引发循环依赖。

职责:
- 定义四角色与技能的映射(ROLE_FOR_SKILL)、SOP 顺序(ROLE_ORDER)、角色中文标签。
- 定义 RoleHandoff 强 Schema 交接物(PRD / DesignSpec / CodeArtifact / ReviewReport),
  每个角色产出一份结构化交接物,由 Orchestrator 经 SharedContext 在角色间传递(上下文隔离 +
  强交接物,而非整段聊天无差别透传)。
- 提供 build_upstream_context():按 SOP 顺序把「上游角色交付物」渲染成下游角色可参考的
  上下文块(强交接物注入),实现角色间上下文隔离。
- ROLE_ORCHESTRATOR_ENABLED:稳健开关(默认开),关闭时 RoleOrchestrator 退化为原生
  Orchestrator,run_skill 退化为无角色增强,保证零破坏回退。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("ai_service.roles.handoff")

# ── 稳健开关:默认开启角色编排层;设 ROLE_ORCHESTRATOR_ENABLED=0 即整体回退原生编排 ──
ROLE_ORCHESTRATOR_ENABLED = os.getenv("ROLE_ORCHESTRATOR_ENABLED", "1") == "1"

# ── 四角色与技能映射(单一来源) ──
# ProductAgent ← agent_requirement(出 PRD)
# DesignAgent  ← agent_design(出 DesignSpec)
# DevAgent     ← agent_build / agent_generate_site / agent_doc(出 CodeArtifact)
# QAAgent      ← agent_review + scoring(出 ReviewReport)
# agent_search / agent_chat / agent_delete 不归属四角色(跨角色支撑 / 兜底 / 独立)。
ROLE_FOR_SKILL: dict[str, str] = {
    "agent_requirement": "product",
    "agent_design": "design",
    "agent_build": "dev",
    "agent_generate_site": "dev",
    "agent_doc": "dev",
    "agent_review": "qa",
}

# 角色 → 交付物类型(强 Schema 标识)
ROLE_ARTIFACT: dict[str, str] = {
    "product": "prd",
    "design": "design_spec",
    "dev": "code_artifact",
    "qa": "review_report",
}

# 角色中文标签(日志/统计展示)
ROLE_LABEL: dict[str, str] = {
    "product": "产品分析师",
    "design": "设计顾问",
    "dev": "开发工程师",
    "qa": "质量评审",
}

# SOP 默认顺序:PRD → DesignSpec → CodeArtifact → ReviewReport
# 下游角色仅可见排在自身之前的上游交付物(上下文隔离)。
ROLE_ORDER: list[str] = ["product", "design", "dev", "qa"]


def map_skill_to_role(skill: str) -> Optional[str]:
    """技能名 → 角色名;非四角色技能返回 None(走原生无角色增强路径)。"""
    return ROLE_FOR_SKILL.get(skill)


@dataclass
class RoleHandoff:
    """强 Schema 交接物:某角色一次执行的产出,供下游角色按 SOP 顺序消费。

    raw 保存完整文本产出(保真),structured 为尽力解析的结构化字段(供下游注入参考),
    summary 为简短摘要(注入下游上下文块用,避免整段聊天无差别透传)。
    """

    role: str                      # 产出角色(product/design/dev/qa)
    skill: str                     # 产出技能名
    artifact_type: str             # prd / design_spec / code_artifact / review_report
    summary: str = ""              # 简短摘要(注入下游上下文)
    raw: str = ""                  # 完整文本产出
    structured: dict = field(default_factory=dict)  # 尽力解析的结构化字段
    captured_at: float = 0.0       # 捕获时间戳

    def to_ref_block(self) -> str:
        """渲染为可注入下游角色 system 上下文的参考块。"""
        label = ROLE_LABEL.get(self.role, self.role)
        head = f"【上游交付物 · {label}({self.artifact_type})】"
        body = self.summary or (self.raw[:600] if self.raw else "(无)")
        return f"{head}\n{body}"


def _extract_first_json(text: str) -> Optional[dict]:
    """尽力从文本中提取第一个 JSON 对象(兼容 ```json 代码块 / 裸 JSON)。"""
    if not text:
        return None
    # 优先整段解析(模型若只输出纯 JSON,避免报告内字符干扰正则)
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def build_upstream_context(role: str, shared_ctx: Any) -> str:
    """按 SOP 顺序,把当前角色之前的所有上游交付物渲染为参考上下文块。

    - 上下文隔离:下游角色只看排在自身之前的角色交付物,不 dump 整段聊天历史。
    - 强交接物:用 RoleHandoff.to_ref_block() 渲染,而非整段透传。
    """
    if shared_ctx is None:
        return ""
    handoffs = getattr(shared_ctx, "handoffs", None) or {}
    if not handoffs:
        return ""
    try:
        idx = ROLE_ORDER.index(role)
        upstream_roles = ROLE_ORDER[:idx]
    except ValueError:
        upstream_roles = list(ROLE_ORDER)
    blocks: list[str] = []
    for r in upstream_roles:
        h = handoffs.get(r)
        if h and isinstance(h, RoleHandoff):
            blocks.append(h.to_ref_block())
    return "\n\n".join(blocks)


def build_handoff(
    role: str,
    skill: str,
    output_text: str,
    artifacts: Optional[list] = None,
) -> RoleHandoff:
    """按角色构造强 Schema 交接物(尽力解析结构化字段 + 生成摘要)。"""
    artifact_type = ROLE_ARTIFACT.get(role, "unknown")
    structured: dict = {}
    summary = ""

    if role == "product":
        data = _extract_first_json(output_text)
        if isinstance(data, dict):
            structured = data
            brand = (data.get("brand") or {})
            name = brand.get("name") or "未命名"
            n_pages = len(data.get("pages", []) or [])
            n_feats = len(data.get("features", []) or [])
            summary = f"PRD:品牌={name},页面={n_pages},功能={n_feats}"
    elif role == "design":
        data = _extract_first_json(output_text)
        if isinstance(data, dict):
            structured = data
            style = data.get("style") or (data.get("design") or {}).get("style", "?")
            summary = f"设计规格:风格={style}"
    elif role == "dev":
        urls = [str(a) for a in (artifacts or []) if a]
        structured = {"urls": urls, "code_len": len(output_text)}
        summary = f"代码产物:预览URL={len(urls)}个,代码长度={len(output_text)}"
    elif role == "qa":
        data = _extract_first_json(output_text)
        if isinstance(data, dict):
            structured = data
            overall = data.get("overall") or (data.get("scores") or {}).get("overall")
            summary = f"评审报告:综合评分={overall}"
    else:
        summary = f"{artifact_type}:长度={len(output_text)}"

    return RoleHandoff(
        role=role,
        skill=skill,
        artifact_type=artifact_type,
        summary=summary,
        raw=output_text,
        structured=structured,
        captured_at=time.time(),
    )
