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
from typing import Any, Optional

from ..core.orchestrator import Orchestrator
from ..core.models import SubTask, SubTaskResult
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
        # 决定本子任务是否走角色增强路径
        role = map_skill_to_role(st.selected_skill) if ROLE_ORCHESTRATOR_ENABLED else None
        agent = get_role_agent(role) if role else None
        if agent is None:
            # 非四角色技能(agent_search/chat/delete 等)→ 原生执行,零改动
            return await super()._run_one(
                st, sink, model_id, base_messages, trace_id, is_cancelled,
                shared_ctx, confirmed_subtasks, **extra_kwargs,
            )

        # 复用父类完整执行(风险门控 + 事件包装 + 上下文补全 + 角色注入已通过 _enrich 生效)
        result = await super()._run_one(
            st, sink, model_id, base_messages, trace_id, is_cancelled,
            shared_ctx, confirmed_subtasks, **extra_kwargs,
        )

        # ── 角色级强交接物捕获 → 注册进 SharedContext(供下游角色按 SOP 消费) ──
        handoff = agent.capture_handoff(result.output_text, result.artifacts)
        if handoff is not None and st.id:
            shared_ctx.register_handoff(st.id, handoff)
            logger.info(
                "[RoleOrchestrator] 捕获交接物 role=%s skill=%s artifact=%s summary=%s",
                agent.label, st.selected_skill, handoff.artifact_type, handoff.summary[:80],
            )

        # ── 角色入参/出参日志(精细日志要求) ──
        agent.log_io(
            st.selected_skill, model_id,
            input_summary=f"sub={st.id} goal={(st.goal or '')[:60]}",
            status=result.status,
            output_summary=(handoff.summary[:120] if handoff else f"len={len(result.output_text)}"),
            duration_ms=result.duration_ms,
        )
        return result
