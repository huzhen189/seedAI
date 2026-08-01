"""M2 十阶段编排骨架。

该模块只负责编排、顺序校验与审计委派，不包含模型、Redis、Chroma、COS 或领域写入。
后续各 Stage 通过契约逐步替换同名 skeleton，而不是创建第二条执行链。
"""

from __future__ import annotations

from collections.abc import Sequence
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
    """M2 事件模拟器和单元测试使用的非持久审计 sink。"""

    def __init__(self) -> None:
        self.results: list[StageResult] = []

    async def append(self, result: StageResult) -> None:
        self.results.append(result)


class Pipeline:
    """唯一的 S0-S9 生命周期编排器。"""

    def __init__(self, stages: Sequence[Stage], audit_sink: AuditSink) -> None:
        received = tuple(stage.stage_id for stage in stages)
        if received != PIPELINE_STAGES:
            raise PipelineContractError(
                "Pipeline stages 必须严格按 S0-S9 各出现一次；"
                f"实际为 {[stage.value for stage in received]}"
            )
        self._stages = tuple(stages)
        self._audit_sink = audit_sink

    async def run(self, context: TurnContext) -> tuple[StageResult, ...]:
        results: list[StageResult] = []
        for stage in self._stages:
            result = await stage.run(context)
            self._validate_result(stage.stage_id, result)
            await self._audit_sink.append(result)
            results.append(result)
        return tuple(results)

    @staticmethod
    def _validate_result(expected_stage: StageId, result: StageResult) -> None:
        if result.stage is not expected_stage:
            raise PipelineContractError(
                f"Stage {expected_stage.value} 返回了错误的 result.stage={result.stage.value}"
            )
        if expected_stage in {StageId.S0, StageId.S8, StageId.S9} and result.status is StageStatus.SKIPPED:
            raise PipelineContractError(f"已接受 Turn 的 {expected_stage.value} 不得 skipped")


def build_m2_skeleton_pipeline(audit_sink: AuditSink) -> Pipeline:
    """构造当前唯一可运行的十阶段空跑 Pipeline。"""
    from .stages import (
        S0GatewayStage,
        S1RecallStage,
        S2UnderstandStage,
        S3DstStage,
        S4ClassifyStage,
        S5ValidateStage,
        S6ExecuteStage,
        S7PersistStateStage,
        S8OutputGuardStage,
        S9ArchiveStage,
    )

    return Pipeline(
        stages=(
            S0GatewayStage(),
            S1RecallStage(),
            S2UnderstandStage(),
            S3DstStage(),
            S4ClassifyStage(),
            S5ValidateStage(),
            S6ExecuteStage(),
            S7PersistStateStage(),
            S8OutputGuardStage(),
            S9ArchiveStage(),
        ),
        audit_sink=audit_sink,
    )
