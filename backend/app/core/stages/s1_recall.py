from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

from sqlalchemy import select

from app.config import settings
from app.core.contracts import Domain, RecallResult, SirState, StageId, StageStatus
from app.core.turn_context import TurnContext
from app.db.repositories import sir_snapshots as sir_repo
from app.models import Artifact
from app.ragstore import retrieve as _rag_retrieve
from .base import BaseStage


class S1RecallStage(BaseStage):
    """S1 召回(§5.6)。

    生产级实现：
      - **加载 SIR 基态**：从 ``sir_snapshots`` 取最新 base 快照写入 ``context.sir_base``，
        让 S3 的 DST 合并真正有"基态"可合并。回溯控制时改取 **prior_turn 那一刻的快照**
        （语义 = 回滚到上一轮结束时的状态再重写），而非会话最新快照。
      - **加载回溯上下文**：prior_turn 的执行域与产物 project，写入 context 供 S2/S4/S6
        做域继承与产物锁定（修改指令常不含域触发词，不继承就会被降级成闲聊）。
      - 任何加载失败都不阻断主链路，降级为空基态并在 recall 上标注 degraded。
    """

    stage_id = StageId.S1

    async def run(self, context: TurnContext):
        if self.session is None:
            context.recall = RecallResult(status="skipped")
            return self.result(StageStatus.SKIPPED, "recall_no_session")

        degraded: str | None = None
        hit = False

        # 1) 回溯上下文：先定位 prior_turn 的域与产物（决定后续基态取哪一份）。
        if context.prior_turn_id is not None:
            try:
                await self._load_retro_context(context)
                hit = True
            except Exception as exc:  # noqa: BLE001 — 召回失败不得中断主链路
                logger.warning("[S1] 回溯上下文加载失败: %s", exc, exc_info=True)
                degraded = "retro_context_failed"

        # 2) SIR 基态。回溯轮回滚到 prior_turn 的快照；常规轮取会话最新。
        try:
            snap = None
            if context.prior_turn_id is not None:
                snap = await sir_repo.latest_for_turn(self.session, context.prior_turn_id)
            if snap is None:
                snap = await sir_repo.latest_for_conversation(self.session, context.session.conversation_id)
            if snap is not None and isinstance(snap.snapshot, dict):
                context.sir_base = SirState.model_validate(snap.snapshot)
                context.sir_base_snapshot_id = snap.id
                hit = True
                logger.info(
                    "[S1] 加载 SIR 基态 snapshot=%s slots=%d turn=%s retro=%s\n  slots内容=%s\n  constraints=%s\n  pending=%s",
                    snap.id, len(context.sir_base.slots), context.turn_id, context.prior_turn_id,
                    context.sir_base.slots, context.sir_base.constraints, context.sir_base.pending,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[S1] 加载 SIR 基态失败，使用空基态: %s", exc, exc_info=True)
            degraded = "sir_base_load_failed"

        if degraded:
            context.recall = RecallResult(status="degraded", degradation_reason=degraded)
            return self.result(StageStatus.COMPLETED, "recall_degraded")

        # 3) 向量上下文召回
        #    - 项目/会话上下文：需 hit(对话有历史/基态)才查，避免无谓嵌入/查询。
        #    - 用户偏好：按 user_id 隔离，**每轮都查**（与对话是否有历史无关），
        #      支撑「每请求前取用户偏好填充」个性化，新会话也不丢失已知偏好。
        vector_refs: list[str] = []
        if hit:
            try:
                vector_refs = await self._load_vector_context(context)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[S1] 向量上下文召回失败(忽略): %s", exc, exc_info=True)
        user_refs: list[str] = []
        try:
            user_refs = await self._load_user_preferences(context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[S1] 用户偏好召回失败(忽略): %s", exc, exc_info=True)

        if hit or vector_refs or user_refs:
            refs = [str(context.sir_base_snapshot_id)] if context.sir_base_snapshot_id else []
            refs.extend(vector_refs)
            refs.extend(user_refs)
            context.recall = RecallResult(status="hit", references=refs)
            return self.result(StageStatus.COMPLETED, "recall_hit")
        context.recall = RecallResult(status="empty")
        return self.result(StageStatus.SKIPPED, "recall_gate_no_signal")

    async def _load_retro_context(self, context: TurnContext) -> None:
        """定位上一轮的执行域与产出 project，供 S2/S4 域继承、S6 产物锁定。

        判定依据是**事实产物**而非文本：上一轮若产出过 Artifact，则其域必为 SITE，
        且 project 就是该 Artifact 的 project——比任何关键词猜测都可靠。
        """
        assert self.session is not None
        artifact = (
            await self.session.execute(
                select(Artifact)
                .where(Artifact.trace_id == context.prior_turn_id)
                .order_by(Artifact.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if artifact is not None:
            context.prior_domain = Domain.SITE
            context.prior_artifact_id = artifact.id
            context.prior_project_id = artifact.project_id
            logger.info(
                "[S1] 回溯锁定上一轮产物 turn=%s artifact=%s project=%s v?",
                context.prior_turn_id, artifact.id, artifact.project_id,
            )
            return
        logger.info("[S1] 上一轮无产物，回溯降级为普通追问 turn=%s", context.prior_turn_id)

    async def _load_vector_context(self, context: TurnContext) -> list[str]:
        """召回项目/会话向量上下文（fail-soft），填充 ``project_context`` 并返回引用。

        仅在对话有历史(hit)时调用，避免无谓的嵌入/查询。``project_context`` 此前是
        死字段（填充后未消费），现在随 user_context 一起接入 prompt 才真正生效。
        """
        refs: list[str] = []
        query = (context.clean_message or "").strip()
        if not query:
            return refs
        for coll in (settings.chroma_collection_project_memory,
                     settings.chroma_collection_conversation_context):
            try:
                hits = await _rag_retrieve(coll, query, top_k=2)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[S1] 向量召回 %s 失败: %s", coll, exc)
                continue
            for h in hits:
                context.project_context.append(f"[{coll}] {h.text}")
                refs.append(f"{coll}:{h.id}")
                logger.info(
                    "[S1] 向量召回(项目/会话) coll=%s id=%s dist=%.3f\n  内容: %s",
                    coll, h.id, getattr(h, "distance", None), h.text,
                )
        if refs:
            logger.info("[S1] 向量召回 %d 条项目/会话上下文", len(refs))
        return refs

    async def _load_user_preferences(self, context: TurnContext) -> list[str]:
        """按 ``user_id`` 召回用户偏好/属性（fail-soft），填充 ``user_context`` 并返回引用。

        **每轮都调用**（与对话是否有历史无关）：用户偏好是跨会话的持久状态，新开对话
        也不应丢失已知偏好。复用同一 ``query`` 嵌入，零额外 embedding 成本。召回结果由
        chat.respond() 注入 system prompt 实现「每请求前取信息填充」个性化。
        """
        refs: list[str] = []
        query = (context.clean_message or "").strip()
        if not query:
            return refs
        try:
            hits = await _rag_retrieve(
                settings.chroma_collection_user_preferences,
                query, top_k=3,
                where={"user_id": context.user.user_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[S1] 用户偏好召回失败: %s", exc)
            return refs
        for h in hits:
            context.user_context.append(f"[user_preferences] {h.text}")
            refs.append(f"user_preferences:{h.id}")
            logger.info(
                "[S1] 用户偏好召回 id=%s dist=%.3f\n  内容: %s",
                h.id, getattr(h, "distance", None), h.text,
            )
        if refs:
            logger.info("[S1] 用户偏好召回 %d 条", len(refs))
        return refs
