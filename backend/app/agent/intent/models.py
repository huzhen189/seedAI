"""意图识别管线结果契约(混合级联 classify_v3 产出的统一形状)。

v1.2.0 收敛说明: 历史上 SIR 分类器(classify_v2 / pipeline.py)与混合级联(classify_v3 / cascade.py)
共用同一份 PipelineResult 契约, 以便 router / queue / worker 零改动切换。自 SIR 下线后,
本契约仅由 classify_v3 产出, 独立成模块以避免 cascade 依赖已被删除的 pipeline.py。

Contract fields
---------------
- decision: "route" | "block" | "confirm" | "options" | "fallback" | "split" | "clarify"
- selected_skill: 路由选定的技能名
- intent: {level1, level2, confidence, industry}
- plan / evidence / risk / tools: 决策支撑数据
- sub_tasks / split_reason: 多意图编排拆分结果
- clarify_questions / clarify_rounds / request_id: 澄清与可观测字段
- clarify_options / clarify_multi / clarify_allow_free_text / clarify_free_text_hint:
  澄清结构化选项(前端浮动卡片用:单选/多选 + 推荐标记 + 自由输入)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .safety import SafetyResult
from .tools import ToolResult
from ..core.models import SubTask


@dataclass
class PipelineResult:
    intent: dict = field(default_factory=lambda: {
        "level1": "chat", "level2": "casual",
        "confidence": 0.3, "industry": "other",
    })
    plan: list[dict] = field(default_factory=list)
    risk: SafetyResult = field(default_factory=SafetyResult)
    tools: ToolResult = field(default_factory=ToolResult)
    evidence: dict = field(default_factory=dict)
    decision: str = "route"  # "route"|"block"|"confirm"|"options"|"fallback"|"split"|"clarify"
    selected_skill: str = "explain"
    sub_tasks: list[SubTask] = field(default_factory=list)   # 多意图: list[SubTask]
    split_reason: str = ""                            # 拆分原因(供统计/前端展示)
    # 澄清 / 可观测
    clarify_questions: list = field(default_factory=list)
    clarify_rounds: int = 0
    # 澄清结构化选项: 前端浮动卡片渲染用(单选/多选 + 推荐标记 + 自由输入)
    clarify_options: list = field(default_factory=list)   # [{"label": str, "recommended": bool}]
    clarify_multi: bool = False                            # 选项是否多选
    clarify_allow_free_text: bool = True                  # 是否允许开放问答(自由输入)
    clarify_free_text_hint: str = ""                      # 自由输入框提示语
    request_id: str = ""
