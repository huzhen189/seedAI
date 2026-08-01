"""SLO 定义与评估（§15.2 可观测性）。

设计约束：
- SLO 阈值来自规范 §15.2：API 非模型请求 p99<500ms、SSE 首个进度事件 p99<1s、
  事件回放成功率≥99.9%、部署不破坏旧 active 版本=100%、W0 终态伪成功=0。
- 评估为纯函数：接收「已测量值快照」，对每个 SLO 给出 pass/fail/unknown。
- 不可测量（快照缺值）标 unknown，绝不误报（false-alarm 比漏报更有害）。
- 告警持续 5 分钟触发、恢复持续 5 分钟解除（见 security_alerting.AlertManager）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

SloStatus = Literal["pass", "fail", "unknown"]


@dataclass(frozen=True)
class SLO:
    id: str
    name: str
    # "lt": measured < target 为 pass; "gte": measured >= target 为 pass; "eq": == target 为 pass
    comparison: Literal["lt", "gte", "eq"]
    target: float
    unit: str
    description: str


# 规范 §15.2 初始 SLO。
SLOS: Final[list[SLO]] = [
    SLO(
        id="api_p99_ms",
        name="API 非模型请求 p99",
        comparison="lt",
        target=500.0,
        unit="ms",
        description="API 非模型请求延迟 p99 须 < 500ms",
    ),
    SLO(
        id="sse_first_event_p99_ms",
        name="SSE 首个进度事件 p99",
        comparison="lt",
        target=1000.0,
        unit="ms",
        description="SSE 首个进度事件延迟 p99 须 < 1s",
    ),
    SLO(
        id="replay_success_rate",
        name="事件回放成功率",
        comparison="gte",
        target=0.999,
        unit="ratio",
        description="事件回放成功率须 ≥ 99.9%",
    ),
    SLO(
        id="deploy_no_break",
        name="部署不破坏旧 active 版本",
        comparison="eq",
        target=1.0,
        unit="ratio",
        description="部署不得破坏旧 active 版本（=100%）",
    ),
    SLO(
        id="w0_pseudo_success",
        name="W0 终态伪成功",
        comparison="eq",
        target=0.0,
        unit="count",
        description="W0 失败不得发送 done(success)（=0）",
    ),
]


@dataclass
class SloResult:
    id: str
    name: str
    measured: float | None
    target: float
    comparison: str
    status: SloStatus
    detail: str


def _compare(slo: SLO, measured: float) -> bool:
    if slo.comparison == "lt":
        return measured < slo.target
    if slo.comparison == "gte":
        return measured >= slo.target
    if slo.comparison == "eq":
        return measured == slo.target
    return False


def evaluate(snapshot: dict[str, float | None]) -> list[SloResult]:
    """对快照中每个 SLO 取值评估；缺值标 unknown。"""
    results: list[SloResult] = []
    for slo in SLOS:
        measured = snapshot.get(slo.id)
        if measured is None:
            results.append(
                SloResult(
                    id=slo.id,
                    name=slo.name,
                    measured=None,
                    target=slo.target,
                    comparison=slo.comparison,
                    status="unknown",
                    detail="缺少测量值，未评估",
                )
            )
            continue
        ok = _compare(slo, measured)
        results.append(
            SloResult(
                id=slo.id,
                name=slo.name,
                measured=measured,
                target=slo.target,
                comparison=slo.comparison,
                status="pass" if ok else "fail",
                detail=(
                    f"measured={measured}{slo.unit} target={slo.comparison} {slo.target}{slo.unit}"
                ),
            )
        )
    return results


def summarize(results: list[SloResult]) -> dict[str, int]:
    out = {"pass": 0, "fail": 0, "unknown": 0}
    for r in results:
        out[r.status] += 1
    return out


__all__: Final[list[str]] = ["SLO", "SLOS", "SloResult", "SloStatus", "evaluate", "summarize"]
