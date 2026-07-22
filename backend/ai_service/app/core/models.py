"""多意图编排共享数据模型(§多意图 v1.0)。

包含:
- SubTask         : 单个子计划(可独立执行的最小单元)
- SplitResult     : 拆分器输出(单意图 or 多意图)
- SharedContext    : 子任务间共享状态(上下文/产物传递)
- SubTaskResult    : 单个子任务执行结果
- OrchestratorResult: 编排器总结果(成功/失败分组 + 合并回复)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# 风险等级常量(与 safety.py 保持一致)
RISK_HIGH = "high"
RISK_MEDIUM = "medium"
RISK_LOW = "low"

# 子任务状态
SUB_PENDING = "pending"
SUB_RUNNING = "running"
SUB_DONE = "done"
SUB_FAILED = "failed"
SUB_BLOCKED = "blocked"
SUB_SKIPPED = "skipped"


@dataclass
class SubTask:
    """一个可独立执行的子计划。

    拆分约束(由 splitter 系统提示强制):
    - 不同目标才拆(原子性原则)
    - 每个子任务必须能独立执行并产出可交付结果
    - 依赖方必须在 context_hint 中声明需要上游的什么产出
    """

    id: str                                  # "sub_0" / "sub_1" ...
    goal: str                                # 该子任务要达成的目标(简述)
    original_text: str = ""                  # 从用户原输入中摘出的对应片段
    level1: str = "learn"                     # 意图一级
    level2: str = "casual"                    # 意图二级
    industry: str = "other"                   # 行业(供 skill 特化)
    selected_skill: str = "explain"          # 已映射到具体 skill(单一来源)
    context_hint: str = ""                    # 该子任务专属上下文(补齐自洽所需)
    risk_level: str = RISK_LOW                # high/medium/low
    dependencies: list[str] = field(default_factory=list)  # 依赖的 sub_task_id
    estimated_tokens: int = 0                # 粗估 token(用于排队/预算)
    status: str = SUB_PENDING                 # pending/running/done/failed/blocked/skipped


@dataclass
class SplitResult:
    """拆分器输出。单意图时 is_multi=False, sub_tasks 仅含 1 个元素。"""

    is_multi: bool = False
    sub_tasks: list[SubTask] = field(default_factory=list)
    split_reason: str = ""                    # 为什么拆 / 为什么不拆
    confidence: float = 0.0                   # 拆分置信度
    strategy: str = "serial"                  # 默认策略(serial/parallel/mixed)


@dataclass
class SharedContext:
    """子任务间共享的可变状态(供依赖方读取上游产出)。"""

    project_status: dict = field(default_factory=dict)
    requirement_doc: Optional[dict] = None
    artifacts: list = field(default_factory=list)      # 已生成文件
    dep_outputs: dict[str, Any] = field(default_factory=dict)  # sub_task_id → 产出摘要
    conversation_summary: str = ""
    conversation_history: list = field(default_factory=list)

    def register_output(self, sub_task_id: str, output: Any) -> None:
        """子任务完成后注册产出,供下游依赖方读取。"""
        self.dep_outputs[sub_task_id] = output

    def get_dep_outputs(self, deps: list[str]) -> str:
        """把依赖方的产出格式化为可注入的上下文文本。"""
        if not deps:
            return ""
        parts = []
        for d in deps:
            out = self.dep_outputs.get(d)
            if out:
                parts.append(f"[前置子任务 {d} 的产出]\n{out}")
        return "\n\n".join(parts)


@dataclass
class SubTaskResult:
    """单个子任务的执行结果。"""

    id: str
    status: str = SUB_DONE                    # done/failed/blocked/skipped
    skill: str = ""
    goal: str = ""
    output_text: str = ""                     # 文本产出(合并用)
    artifacts: list = field(default_factory=list)  # 产物文件
    references: list = field(default_factory=list)  # 引用/链接
    error: str = ""                           # 失败原因
    duration_ms: int = 0
    risk_level: str = RISK_LOW


@dataclass
class OrchestratorResult:
    """编排器总结果。"""

    success_results: list[SubTaskResult] = field(default_factory=list)
    failed_results: list[SubTaskResult] = field(default_factory=list)
    merged_text: str = ""                     # 合并后的连贯中文回复
    strategy: str = "serial"

    @property
    def total(self) -> int:
        return len(self.success_results) + len(self.failed_results)

    @property
    def partial_delivery(self) -> bool:
        """至少 1 成功 + 至少 1 失败 = 部分交付。"""
        return len(self.success_results) > 0 and len(self.failed_results) > 0

    @property
    def total_failure(self) -> bool:
        """全部失败。"""
        return len(self.success_results) == 0 and len(self.failed_results) > 0

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return len(self.success_results) / self.total
