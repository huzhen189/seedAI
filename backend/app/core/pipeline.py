"""唯一的 S0-S9 生命周期编排器。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from .contracts import PIPELINE_STAGES, StageId, StageResult, StageStatus
from .errors import PipelineContractError
from .turn_context import TurnContext


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
        for stage in self._stages:
            result = await stage.run(context)
            self._validate_result(stage.stage_id, result)
            await self._audit_sink.append(result)
            if observer is not None:
                await observer(result)
            results.append(result)
        return tuple(results)

    @staticmethod
    def _validate_result(expected_stage: StageId, result: StageResult) -> None:
        if result.stage is not expected_stage:
            raise PipelineContractError(f"Stage {expected_stage.value} 返回了错误 result.stage={result.stage.value}")
        if expected_stage in {StageId.S0, StageId.S8, StageId.S9} and result.status is StageStatus.SKIPPED:
            raise PipelineContractError(f"已接受 Turn 的 {expected_stage.value} 不得 skipped")
