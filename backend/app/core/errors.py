"""Pipeline 的结构化错误类型。"""

from __future__ import annotations

from .contracts import ErrorEnvelope, StageId


class PipelineContractError(RuntimeError):
    """Stage 返回了不满足十阶段契约的结果。"""


class StageExecutionError(RuntimeError):
    """保留安全错误信息的 Stage 执行异常。"""

    def __init__(self, stage: StageId, error: ErrorEnvelope) -> None:
        super().__init__(f"{stage.value} 执行失败: {error.code}")
        self.stage = stage
        self.error = error
