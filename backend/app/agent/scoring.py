"""统一质量打分维度(7 维, 单一来源), v1.2.0 收敛。

背景(C3): 原 reviewer(agent_generate_site / agent_build 的 SYS_REVIEWER)使用
  correctness/completeness/readability/compliance/efficiency/craft —— 有 craft 无 safety;
  原 QC(qc.py)使用
  correctness/completeness/compliance/efficiency/readability/safety —— 有 safety 无 craft。
  两者维度割裂, 且每次生成跑 4 次 LLM 打分(C4)。

现统一为 7 维, reviewer 与 QC 共用同一套定义 + 解析/聚合工具, 维度对齐:
  correctness  正确性
  completeness 完整性
  readability  可读性
  compliance   合规性
  efficiency   效率
  craft        精致度(视觉/交互高级感, premium 核心指标)
  safety       安全性

解析/判定工具供 qc.py(三裁判聚合) 与 skills 的 reviewer(单 LLM 门控) 复用。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("ai_service.scoring")

# 统一 7 维(顺序即雷达图轴序 / 解析顺序)
SCORING_DIMENSIONS: list[str] = [
    "correctness", "completeness", "readability",
    "compliance", "efficiency", "craft", "safety",
]

# 走 LLM 三裁判评分的维度(QC 中其余走确定性地板, 零 LLM 成本)
LLM_SCORING_DIMS = ("correctness", "completeness", "readability", "craft")

# 确定性维度(QC 中用 run_safety 地板 + 固定基线)
DETERMINISTIC_DIMS = ("compliance", "efficiency", "safety")


def parse_scores(raw_obj: dict, default: int = 0) -> dict[str, int]:
    """从模型 JSON 解析 7 维评分(1-10 整数), 缺失/异常维度填 default。

    返回固定顺序的 dict(键=SCORING_DIMENSIONS), 供聚合/雷达图直接使用。
    """
    dims: dict[str, int] = {}
    for d in SCORING_DIMENSIONS:
        v = raw_obj.get(d)
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, (int, float)):
            dims[d] = max(1, min(10, int(round(v))))
        else:
            dims[d] = default
    return dims


def empty_scores(default: int = 0) -> dict[str, int]:
    return {d: default for d in SCORING_DIMENSIONS}


def needs_review(scores: dict[str, int], min_dim: int = 6) -> bool:
    """任一维低于 min_dim → 需复核(reviewer 用它决定是否升级到 QC / 修复循环)。"""
    return any(scores.get(d, 0) < min_dim for d in SCORING_DIMENSIONS)
