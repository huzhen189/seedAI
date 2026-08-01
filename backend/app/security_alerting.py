"""告警投递与生命周期管理（§15.2 可观测性）。

设计约束：
- 告警持续 5 分钟触发、恢复持续 5 分钟解除（规范 §15.2）。
- Notifier 可插拔：log 默认；webhook 经 ALERT_WEBHOOK_URL 可选；email 留 stub。
- AlertManager 维护每 SLO 的 failing/recovering 时间戳，避免抖动误报。
- 每条告警必须有 runbook 与责任角色（随 SLO 一并携带）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Final, Protocol

from app.security_slo import SloResult

logger = logging.getLogger("app.security.alerting")


class AlertEvent:
    """一次告警状态变化。"""

    def __init__(
        self,
        slo_id: str,
        slo_name: str,
        kind: str,  # "fire" | "recover"
        measured: float | None,
        detail: str,
        runbook: str,
        owner: str,
    ) -> None:
        self.slo_id = slo_id
        self.slo_name = slo_name
        self.kind = kind
        self.measured = measured
        self.detail = detail
        self.runbook = runbook
        self.owner = owner
        self.ts = int(time.time())

    def to_dict(self) -> dict:
        return {
            "slo_id": self.slo_id,
            "slo_name": self.slo_name,
            "kind": self.kind,
            "measured": self.measured,
            "detail": self.detail,
            "runbook": self.runbook,
            "owner": self.owner,
            "ts": self.ts,
        }


class Notifier(Protocol):
    def notify(self, event: AlertEvent) -> None: ...


class LogNotifier:
    """默认 Notifier：结构化日志（含 runbook 与责任角色）。"""

    def notify(self, event: AlertEvent) -> None:
        level = logging.CRITICAL if event.kind == "fire" else logging.INFO
        logger.log(
            level,
            "SLO 告警[%s] %s: slo=%s measured=%s | runbook=%s owner=%s",
            event.kind,
            event.slo_name,
            event.slo_id,
            event.measured,
            event.runbook,
            event.owner,
        )


class WebhookNotifier:
    """可选 Notifier：POST JSON 到 ALERT_WEBHOOK_URL（未配置则降级为 log）。"""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.getenv("ALERT_WEBHOOK_URL", "").strip()

    def notify(self, event: AlertEvent) -> None:
        if not self.url:
            LogNotifier().notify(event)
            return
        try:
            import urllib.request

            data = json.dumps(event.to_dict()).encode("utf-8")
            req = urllib.request.Request(
                self.url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                if resp.status >= 400:
                    logger.warning("告警 webhook 返回 %s", resp.status)
        except Exception as exc:  # pragma: no cover - 投递失败不阻断主流程
            logger.warning("告警 webhook 投递失败: %s", exc)
            LogNotifier().notify(event)


# SLO -> runbook / 责任角色（规范 §15.2：每条告警必须有 runbook 与责任角色）。
_RUNBOOK: Final[dict[str, tuple[str, str]]] = {
    "api_p99_ms": ("runbook/api-latency.md", "Backend"),
    "sse_first_event_p99_ms": ("runbook/sse-latency.md", "Backend"),
    "replay_success_rate": ("runbook/replay.md", "Ops"),
    "deploy_no_break": ("runbook/deploy.md", "Ops"),
    "w0_pseudo_success": ("runbook/finalize.md", "Security"),
}


@dataclass
class _SloState:
    failing_since: float | None = None
    recovering_since: float | None = None
    fired: bool = False


class AlertManager:
    """按 SLO 维护告警生命周期；持续 sustain 秒触发、recover 秒解除。"""

    def __init__(
        self,
        notifier: Notifier | None = None,
        sustain_seconds: float = 300.0,
        recover_seconds: float = 300.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._notifier = notifier or LogNotifier()
        self.sustain = sustain_seconds
        self.recover = recover_seconds
        self._now = now or time.monotonic
        self._states: dict[str, _SloState] = {}
        self._recent: list[AlertEvent] = []

    def _runbook(self, slo_id: str) -> tuple[str, str]:
        return _RUNBOOK.get(slo_id, ("runbook/generic.md", "Ops"))

    def update(self, results: list[SloResult]) -> list[AlertEvent]:
        """根据本轮评估结果推进告警状态，返回本轮产生的 fire/recover 事件。"""
        emitted: list[AlertEvent] = []
        for r in results:
            st = self._states.setdefault(r.id, _SloState())
            runbook, owner = self._runbook(r.id)
            is_failing = r.status == "fail"
            if is_failing:
                st.recovering_since = None
                if st.failing_since is None:
                    st.failing_since = self._now()
                if not st.fired and (self._now() - st.failing_since) >= self.sustain:
                    ev = AlertEvent(r.id, r.name, "fire", r.measured, r.detail, runbook, owner)
                    self._notifier.notify(ev)
                    self._recent.append(ev)
                    emitted.append(ev)
                    st.fired = True
            else:
                # pass / unknown：视为恢复中
                st.failing_since = None
                if st.fired:
                    if st.recovering_since is None:
                        st.recovering_since = self._now()
                    if (self._now() - st.recovering_since) >= self.recover:
                        ev = AlertEvent(
                            r.id, r.name, "recover", r.measured, r.detail, runbook, owner
                        )
                        self._notifier.notify(ev)
                        self._recent.append(ev)
                        emitted.append(ev)
                        st.fired = False
                        st.recovering_since = None
        return emitted

    def active_alerts(self) -> list[str]:
        return [sid for sid, st in self._states.items() if st.fired]


__all__: Final[list[str]] = [
    "AlertEvent",
    "AlertManager",
    "LogNotifier",
    "Notifier",
    "WebhookNotifier",
]
