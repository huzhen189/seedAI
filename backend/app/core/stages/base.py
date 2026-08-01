from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import ErrorEnvelope, StageId, StageResult, StageStatus
from app.core.turn_context import TurnContext

logger = logging.getLogger("app.core.stages")


class BaseStage:
    """Pipeline 阶段基类(§5.6 / S0-S9)。

    子类声明 ``stage_id`` 并实现 ``run(context)``。``result()`` 统一构造 ``StageResult``,
    并自动计算 ``duration_ms``(端到端耗时,供 SSE 的 stage 事件与性能审计使用)。

    约定：阶段**不得直接抛裸异常**——若执行出错,应在 ``run`` 内捕获并 ``return self.result(
    StageStatus.FAILED/... , "reason_code", error=ErrorEnvelope(...))``,由 Pipeline 统一收口。
    """

    stage_id: StageId

    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session

    def result(
        self,
        status: StageStatus,
        reason_code: str,
        *,
        entered_at: datetime | None = None,
        error: ErrorEnvelope | None = None,
        output_refs: list[str] | None = None,
    ) -> StageResult:
        """构造阶段结果并填写耗时。``duration_ms`` 取自 entered_at / 当前 UTC 之差。"""
        start = entered_at or datetime.now(UTC)
        end = datetime.now(UTC)
        return StageResult(
            stage=self.stage_id,
            status=status,
            reason_code=reason_code,
            entered_at=start,
            left_at=end,
            duration_ms=max(0, int((end - start).total_seconds() * 1000)),
            error=error,
            output_refs=output_refs or [],
        )

    async def run(self, context: TurnContext) -> StageResult:
        """阶段执行入口。子类必须实现,且只返回 StageResult,不抛裸异常。"""
        raise NotImplementedError
