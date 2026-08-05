"""唯一的 S0-S9 生命周期编排器。"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, cast

from pydantic import BaseModel

from .contracts import PIPELINE_STAGES, StageId, StageResult, StageStatus
from .errors import PipelineContractError
from .turn_context import TurnContext

logger = logging.getLogger("app.core.pipeline")

# IO 日志截断上限。调试阶段放开为「尽量看全」：SIR/plan/向量召回内容不再被切没。
# 上限仍保留以防 reply_final 等超长字段把日志打爆，但足够覆盖正常 SIR 规模。
_IO_MAX_STR = 4000
_IO_MAX_LIST_SAMPLE = 20


def _log_safe(obj: object, max_str: int = _IO_MAX_STR, max_list: int = _IO_MAX_LIST_SAMPLE) -> object:
    """把任意 context 状态递归转成「日志安全 + 截断」的纯 JSON 结构。

    - Pydantic 模型：``model_copy(deep=True)`` 后再 ``model_dump(mode="json")``，
      既与原始引用解耦（前后快照不串味），又保证 datetime/Enum 可序列化。
    - 字符串：超过 ``max_str`` 截断并标注剩余长度。
    - list/tuple：输出 ``{__len__, __sample__}``，只保留前 ``max_list`` 个样本。
    """
    if isinstance(obj, BaseModel):
        return _log_safe(obj.model_dump(mode="json"), max_str, max_list)
    if isinstance(obj, dict):
        return {k: _log_safe(v, max_str, max_list) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        items = list(obj)
        return {
            "__len__": len(items),
            "__sample__": [_log_safe(x, max_str, max_list) for x in items[:max_list]],
        }
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else f"{obj[:max_str]}[+{len(obj) - max_str} chars]"
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj
    return _log_safe(str(obj), max_str, max_list)


class Stage(Protocol):
    stage_id: StageId

    async def run(self, context: TurnContext) -> StageResult: ...


class AuditSink(Protocol):
    async def append(self, result: StageResult) -> None: ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.results: list[StageResult] = []

    async def append(self, result: StageResult) -> None:
        self.results.append(result)


StageObserver = Callable[[StageResult], Awaitable[None]]


class Pipeline:
    def __init__(self, stages: Sequence[Stage], audit_sink: AuditSink) -> None:
        received = tuple(stage.stage_id for stage in stages)
        if received != PIPELINE_STAGES:
            raise PipelineContractError(f"Pipeline stages 必须严格按 S0-S9 各出现一次，实际为 {[stage.value for stage in received]}")
        self._stages = tuple(stages)
        self._audit_sink = audit_sink

    async def run(self, context: TurnContext, observer: StageObserver | None = None) -> tuple[StageResult, ...]:
        results: list[StageResult] = []
        logger.info("[pipeline] 开始执行 turn=%s 共 %d 阶段", context.turn_id, len(self._stages))
        for stage in self._stages:
            intents = ",".join(
                i.intent_id for i in (context.understanding.resolved_intents if context.understanding else [])
            ) or "-"
            logger.info(
                "[pipeline] ▶ 进入 %s turn=%s msg=%.40s intents=%s",
                stage.stage_id.value, context.turn_id, context.clean_message, intents,
            )
            # 进入阶段即发一条 running 事件, 让前端 StageRail 实时显示"进行中"+友好文案。
            # 修复此前只发结束事件、阶段从 pending 直跳 completed、用户体感"卡死无反馈"的问题。
            if observer is not None:
                await observer(StageResult(stage=stage.stage_id, status=StageStatus.RUNNING, reason_code="enter"))
            # 节点进入前快照（run 前取，确保不被 stage 原地修改污染）。
            in_io = cast(dict[str, Any], _log_safe(context.snapshot_state()))
            # 真实阶段耗时: base.py 的 result() 因未收到 entered_at 会把 duration_ms 算成 ~0,
            # 这里在 run 前后用单调时钟实测并回填, 供 SSE stage 事件 / 统计(record_gen_stage) 取真实耗时。
            t0 = time.perf_counter()
            result = await stage.run(context)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            result = result.model_copy(update={"duration_ms": elapsed_ms})
            # 节点完成后快照 + 变更字段 diff。
            out_io = cast(dict[str, Any], _log_safe(context.snapshot_state()))
            changed = sorted(
                k for k in (set(in_io) | set(out_io)) if in_io.get(k) != out_io.get(k)
            )
            # 日志体量与可读性：S1 / S3 是 SIR 状态的两个关键拐点（加载基态 / 合并结果），
            # 完整打印 IN/OUT 便于回放；其余阶段只打印「本次新增/修改了哪些字段 + 新值」，
            # 避免每个阶段都刷一遍完整 SIR 结构体（调试期日志噪音极大）。
            if result.stage in (StageId.S1, StageId.S3):
                logger.info(
                    "[pipeline.io] %s turn=%s | changed=%s | IN=%s | OUT=%s",
                    result.stage.value, context.turn_id, changed,
                    json.dumps(in_io, ensure_ascii=False),
                    json.dumps(out_io, ensure_ascii=False),
                )
            else:
                # 精简：只打出发生变化的字段名与其新值（changed 已含字段名，这里附新值）。
                changed_kv = {k: out_io.get(k) for k in changed}
                logger.info(
                    "[pipeline.io] %s turn=%s | 变更字段=%s | %s",
                    result.stage.value, context.turn_id, changed,
                    json.dumps(changed_kv, ensure_ascii=False),
                )
            logger.info(
                "[pipeline] ◀ %s -> status=%s reason=%s duration=%dms turn=%s",
                result.stage.value, result.status.value, result.reason_code,
                result.duration_ms, context.turn_id,
            )
            self._validate_result(stage.stage_id, result)
            # 审计副本额外携带 IN/OUT/changed 快照(wire 侧 exclude, 只给 AuditSink 落库)。
            # 用 model_copy 而非原地改, 保证 observer/返回给调用方的 result 保持纯净轻量。
            await self._audit_sink.append(
                result.model_copy(update={"io_in": in_io, "io_out": out_io, "io_changed": changed})
            )
            if observer is not None:
                await observer(result)
            results.append(result)
        status_counts = Counter(r.status.value for r in results)
        logger.info(
            "[pipeline] 执行结束 turn=%s 阶段=%d 状态分布=%s",
            context.turn_id, len(results), dict(status_counts),
        )
        return tuple(results)

    @staticmethod
    def _validate_result(expected_stage: StageId, result: StageResult) -> None:
        if result.stage is not expected_stage:
            raise PipelineContractError(f"Stage {expected_stage.value} 返回了错误 result.stage={result.stage.value}")
        if expected_stage in {StageId.S0, StageId.S8, StageId.S9} and result.status is StageStatus.SKIPPED:
            raise PipelineContractError(f"已接受 Turn 的 {expected_stage.value} 不得 skipped")
