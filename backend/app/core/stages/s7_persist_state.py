from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

from app.config import settings
from app.core.contracts import MemoryDecision, StageId, StageStatus
from app.core.memory_write import persist_and_extract
from app.core.turn_context import TurnContext
from app.db.repositories import (
    conversations as conversation_repo,
    sir_snapshots as sir_repo,
)
from .base import BaseStage


class S7PersistStateStage(BaseStage):
    """S7 状态固化(§5.6) + 记忆写入主链路派发（统一对所有 turn 跑一次）。

    把 DST 后的 SIR 置为最终态(``sir_final``)，并**对每一个 turn 统一派发一次**
    「总结 + 聊天级 QC + 记忆点提取落库」（异步、fail-soft、不在 token 流内）：
    不论意图类型（闲聊/建站/检索…）、不论意图数量，S7 都只做一次；QC 与记忆提取
    共享一次 LLM 调用(``llm_extract``)，绝不反噬主链路。

    与旧版差异：去掉了「S5 校验未通过(pass 以外)就跳过固化/记忆」的限制——
    按需求「不管意图都走一次」。仅在「开关关闭」或「用户/助手文本皆空」时跳过。
    见 docs/plan-memory-v2-landing.md §2。
    """

    stage_id = StageId.S7

    async def run(self, context: TurnContext):
        context.sir_final = context.sir_after_dst

        # 会话级「当前 SIR」指针回写：S7 是唯一状态固化点，指针指向的就是下一轮基态。
        snapshot_id = await self._commit_canonical_sir(context)

        # 统一派发「总结 + QC + 记忆」任务（所有 turn 一次，fail-soft、后台异步）。
        self._dispatch_memory_write(context)
        task = context.sir_final.task
        logger.info(
            "[S7] 规范状态就绪 turn=%s canonical_snapshot=%s task=%s phase=%s (总结+QC+记忆 已派发)",
            context.turn_id, snapshot_id,
            task.id if task else None,
            task.phase.value if task else "idle",
        )
        context.memory_decision = MemoryDecision(status="persisted", reason_codes=["async_extraction_dispatched"])
        return self.result(
            StageStatus.COMPLETED,
            "canonical_state_ready",
            output_refs=[f"canonical_sir:{snapshot_id}"] if snapshot_id else [],
        )

    async def _commit_canonical_sir(self, context: TurnContext) -> int | None:
        """把本轮终态 SIR 固化，并让会话指针指向它。

        为什么必须在 S7 做（而不是 S3 或 S5 各自落）：
          - S3 只在"槽位有变化"时落快照，纯追问轮（用户只回一句、没抽到槽）
            压根不落 —— 但那一轮的 ``agenda``/``task.phase`` 恰恰变了；
          - S5 旧实现自己又落一条，同一轮出现两条 base 快照，
            "latest_for_conversation 取最新一条"这个语义随之失去意义。
        现在统一：**终态由 S7 落一次，指针只认这一条。**
        无变化时复用 S3 已落的快照 id，不重复写。
        """
        if self.session is None:
            return None
        conversation_id = context.session.conversation_id
        if not conversation_id:
            return None
        snapshot_id = context.sir_after_dst_snapshot_id
        try:
            # S3 未落（本轮无槽位变化）但状态机有推进 → 补落一条终态快照。
            if snapshot_id is None:
                snap = await sir_repo.insert(
                    self.session,
                    conversation_id=conversation_id,
                    turn_id=context.turn_id,
                    kind="base",
                    snapshot=context.sir_final.model_dump(mode="json"),
                    prev_snapshot_id=context.sir_base_snapshot_id,
                )
                snapshot_id = snap.id
                context.sir_after_dst_snapshot_id = snap.id
            await conversation_repo.touch_current_sir(
                self.session, conversation_id, snapshot_id
            )
            return snapshot_id
        except Exception as exc:  # noqa: BLE001 — 固化失败不得中断回复主链路
            logger.warning("[S7] 固化 canonical SIR 失败(非致命): %s", exc, exc_info=True)
            return None

    def _dispatch_memory_write(self, context: TurnContext) -> None:
        if not getattr(settings, "memory_extraction_enabled", True):
            logger.debug("[S7] 记忆提取开关关闭，跳过 turn=%s", context.turn_id)
            return
        user_text = (context.clean_message or "").strip()
        assistant_text = context.reply_final or "".join(
            f.text for f in context.response_fragments if f.status == "success"
        )
        if not user_text and not assistant_text:
            return
        user_id = int(context.user.user_id)
        project_id = int(context.session.project_id) if context.session.project_id else None
        conversation_id = int(context.session.conversation_id) if context.session.conversation_id else None
        # 批次 B：把状态机本轮确定性产出的 memory_hints 一并喂给记忆写入，
        # 让「承接 lineage / 网站类型偏好」这类结构化信号确定性落库（不靠 LLM 是否听懂）。
        # 只取每轮新鲜的 round_plan.memory_hints，不取 SirState 累积版（避免跨轮重放）。
        hints = None
        round_plan = getattr(context, "round_plan", None)
        if round_plan is not None and getattr(round_plan, "memory_hints", None):
            hints = round_plan.memory_hints
        asyncio.create_task(
            persist_and_extract(
                user_id=user_id,
                project_id=project_id,
                conversation_id=conversation_id,
                user_text=user_text,
                assistant_text=assistant_text,
                trace_id=context.trace_id,  # turn_id == trace_id，供 QC 落库关联
                hints=hints,
            )
        )


__all__ = ["S7PersistStateStage"]
