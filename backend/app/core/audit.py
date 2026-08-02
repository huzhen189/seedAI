"""审计落库：把 S0-S9 每个阶段的 IN/OUT 快照持久化到 ``trace_events``。

在此之前，阶段链路只存在于 ``app.log`` 的 ``[pipeline.io]`` 文本行里——管理后台
「回放」点开一条记录只能看到 messages，想看链路必须去翻日志。本模块把同一份快照
写进可查询的 ``trace_events`` 表，让回放详情能直接还原 S0→S9 的完整过程。

设计取舍：
- **缓冲后一次性 flush**：每阶段单独开事务写库 = 10 次云 MySQL 往返，代价太高；
  改为内存缓冲，Turn 结束（成功或失败）时一次性批量写入。
- **独立事务**：不复用 pipeline 的业务 session。业务事务回滚时，恰恰最需要保留
  失败现场的链路，二者生命周期必须解耦。
- **fail-soft**：审计写失败只记日志，绝不影响用户这一轮对话。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.db.repositories.trace_events import trace_event_repo
from app.db.session import transaction

from .contracts import StageResult
from .pipeline import _log_safe

logger = logging.getLogger("app.core.audit")

# MySQL TEXT 上限 65535 字节，留足余量；超限时逐级收紧字符串截断长度。
_PAYLOAD_MAX_BYTES = 48_000
_SHRINK_STEPS: tuple[int, ...] = (400, 160, 60)


def _payload_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _fit_payload(payload: dict[str, Any]) -> str:
    """把事件 payload 序列化成不超过 TEXT 容量的 JSON 文本。

    逐级降级：原样 → 字符串截断到 400/160/60 → 丢弃 IO 快照只留元信息。
    """
    text = json.dumps(payload, ensure_ascii=False)
    if _payload_bytes(text) <= _PAYLOAD_MAX_BYTES:
        return text
    for max_str in _SHRINK_STEPS:
        shrunk = dict(payload)
        if "io_in" in shrunk:
            shrunk["io_in"] = _log_safe(shrunk["io_in"], max_str, 2)
        if "io_out" in shrunk:
            shrunk["io_out"] = _log_safe(shrunk["io_out"], max_str, 2)
        shrunk["io_truncated_to"] = max_str
        text = json.dumps(shrunk, ensure_ascii=False)
        if _payload_bytes(text) <= _PAYLOAD_MAX_BYTES:
            return text
    minimal = {k: v for k, v in payload.items() if k not in {"io_in", "io_out"}}
    minimal["io_dropped"] = True
    return json.dumps(minimal, ensure_ascii=False)


def _stage_payload(result: StageResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "reason_code": result.reason_code,
        "duration_ms": result.duration_ms,
        "entered_at": result.entered_at.isoformat(),
        "left_at": result.left_at.isoformat(),
        "output_refs": list(result.output_refs),
        "error": result.error.model_dump(mode="json") if result.error else None,
        "changed": list(result.io_changed or []),
        "io_in": result.io_in,
        "io_out": result.io_out,
    }


class DbAuditSink:
    """把阶段链路缓冲在内存，Turn 收尾时批量落 ``trace_events``。"""

    def __init__(self, *, trace_id: str, turn_id: str) -> None:
        self.trace_id = trace_id
        self.turn_id = turn_id
        # 与 InMemoryAuditSink 行为兼容：调用方仍可读 .results
        self.results: list[StageResult] = []
        self._pending: list[dict[str, Any]] = []
        self._seq = 0

    # ── 采集 ──────────────────────────────────────────────────────────────
    def add_event(self, event_type: str, payload: dict[str, Any], *, stage: str | None = None) -> None:
        """记录一条非阶段事件（turn_start / turn_end / error）。"""
        self._seq += 1
        self._pending.append(
            {
                "trace_id": self.trace_id,
                "seq": self._seq,
                "event_type": event_type[:16],
                "stage": stage[:32] if stage else None,
                "payload": _fit_payload(payload),
            }
        )

    async def append(self, result: StageResult) -> None:
        """AuditSink 协议实现：pipeline 每完成一个阶段调用一次。"""
        self.results.append(result)
        self.add_event("stage", _stage_payload(result), stage=result.stage.value)

    # ── 落库 ──────────────────────────────────────────────────────────────
    async def flush(self) -> int:
        """把缓冲事件一次性写库。返回写入条数；失败返回 0 且不抛异常。"""
        if not self._pending:
            return 0
        batch, self._pending = self._pending, []
        try:
            async with transaction() as session:
                for row in batch:
                    await trace_event_repo.create(session, **row)
            logger.info(
                "[audit] trace_events 落库成功 trace=%s turn=%s 事件=%d",
                self.trace_id, self.turn_id, len(batch),
            )
            return len(batch)
        except Exception as exc:  # noqa: BLE001 - 审计失败不得影响主链路
            logger.exception(
                "[audit] trace_events 落库失败 trace=%s turn=%s 事件=%d: %s",
                self.trace_id, self.turn_id, len(batch), exc,
            )
            return 0


__all__ = ["DbAuditSink"]
