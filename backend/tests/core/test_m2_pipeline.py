from __future__ import annotations

import asyncio

import pytest

from app.core.contracts import PIPELINE_STAGES, StageId, StageResult, StageStatus, SessionInfo, UserIdentity
from app.core.errors import PipelineContractError
from app.core.ids import new_ulid
from app.core.pipeline import InMemoryAuditSink, Pipeline
from app.core.turn_context import TurnContext
from app.core.stages import (
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


def make_context() -> TurnContext:
    return TurnContext(
        schema_version="1.0",
        trace_id="m2-trace",
        stream_id=new_ulid(),
        turn_id=new_ulid(),
        client_msg_id="client-message-1",
        run_epoch=0,
        fencing_token="fence-0",
        user=UserIdentity(user_id=1),
        session=SessionInfo(conversation_id=1, project_id=1),
        clean_message="请创建一个网站",
    )


def test_pipeline_has_exactly_s0_to_s9_in_order() -> None:
    """规范: 十阶段 Pipeline(S0-S9) 必须存在且按序编排, 不再有 skeleton NO_OP 阶段。

    真实执行需要数据库会话(由 API 层在请求内注入), 故此处只校验结构契约:
    构造不依赖 DB, 且阶段数=10、顺序严格 S0..S9。端到端执行由 live smoke 覆盖。
    """
    from app.core.stages import build_pipeline

    pipeline = build_pipeline(audit_sink=InMemoryAuditSink(), session=None)
    stage_ids = [s.stage_id for s in pipeline._stages]  # noqa: SLF001 - 结构校验
    assert stage_ids == list(PIPELINE_STAGES)
    assert len(stage_ids) == 10
    assert [s.value for s in stage_ids] == [
        "S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9",
    ]


def test_pipeline_rejects_reordered_stages() -> None:
    with pytest.raises(PipelineContractError, match="S0-S9"):
        Pipeline(
            (
                S1RecallStage(),
                S0GatewayStage(),
                S2UnderstandStage(),
                S3DstStage(),
                S4ClassifyStage(),
                S5ValidateStage(),
                S6ExecuteStage(),
                S7PersistStateStage(),
                S8OutputGuardStage(),
                S9ArchiveStage(),
            ),
            InMemoryAuditSink(),
        )


def test_pipeline_rejects_skipped_s0_s8_or_s9() -> None:
    class InvalidS0:
        stage_id = StageId.S0

        async def run(self, context: TurnContext) -> StageResult:
            del context
            return StageResult(stage=StageId.S0, status=StageStatus.SKIPPED, reason_code="invalid")

    async def scenario() -> None:
        pipeline = Pipeline(
            (
                InvalidS0(),
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
            InMemoryAuditSink(),
        )
        with pytest.raises(PipelineContractError, match="S0"):
            await pipeline.run(make_context())

    asyncio.run(scenario())
