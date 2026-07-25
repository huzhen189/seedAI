"""后置质检(QC)三裁判模块 v0.8.5 (M1)。

设计要点:
- 默认开启, 默认三裁判全量(deepseek / qwen / hy3 各按 6 维度独立打分)。
- 6 维度: correctness(正确性) / completeness(完整性) / compliance(合规性) /
  efficiency(效率) / readability(可读性) / safety(安全性), 各 1-10 整数。
- 聚合: 每维取均值 + 方差; 方差大(分歧大)标 needs_review。
- 混合判定(降本): compliance / safety / efficiency 叠加 run_safety 确定性地板 +
  用量确定性规则(零额外成本), 仅 correctness / completeness / readability 走 LLM 三裁判。
- 韧性: 单裁判失败不影响其他; 全部失败则 partial=True 降级; 超时由调用方控制。

输出结构(可直接作为 SSE `qc` 事件 data, 亦可直接落库 / 供后台雷达图):
{
  "judges": [{"model": "deepseek", "valid": true, "comment": "..."}, ...],  # 顺序=QC_JUDGES
  "dimensions": {                                                          # 键=QC_DIMENSIONS
     "<dim>": {"mean": float, "variance": float, "scores": [d, d, d]}      # scores 对齐 QC_JUDGES
  },
  "overall": float,          # 6 维均值的平均(整体评分)
  "needs_review": bool,      # 任一维方差过大 → 需人工复核
  "safety_risk": str,        # low|medium|high|critical (来自 run_safety 地板)
  "partial": bool            # 有裁判失败/未参与
}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from .providers import get_chat_model
from .config import settings
from .scoring import (
    SCORING_DIMENSIONS, LLM_SCORING_DIMS, parse_scores,
)

logger = logging.getLogger("ai_service.qc")

# 7 维度定义(顺序即雷达图轴序), 与 scoring.SCORING_DIMENSIONS 对齐(单一来源)
QC_DIMENSIONS: List[str] = list(SCORING_DIMENSIONS)

# 默认三裁判(全量);顺序即 scores 数组下标。来源: config.qc_judges(单一来源)
QC_JUDGES: List[str] = [m.strip() for m in settings.qc_judges.split(",") if m.strip()]

# 走 LLM 三裁判的维度(其余走确定性地板); 与 scoring.LLM_SCORING_DIMS 对齐
_LLM_DIMS = tuple(LLM_SCORING_DIMS)

_SYSTEM_PROMPT = """你是一名严格的中文内容质量评审专家。
请基于「用户请求」与「AI 助手的最终输出」, 从以下 4 个维度独立打分(1-10 整数, 10 为最佳):
- correctness(正确性): 事实 / 逻辑 / 技术是否准确, 是否答其所问、有无明显错误。
- completeness(完整性): 是否覆盖用户需求的核心点, 有无明显遗漏。
- readability(可读性): 结构清晰、表达易懂、格式规范。
- craft(精致度): 视觉层次 / 留白 / 微交互 / 缓动 / 响应式等是否达到『高级感』。

(注: compliance / efficiency / safety 由系统确定性规则自动评估, 无需你打分。)

仅输出一个 JSON 对象, 不要任何解释或 Markdown 代码块, 格式如下:
{"correctness": <int>, "completeness": <int>, "readability": <int>, "craft": <int>, "comment": "<简短中文总评, 不超过40字>"}
"""

_USER_TEMPLATE = """【用户请求】
{user_text}

【AI 输出】
{assistant_text}

