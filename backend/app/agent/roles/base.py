"""§4 角色重构 · RoleAgent 基类(方案 B-轻)。

每个 RoleAgent 封装一个(或一组)现有 skill,提供:
- system_prompt_fragment():角色身份声明(上下文隔离的身份锚点)。
- inject_context():把「角色身份 + 上游强交接物」注入到消息尾部 system 指令,
  实现上下文隔离(不 dump 整段聊天历史,只给角色该看的)。
- capture_handoff():从技能产出文本/产物中提取强 Schema 交接物(RoleHandoff)。
- log_io():打印该 agent 的入参/出参(满足精细日志要求)。

RoleAgent 不替代 skill handler,只做「上下文隔离 + 交接物捕获 + 日志」的薄封装层,
真正执行仍委托给现有 skill(复用 8 个 skill,守单进程铁律)。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .handoff import ROLE_LABEL, build_handoff, build_upstream_context

logger = logging.getLogger("ai_service.roles.agent")


class RoleAgent:
    """角色 Agent 基类。"""

    # 子类覆盖
    role: str = "unknown"
    owned_skills: list[str] = []

    def __init__(self) -> None:
        self.label = ROLE_LABEL.get(self.role, self.role)

    # ── 角色身份声明(上下文隔离锚点) ──
    def system_prompt_fragment(self) -> str:
        return (
            f"你当前以「{self.label}」身份工作,只负责你职责范围内的产出,"
            f"不要越界承担其他角色的任务。请基于下方上游交付物(若有)专注完成本角色工作。"
        )

    # ── 上下文注入:角色身份 + 上游强交接物 ──
    def inject_context(self, messages: list[dict], shared_ctx: Any) -> list[dict]:
        """在消息尾部追加一条 system 指令:角色身份 + 上游交付物参考。

        上下文隔离要点:只注入『本角色 + 上游 SOP 交付物』,不把整段聊天无差别透传。
        """
        upstream = build_upstream_context(self.role, shared_ctx)
        block = self.system_prompt_fragment()
        if upstream:
            block += f"\n\n## 上游交付物(供你参考,不要重复生成)\n{upstream}\n"
        if not block.strip():
            return messages
        return messages + [{"role": "system", "content": block}]

    # ── 交接物捕获(由子类按产出形态重写) ──
    def capture_handoff(self, output_text: str, artifacts: Optional[list] = None) -> Any:
        return build_handoff(self.role, self.owned_skills[0] if self.owned_skills else "?", output_text, artifacts)

    # ── 入参/出参日志(精细日志要求) ──
    def log_io(
        self,
        skill: str,
        model_id: str,
        input_summary: str,
        status: str,
        output_summary: str,
        duration_ms: int,
    ) -> None:
        logger.info(
            "[角色:%s] skill=%s model=%s status=%s 入参=%s 出参=%s 耗时=%dms",
            self.label, skill, model_id, status, input_summary, output_summary, duration_ms,
        )

    # ── §方案B P1: 一等执行单元 ──
    async def execute(
        self,
        *,
        subtask: Any,
        model_id: str,
        messages: list[dict],
        shared_ctx: Any,
        is_cancelled: Any = None,
        trace_id: Optional[str] = None,
        sink: Any = None,
        **extra_kwargs,
    ) -> Any:
        """真正执行本角色对应的底层技能,流式转发事件,并返回强 Schema 交接物(RoleHandoff)。

        这是方案 B 中「角色成为一等执行单元」的核心:不再只是注入身份提示,
        而是实际调用 owned skill 干活,并把产出封装为 RoleHandoff 供下游角色按 SOP 消费。
        (run_skill 惰性导入,避免与 core.runner 形成模块加载环。)
        """
        from ..core.runner import run_skill
        from ..events import ev

        out_buf: list[str] = []
        artifacts: list[str] = []
        proj_status = "draft"
        if isinstance(getattr(shared_ctx, "project_status", None), dict):
            proj_status = shared_ctx.project_status.get("status", "draft")
        intent_info = {
            "level1": getattr(subtask, "level1", None),
            "level2": getattr(subtask, "level2", None),
            "confidence": 0.9,
            "industry": getattr(subtask, "industry", None),
            "decision": "route",
            "selected_skill": subtask.selected_skill,
            "risk_level": subtask.risk_level,
        }
        async for item in run_skill(
            subtask.selected_skill, model_id, messages,
            trace_id=trace_id, is_cancelled=is_cancelled, intent_info=intent_info,
            requirement_doc=getattr(shared_ctx, "requirement_doc", None),
            project_status=proj_status,
            conversation_summary=getattr(shared_ctx, "conversation_summary", None),
            **extra_kwargs,  # 透传 project_system_prompt / project_constraints 等(与单意图路径一致)
        ):
            ev_name = item.get("event")
            if ev_name in ("intent", "done"):
                continue
            item.setdefault("sub_task_id", subtask.id)
            if sink is not None:
                await sink(item)
            if ev_name == "token":
                d = item.get("data", "")
                if isinstance(d, str):
                    out_buf.append(d)
            if ev_name == "preview" and isinstance(item.get("data"), dict):
                u = item["data"].get("url")
                if u:
                    artifacts.append(u)
        handoff = self.capture_handoff("".join(out_buf), artifacts)
        if handoff is not None:
            handoff.structured = {**(handoff.structured or {}), "artifacts": artifacts}
        return handoff
