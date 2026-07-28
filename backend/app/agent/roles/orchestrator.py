"""§4 角色重构 · RoleOrchestrator(方案 B-轻 · 编排层)。

继承 Orchestrator,仅做两处「增强覆盖」,完整执行逻辑(风险门控 / DAG 调度 / 事件包装 /
合并)全部复用父类,保证零破坏:
  1. _enrich():在父类上下文补全之后,按技能映射出 RoleAgent,注入「角色身份 + 上游强交接物」
     (上下文隔离,不 dump 整段聊天历史)。
  2. _run_one():复用父类完整执行拿到 SubTaskResult 后,做角色级强交接物捕获
     (RoleHandoff → SharedContext.handoffs,供下游角色按 SOP 顺序消费) + 角色入参/出参日志。

统计(ai:role:*)统一在 runner.run_skill 记录(单一路径,避免双记);本类不重复记。
开关 ROLE_ORCHESTRATOR_ENABLED=0 时,queue.py 回退使用原生 Orchestrator,本类不被实例化。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ..core.orchestrator import Orchestrator, _cancelled_now
from ..core.models import (
    RISK_HIGH,
    RISK_MEDIUM,
    SUB_BLOCKED,
    SUB_CANCELLED,
    SUB_DONE,
    SUB_FAILED,
    SUB_SKIPPED,
    SubTask,
    SubTaskResult,
)
from ..events import ev
from .handoff import ROLE_ORCHESTRATOR_ENABLED, map_skill_to_role
from .product import ProductAgent
from .design import DesignAgent
from .dev import DevAgent
from .qa import QAAgent

logger = logging.getLogger("ai_service.roles.orchestrator")

# 角色名 → RoleAgent 实例(单例)
_ROLE_AGENTS: dict[str, Any] = {
    "product": ProductAgent(),
    "design": DesignAgent(),
    "dev": DevAgent(),
    "qa": QAAgent(),
}


def get_role_agent(role: Optional[str]) -> Optional[Any]:
    if not role:
        return None
    return _ROLE_AGENTS.get(role)


class RoleOrchestrator(Orchestrator):
    """角色感知的多意图编排器(复用父类 DAG 执行,叠加角色上下文隔离 + 强交接物)。"""

    def _enrich(self, st: SubTask, base_messages: list[dict], shared_ctx: Any) -> list[dict]:
        # 先复用父类上下文补全(子任务聚焦 + 依赖产出)
        enriched = super()._enrich(st, base_messages, shared_ctx)
        if not ROLE_ORCHESTRATOR_ENABLED:
            return enriched
        role = map_skill_to_role(st.selected_skill)
        agent = get_role_agent(role) if role else None
        if agent is None:
            return enriched
        enriched = agent.inject_context(enriched, shared_ctx)
        logger.info("[RoleOrchestrator] 注入角色上下文 role=%s skill=%s", agent.label, st.selected_skill)
        return enriched

    async def _run_one(
        self,
        st: SubTask,
        sink,
        model_id: str,
        base_messages: list[dict],
        trace_id: Optional[str],
        is_cancelled,
        shared_ctx: Any,
        confirmed_subtasks: set,
        **extra_kwargs,
    ) -> SubTaskResult:
        # 决定本子任务是否走角色增强路径(四角色技能)
        role = map_skill_to_role(st.selected_skill) if ROLE_ORCHESTRATOR_ENABLED else None
        agent = get_role_agent(role) if role else None
        if agent is None:
            # 非四角色技能(agent_search/chat/delete 等)→ 原生执行,零改动
            return await super()._run_one(
                st, sink, model_id, base_messages, trace_id, is_cancelled,
                shared_ctx, confirmed_subtasks, **extra_kwargs,
            )

        # §方案B P1: 四角色技能由 RoleAgent.execute() 真正执行(一等执行单元),
        # 执行后产出强 Schema 交接物(RoleHandoff)供下游角色按 SOP 消费。
        t0 = time.time()
        # 1) 风险门控(死红线 HIGH / 需确认 MEDIUM),与原 Orchestrator 一致
        if st.risk_level == RISK_HIGH:
            st.transition(SUB_BLOCKED)
            await sink(ev("subtask_fail", sub_task_id=st.id, reason="高风险操作不予执行(系统拒绝)", recoverable=False))
            return SubTaskResult(id=st.id, status=SUB_BLOCKED, skill=st.selected_skill, goal=st.goal,
                                 error="高风险拦截", risk_level=st.risk_level)
        if st.risk_level == RISK_MEDIUM and st.id not in confirmed_subtasks:
            st.transition(SUB_SKIPPED)
            await sink(ev("subtask_fail", sub_task_id=st.id,
                          reason="中风险操作需用户确认(回复确认后重发)", recoverable=True))
            return SubTaskResult(id=st.id, status=SUB_SKIPPED, skill=st.selected_skill, goal=st.goal,
                                 error="中风险待确认", risk_level=st.risk_level)

        # 2) 上下文补全(子类聚焦 + 依赖产出),与原 Orchestrator._enrich 一致
        enriched = self._enrich(st, base_messages, shared_ctx)

        # 3) 角色真正执行(Planner/Coder/Reviewer 等底层能力),返回强 Schema 交接物
        try:
            handoff = await agent.execute(
                subtask=st, model_id=model_id, messages=enriched,
                shared_ctx=shared_ctx, is_cancelled=is_cancelled, trace_id=trace_id,
                sink=sink, **extra_kwargs,
            )
            if await _cancelled_now(is_cancelled):
                st.transition(SUB_CANCELLED)
                await sink(ev("subtask_fail", sub_task_id=st.id, reason="用户取消", recoverable=True))
                return SubTaskResult(id=st.id, status=SUB_CANCELLED, skill=st.selected_skill, goal=st.goal,
                                     error="用户取消", risk_level=st.risk_level,
                                     duration_ms=int((time.time() - t0) * 1000))
            st.transition(SUB_DONE)
            _artifacts = (handoff.structured or {}).get("artifacts", []) or [] if handoff else []
            _raw = handoff.raw if handoff else ""
            await sink(ev("subtask_done", sub_task_id=st.id,
                          result_summary=_raw[:200], artifacts=_artifacts))
            # 注册强 Schema 交接物(供下游角色按 SOP 顺序消费) + 注册文本产出
            if handoff is not None and st.id:
                shared_ctx.register_handoff(st.id, handoff)
                logger.info(
                    "[RoleOrchestrator] 捕获交接物 role=%s skill=%s artifact=%s summary=%s",
                    agent.label, st.selected_skill, handoff.artifact_type, handoff.summary[:80],
                )
            shared_ctx.register_output(st.id, _raw[:2000])
            agent.log_io(
                st.selected_skill, model_id,
                input_summary=f"sub={st.id} goal={(st.goal or '')[:60]}",
                status="done",
                output_summary=(handoff.summary[:120] if handoff else f"len={len(_raw)}"),
                duration_ms=int((time.time() - t0) * 1000),
            )
            return SubTaskResult(
                id=st.id, status=SUB_DONE, skill=st.selected_skill, goal=st.goal,
                output_text=_raw, artifacts=_artifacts,
                risk_level=st.risk_level, duration_ms=int((time.time() - t0) * 1000),
            )
        except Exception as e:
            logger.error("[RoleOrchestrator] 子任务 %s 执行失败: %s", st.id, e)
            await sink(ev("subtask_fail", sub_task_id=st.id, reason=f"执行异常: {e}", recoverable=True))
            return SubTaskResult(
                id=st.id, status=SUB_FAILED, skill=st.selected_skill, goal=st.goal,
                error=str(e), risk_level=st.risk_level, duration_ms=int((time.time() - t0) * 1000),
            )