请按上述要求输出 JSON 评分。"""


def _parse_judge_output(raw: str) -> Optional[Dict[str, Any]]:
    """从模型输出中解析 7 维评分 JSON; 缺失 / 异常维度填 0(标记为无效)。

    复用 scoring.parse_scores 保证与 reviewer 维度定义完全一致(单一来源)。
    """
    try:
        s = (raw or "").strip()
        if not s:
            return None
        start = s.find("{")
        end = s.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return None
        obj = json.loads(s[start : end + 1])
        dims: Dict[str, int] = parse_scores(obj)  # 7 维, 缺失/异常填 0
        dims["comment"] = str(obj.get("comment", ""))[:60]
        return dims
    except Exception as e:  # noqa: BLE001
        logger.warning("QC 评委输出解析失败: %s", e)
        return None


async def _judge_one(model_id: str, user_text: str, assistant_text: str) -> Dict[str, Any]:
    """单个裁判打分(非流式 ainvoke)。失败返回空维度(标记异常, 不阻断整体)。"""
    try:
        chat = get_chat_model(model_id, streaming=False)
        msgs = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_TEMPLATE.format(
                user_text=user_text[:4000], assistant_text=assistant_text[:8000])},
        ]
        resp = await chat.ainvoke(msgs)
        raw = resp.content if hasattr(resp, "content") else str(resp)
        parsed = _parse_judge_output(raw)
        if parsed is None:
            logger.warning("QC 评委 %s 输出无法解析", model_id)
            return {"model": model_id, "valid": False,
                    "dims": {d: 0 for d in QC_DIMENSIONS}, "comment": "解析失败"}
        return {"model": model_id, "valid": True, "dims": parsed, "comment": parsed.get("comment", "")}
    except Exception as e:  # noqa: BLE001
        logger.warning("QC 评委 %s 调用失败: %s", model_id, e)
        return {"model": model_id, "valid": False,
                "dims": {d: 0 for d in QC_DIMENSIONS}, "comment": f"调用失败:{type(e).__name__}"}


def _deterministic_dim(dim: str, safety_risk: str) -> float:
    """零成本确定性评分: 合规 / 效率 / 安全由安全地板 + 固定基线决定(不走 LLM)。"""
    if dim == "safety":
        return {"critical": 2.0, "high": 3.0, "medium": 6.0}.get(safety_risk, 9.0)
    if dim == "compliance":
        return 4.0 if safety_risk in ("high", "critical") else 9.0
    if dim == "efficiency":
        return 8.0
    return 8.0


def _aggregate(judges: List[Dict[str, Any]], safety_risk: str = "low") -> Dict[str, Any]:
    """聚合三裁判打分 → 每维均值 / 方差 + 整体; 叠加确定性地板。"""
    valid = [j for j in judges if j.get("valid")]
    dimensions: Dict[str, Any] = {}
    for d in QC_DIMENSIONS:
        scores = [j["dims"].get(d, 0) for j in valid if j["dims"].get(d, 0) > 0]
        if scores:
            mean = sum(scores) / len(scores)
            var = sum((s - mean) ** 2 for s in scores) / len(scores)
        else:
            mean, var = 0.0, 0.0
        # scores 数组对齐 QC_JUDGES(无效/失败填 0, 供后台雷达图识别缺失)
        aligned = [j["dims"].get(d, 0) for j in judges]
        dimensions[d] = {
            "mean": round(mean, 2),
            "variance": round(var, 2),
            "scores": aligned,
        }
    # 确定性维度(合规/效率/安全): 不走 LLM, 由规则地板 + 固定基线决定(降本, 与文档一致)
    for d in QC_DIMENSIONS:
        if d not in _LLM_DIMS:
            score = _deterministic_dim(d, safety_risk)
            dimensions[d] = {
                "mean": score, "variance": 0.0,
                "scores": [score] * len(QC_JUDGES),
            }
    # 整体均值(6 维 mean 的平均; mean=0 视为该维无有效分, 不计入)
    means = [dimensions[d]["mean"] for d in QC_DIMENSIONS if dimensions[d]["mean"] > 0]
    overall = round(sum(means) / len(means), 2) if means else 0.0

    # 最大方差(任一维方差 >= 阈值 ≈ 标准差 >= 2 即分歧大) → 标注需人工复核
    # 阈值来源: config.qc_needs_review_variance(单一来源)
    max_var = max((dimensions[d]["variance"] for d in QC_DIMENSIONS), default=0.0)
    needs_review = max_var >= settings.qc_needs_review_variance

    # 确定性地板: 安全 / 合规 / 效率(零成本, 来自 run_safety + 规则)
    if safety_risk in ("high", "critical"):
        dimensions["safety"]["mean"] = min(dimensions["safety"]["mean"], 3.0)
        dimensions["compliance"]["mean"] = min(dimensions["compliance"]["mean"], 4.0)
        needs_review = True
    elif safety_risk == "medium":
        dimensions["safety"]["mean"] = min(dimensions["safety"]["mean"], 6.0)

    return {
        "judges": [{"model": j["model"], "valid": j["valid"], "comment": j.get("comment", "")}
                   for j in judges],
        "dimensions": dimensions,
        "overall": overall,
        "needs_review": needs_review,
        "safety_risk": safety_risk,
        "partial": len(valid) < len(judges),  # 有评委失败
    }


async def run_qc(
    user_text: str,
    assistant_text: str,
    project_constraints: Optional[List[str]] = None,
    safety_risk: str = "low",
) -> Dict[str, Any]:
    """运行三裁判 QC, 返回聚合结果(可直接作为 SSE `qc` 事件 data)。

    默认三裁判全量并行(deepseek / qwen / hy3); 超时由调用方 asyncio.wait_for 控制。
    project_constraints 预留(当前地板仅用 safety_risk, 后续可扩展约束命中检测)。
    """
    logger.info("[QC] 开始三裁判评分 judges=%s safety_risk=%s", QC_JUDGES, safety_risk)
    t0 = time.monotonic()
    judges = await asyncio.gather(*[
        _judge_one(mid, user_text, assistant_text) for mid in QC_JUDGES
    ])
    result = _aggregate(judges, safety_risk=safety_risk)
    logger.info("[QC] 评分完成 耗时=%.2fs overall=%.2f needs_review=%s partial=%s",
                time.monotonic() - t0, result.get("overall", 0),
                result.get("needs_review"), result.get("partial"))
    return result
