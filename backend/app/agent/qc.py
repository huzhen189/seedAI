"""后置质检(QC)单裁判模块 v2.3.0。

设计要点(v2.3.0 改造, 取代三裁判并行):
- 直接用「本次生成所用模型」跑一次 QC(单裁判), 不再 deepseek/qwen/hy3 并行 → 降本。
- 6 维度: correctness / completeness / compliance / efficiency / readability / safety, 各 1-10 整数。
- 引擎维度(correctness / completeness / readability / craft 由 LLM 打分;
  compliance / efficiency / safety 叠加 run_safety 确定性地板 + 固定基线, 零额外成本)。
- 复核判定: overall 低于 config.qc_solo_needs_review_overall 即标 needs_review
  → 触发质量闭环重做(建站)或闲聊 Phase D 重答。低分打回由主链路闭环兜底, 故不再需要多裁判互验。
- 韧性: 单次调用失败返回 partial=True 降级(不影响主链路落库 / 展示)。

输出结构(与旧三裁判版保持兼容, 可直接作为 SSE `qc` 事件 data / 落库 / 供后台雷达图):
{
  "judges": [{"model": "<所用模型>", "valid": true, "comment": "..."}],  # 长度=1(单裁判)
  "dimensions": {                                                          # 键=QC_DIMENSIONS
     "<dim>": {"mean": float, "variance": float, "scores": [d]}            # scores 长度=1
  },
  "overall": float,          # 6 维均值的平均(整体评分)
  "needs_review": bool,      # overall < 阈值 → 需复核(触发重做/重答)
  "safety_risk": str,        # low|medium|high|critical (来自 run_safety 地板)
  "partial": bool            # 单裁判调用失败 → True
}
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from .providers import get_chat_model
from .config import settings
from .scoring import (
    SCORING_DIMENSIONS, LLM_SCORING_DIMS, parse_scores,
)
from .analytics import record_qc, record_llm_call

logger = logging.getLogger("ai_service.qc")

# 维度定义(顺序即雷达图轴序), 与 scoring.SCORING_DIMENSIONS 对齐(单一来源)
QC_DIMENSIONS: List[str] = list(SCORING_DIMENSIONS)

# 走 LLM 打分的维度(其余走确定性地板); 与 scoring.LLM_SCORING_DIMS 对齐
_LLM_DIMS = tuple(LLM_SCORING_DIMS)

_SYSTEM_PROMPT = """你是一名严格的中文内容质量评审专家。
请基于「用户请求」与「AI 助手的最终输出」, 从以下维度独立打分(1-10 整数, 10 为最佳):
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
        logger.warning("QC 输出解析失败: %s", e)
        return None


async def _judge_once(model_id: str, user_text: str, assistant_text: str) -> Dict[str, Any]:
    """单裁判打分(非流式 ainvoke), 使用本次生成所用模型。失败返回空维度(标记异常)。

    同时写入 LLM Provider 统计(耗时 / 成功 / Token 用量 / 错误类型)。
    """
    t0 = time.monotonic()
    try:
        chat = get_chat_model(model_id, streaming=False)
        msgs = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_TEMPLATE.format(
                user_text=user_text[:4000], assistant_text=assistant_text[:8000])},
        ]
        resp = await chat.ainvoke(msgs)
        # 提取 Token 用量(OpenAI 兼容协议), 缺省 0
        meta = getattr(resp, "response_metadata", {}) or {}
        usage = meta.get("usage") or {}
        tin = int(usage.get("prompt_tokens", 0) or 0)
        tout = int(usage.get("completion_tokens", 0) or 0)
        await record_llm_call(model_id, True, (time.monotonic() - t0) * 1000,
                              tokens_in=tin, tokens_out=tout)
        raw = resp.content if hasattr(resp, "content") else str(resp)
        parsed = _parse_judge_output(raw)
        if parsed is None:
            logger.warning("QC 模型 %s 输出无法解析(标记无效)", model_id)
            return {"model": model_id, "valid": False,
                    "dims": {d: 0 for d in QC_DIMENSIONS}, "comment": "解析失败"}
        avg = sum(parsed.get(d, 0) for d in QC_DIMENSIONS) / max(len(QC_DIMENSIONS), 1)
        logger.info("[QC] 单裁判打分 model=%s valid=True overall=%.2f", model_id, avg)
        return {"model": model_id, "valid": True, "dims": parsed, "comment": parsed.get("comment", "")}
    except Exception as e:  # noqa: BLE001
        await record_llm_call(model_id, False, (time.monotonic() - t0) * 1000,
                              error_type=type(e).__name__)
        logger.warning("QC 模型 %s 调用失败: %s", model_id, e)
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


