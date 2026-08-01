from __future__ import annotations

import asyncio

import pytest

from app.core.contracts import PIPELINE_STAGES, StageId, StageResult, StageStatus, SessionInfo, UserIdentity
from app.core.errors import PipelineContractError
from app.core.ids import new_ulid
from app.core.pipeline import InMemoryAuditSink, Pipeline, build_m2_skeleton_pipeline
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


def test_skeleton_pipeline_runs_exactly_s0_to_s9_without_side_effects() -> None:
    async def scenario() -> None:
        audit = InMemoryAuditSink()
        context = make_context()
        results = await build_m2_skeleton_pipeline(audit).run(context)

        assert tuple(result.stage for result in results) == PIPELINE_STAGES
        assert tuple(result.status for result in results) == (
            StageStatus.COMPLETED,
            StageStatus.NO_OP,
            StageStatus.NO_OP,
            StageStatus.NO_OP,
            StageStatus.NO_OP,
            StageStatus.NO_OP,
            StageStatus.NO_OP,
            StageStatus.NO_OP,
            StageStatus.COMPLETED,
            StageStatus.COMPLETED,
        )
        assert audit.results == list(results)
        assert context.execution is None
        assert context.reply_final == ""

    asyncio.run(scenario())


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
