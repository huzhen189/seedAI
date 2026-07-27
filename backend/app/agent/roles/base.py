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