def _assemble(judge: Dict[str, Any], safety_risk: str = "low") -> Dict[str, Any]:
    """聚合单裁判打分 → 每维均值 / 方差 + 整体; 叠加确定性地板。

    单裁判下 variance 恒为 0(无多裁判分歧); needs_review 改由 overall 阈值判定。
    """
    dimensions: Dict[str, Any] = {}
    for d in QC_DIMENSIONS:
        score = judge["dims"].get(d, 0)
        dimensions[d] = {
            "mean": float(score),
            "variance": 0.0,
            "scores": [score],  # 单裁判: 长度=1(前台雷达图兼容可空)
        }
    # 确定性维度(合规/效率/安全): 不走 LLM, 由规则地板 + 固定基线决定(降本)
    for d in QC_DIMENSIONS:
        if d not in _LLM_DIMS:
            score = _deterministic_dim(d, safety_risk)
            dimensions[d] = {"mean": score, "variance": 0.0, "scores": [score]}
    # 整体均值(6 维 mean 的平均; mean=0 视为该维无有效分, 不计入)
    means = [dimensions[d]["mean"] for d in QC_DIMENSIONS if dimensions[d]["mean"] > 0]
    overall = round(sum(means) / len(means), 2) if means else 0.0

    # 确定性地板: 安全 / 合规 / 效率(零成本, 来自 run_safety + 规则) → 命中高风险必复核
    if safety_risk in ("high", "critical"):
        dimensions["safety"]["mean"] = min(dimensions["safety"]["mean"], 3.0)
        dimensions["compliance"]["mean"] = min(dimensions["compliance"]["mean"], 4.0)
    elif safety_risk == "medium":
        dimensions["safety"]["mean"] = min(dimensions["safety"]["mean"], 6.0)

    # 复核判定: overall 低于阈值 → 需复核(触发重做/重答)。高风险亦强制复核。
    needs_review = (
        not judge.get("valid")
        or overall < settings.qc_solo_needs_review_overall
        or safety_risk in ("high", "critical")
    )
    partial = not judge.get("valid")

    return {
        "judges": [{"model": judge["model"], "valid": judge.get("valid", False),
                    "comment": judge.get("comment", "")}],
        "dimensions": dimensions,
        "overall": overall,
        "needs_review": needs_review,
        "safety_risk": safety_risk,
        "partial": partial,
    }


async def run_qc(
    user_text: str,
    assistant_text: str,
    project_constraints: Optional[List[str]] = None,
    safety_risk: str = "low",
    model_id: str = "qwen",
) -> Dict[str, Any]:
    """运行单裁判 QC(使用本次生成所用模型), 返回聚合结果(可直接作为 SSE `qc` 事件 data)。

    v2.3.0 起: 取消三裁判并行, 改用 model_id(调用方传入的生成所用模型)单次打分 → 降本。
    低分(overall<阈值)由主链路质量闭环打回重做/重答兜底, 故无需多裁判互验。
    """
    logger.info("[QC] 单裁判评分 model=%s safety_risk=%s", model_id, safety_risk)
    t0 = time.monotonic()
    judge = await _judge_once(model_id, user_text, assistant_text)
    result = _assemble(judge, safety_risk=safety_risk)
    dur = time.monotonic() - t0
    logger.info("[QC] 评分完成 耗时=%.2fs overall=%.2f needs_review=%s partial=%s model=%s",
                dur, result.get("overall", 0), result.get("needs_review"),
                result.get("partial"), model_id)
    # 写入后置 QC 统计(整体/7维/复核率/掉线率/安全风险), 失败仅告警
    try:
        await record_qc(result, dur * 1000)
    except Exception as e:  # noqa: BLE001
        logger.warning("[QC] 统计写入失败(忽略): %s", e)
    return result
