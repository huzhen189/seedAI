from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pipeline import AuditSink, Pipeline
from .s0_gateway import S0GatewayStage
from .s1_recall import S1RecallStage
from .s2_understand import S2UnderstandStage
from .s3_dst import S3DstStage
from .s4_classify import S4ClassifyStage
from .s5_validate import S5ValidateStage
from .s6_execute import S6ExecuteStage
from .s7_persist_state import S7PersistStateStage
from .s8_output_guard import S8OutputGuardStage
from .s9_archive import S9ArchiveStage


def build_pipeline(*, audit_sink: AuditSink, session: AsyncSession | None) -> Pipeline:
    return Pipeline(
        (
            S0GatewayStage(session),
            S1RecallStage(session),
            S2UnderstandStage(session),
            S3DstStage(session),
            S4ClassifyStage(session),
            S5ValidateStage(session),
            S6ExecuteStage(session),
            S7PersistStateStage(session),
            S8OutputGuardStage(session),
            S9ArchiveStage(session),
        ),
        audit_sink,
    )


__all__ = ["build_pipeline"]
