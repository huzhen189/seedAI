"""SLO 评估与告警生命周期测试（M10c）。"""

from __future__ import annotations

from app.security_alerting import AlertManager, LogNotifier
from app.security_slo import SloResult, evaluate, summarize


def _result(slo_id: str, measured, status: str) -> SloResult:
    return SloResult(
        id=slo_id,
        name=slo_id,
        measured=measured,
        target=0.0,
        comparison="lt",
        status=status,  # type: ignore[arg-type]
        detail="",
    )


def test_evaluate_pass_fail_unknown() -> None:
    snap = {"api_p99_ms": 400.0, "w0_pseudo_success": 0.0, "sse_first_event_p99_ms": None}
    results = {r.id: r for r in evaluate(snap)}
    assert results["api_p99_ms"].status == "pass"      # 400 < 500
    assert results["w0_pseudo_success"].status == "pass"  # 0 == 0 (eq)
    assert results["sse_first_event_p99_ms"].status == "unknown"  # 缺值


def test_evaluate_fail_when_over_threshold() -> None:
    results = {r.id: r for r in evaluate({"api_p99_ms": 600.0})}
    assert results["api_p99_ms"].status == "fail"      # 600 >= 500


def test_summarize_counts() -> None:
    results = [
        _result("a", 1.0, "pass"),
        _result("b", 2.0, "fail"),
        _result("c", None, "unknown"),
    ]
    assert summarize(results) == {"pass": 1, "fail": 1, "unknown": 1}


def test_alert_fires_after_sustain_and_recovers() -> None:
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    fired: list = []
    mgr = AlertManager(
        notifier=LogNotifier(), sustain_seconds=300, recover_seconds=300, now=now
    )
    failing = [_result("api_p99_ms", 900.0, "fail")]

    # t=0 开始失败，但不足 5min 不触发
    clock["t"] = 0.0
    assert mgr.update(failing) == []
    assert mgr.active_alerts() == []
    # t=301 超过 sustain，触发 fire
    clock["t"] = 301.0
    events = mgr.update(failing)
    assert len(events) == 1 and events[0].kind == "fire"
    assert mgr.active_alerts() == ["api_p99_ms"]
    # 持续失败不再重复 fire
    clock["t"] = 400.0
    assert mgr.update(failing) == []
    # t=401 恢复(pass)，进入 recovering，不足 recover 窗口不解除
    passing = [_result("api_p99_ms", 100.0, "pass")]
    clock["t"] = 401.0
    assert mgr.update(passing) == []
    assert mgr.active_alerts() == ["api_p99_ms"]
    # t=702 超过 recover 窗口，触发 recover
    clock["t"] = 702.0
    events2 = mgr.update(passing)
    assert len(events2) == 1 and events2[0].kind == "recover"
    assert mgr.active_alerts() == []


def test_alert_ignores_unknown_as_not_failing() -> None:
    clock = {"t": 0.0}
    mgr = AlertManager(sustain_seconds=300, recover_seconds=300, now=lambda: clock["t"])
    unknown = [_result("api_p99_ms", None, "unknown")]
    # unknown 不应触发 fire，也不应误判为恢复。
    clock["t"] = 1000.0
    assert mgr.update(unknown) == []
    assert mgr.active_alerts() == []
