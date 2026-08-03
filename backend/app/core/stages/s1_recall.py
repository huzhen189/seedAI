from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.contracts import Domain, RecallResult, SirState, StageId, StageStatus
from app.core.turn_context import TurnContext
from app.db import transaction
from app.db.repositories import (
    memories as memory_repo,
    project_events as project_event_repo,
    project_facts as project_fact_repo,
    sir_snapshots as sir_repo,
    user_facts as user_fact_repo,
    user_soft_preferences as soft_pref_repo,
)
from app.models import Artifact, Message
from app.ragstore import retrieve as _rag_retrieve
from .base import BaseStage


class S1RecallStage(BaseStage):
    """S1 召回(§5.6)。

    生产级实现：
      - **加载 SIR 基态**：从 ``sir_snapshots`` 取最新 base 快照写入 ``context.sir_base``。
      - **加载回溯上下文**：prior_turn 的执行域与产物 project，供 S2/S4/S6 域继承。
      - **L2 强事实精确取**（新增，记忆 v2）：user_facts / project_facts 全量精确取，
        零容错、不依赖向量；注入 ``user_context`` 作为强事实段，L2 压 L5。
      - **L5 向量语义召回**（记忆 v2）：查 ``memory`` 集合，命中经 ``(source_type,
        source_id)`` 回 MySQL 取标题+正文（绝不读 ``h.text`` 当真相），经软偏好 rerank
        加权后注入 ``project_context`` 作远场补充。project_events 不进 prompt。
      - 任何加载失败都不阻断主链路，降级为空基态并在 recall 上标注 degraded。
    """

    stage_id = StageId.S1

    async def run(self, context: TurnContext):
        if self.session is None:
            context.recall = RecallResult(status="skipped")
            return self.result(StageStatus.SKIPPED, "recall_no_session")

        degraded: str | None = None
        hit = False

        # 1) 回溯上下文
        if context.prior_turn_id is not None:
            try:
                await self._load_retro_context(context)
                hit = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("[S1] 回溯上下文加载失败: %s", exc, exc_info=True)
                degraded = "retro_context_failed"

        # 2) SIR 基态
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

        # 3) L2 强事实精确取（零容错，不依赖向量）
        #    注意：即使 SIR/回溯降级也照常执行——L2 与 SIR 解耦，
        #    强事实命中不应因 SIR 基态加载失败而丢失（sir_base 默认空壳即可继续）。
        try:
            await self._load_strong_facts(context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[S1] L2 强事实加载失败(忽略): %s", exc, exc_info=True)

        # 4) L5 向量语义召回 + 软偏好 rerank
        vector_refs: list[str] = []
        if hit:
            try:
                vector_refs = await self._load_memory_recall(context)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[S1] 向量语义召回失败(忽略): %s", exc, exc_info=True)

        # 5) 用户偏好（旧 user_preferences 集合，保留兼容；重置后通常为空）
        user_refs: list[str] = []
        try:
            user_refs = await self._load_user_preferences(context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[S1] 用户偏好召回失败(忽略): %s", exc, exc_info=True)

        has_content = bool(
            hit or vector_refs or user_refs or context.user_context or context.project_context
        )
        refs = [str(context.sir_base_snapshot_id)] if context.sir_base_snapshot_id else []
        refs.extend(vector_refs)
        refs.extend(user_refs)

        # 降级不影响 L2/L5 服务：有内容则带降级原因返回（记为 degraded，仍附 refs）；
        # 仅当 SIR 失败且无任何内容时才是纯 degraded。
        if has_content:
            if degraded:
                context.recall = RecallResult(
                    status="degraded", degradation_reason=degraded, references=refs
                )
                return self.result(StageStatus.COMPLETED, "recall_degraded_with_hits")
            context.recall = RecallResult(status="hit", references=refs)
            return self.result(StageStatus.COMPLETED, "recall_hit")
        if degraded:
            context.recall = RecallResult(status="degraded", degradation_reason=degraded)
            return self.result(StageStatus.COMPLETED, "recall_degraded")
        context.recall = RecallResult(status="empty")
        return self.result(StageStatus.SKIPPED, "recall_gate_no_signal")

    async def _load_retro_context(self, context: TurnContext) -> None:
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

    # ── L2 强事实精确取（零容错，压 L5） ──────────────────────────────────────
    async def _load_strong_facts(self, context: TurnContext) -> None:
        assert self.session is not None
        user_id = int(context.user.user_id)
        facts = await user_fact_repo.list_for_user(self.session, user_id)
        if facts:
            lines = [f"- {f.category}/{f.key_name}：{f.value}" for f in facts]
            context.user_context.append(
                "【强事实·用户偏好(零容错，不可被语义召回覆盖)】\n" + "\n".join(lines)
            )
            logger.info("[S1] L2 用户强事实 %d 条", len(facts))
        project_id = int(context.session.project_id) if context.session.project_id else None
        if project_id is not None:
            pf = await project_fact_repo.list_for_project(self.session, project_id)
            if pf:
                lines = [f"- {f.category}/{f.key_name}：{f.value}" for f in pf]
                context.user_context.append(
                    "【强事实·项目事实(零容错，不可被语义召回覆盖)】\n" + "\n".join(lines)
                )
                logger.info("[S1] L2 项目强事实 %d 条", len(pf))

    # ── L5 向量语义召回 + 软偏好 rerank（命中回 MySQL 取正文） ──────────────────
    async def _load_memory_recall(self, context: TurnContext) -> list[str]:
        refs: list[str] = []
        query = (context.clean_message or "").strip()
        if not query:
            return refs
        user_id = int(context.user.user_id)
        project_id = int(context.session.project_id) if context.session.project_id else None
        where: dict = {"user_id": user_id}
        if project_id is not None:
            where["project_id"] = project_id
        try:
            hits = await _rag_retrieve(
                settings.chroma_collection_memory,
                query,
                top_k=settings.memory_recall_top_k,
                where=where,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[S1] memory 集合召回失败: %s", exc)
            return refs

        # 经 (source_type, source_id) 回查 MySQL 取标题+正文（绝不读 h.text 当真相）。
        backfilled: list[tuple[str, str]] = []  # (text, kind)
        for h in hits:
            meta = h.metadata or {}
            st = meta.get("source_type")
            sid = meta.get("source_id")
            text = ""
            kind = meta.get("kind", "")
            try:
                if st == "message" and sid is not None:
                    row = await memory_repo.get(self.session, int(sid))  # type: ignore[arg-type]
                    if row is not None:
                        text = row.summary
                elif st == "project_event" and sid is not None:
                    # 事件不进 prompt：跳过（仅审计/间接经 mem接 摘要入 L5）
                    continue
                elif st == "user_soft_pref":
                    # 软偏好不进 prompt，仅参与 rerank，跳过正文注入
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("[S1] 回查 MySQL 失败 source=%s/%s: %s", st, sid, exc)
                text = ""
            if text:
                backfilled.append((text, kind))
                refs.append(f"memory:{st}:{sid}")
                logger.info(
                    "[S1] 向量召回(记忆) source=%s/%s kind=%s dist=%.3f\n  正文(回MySQL): %s",
                    st, sid, kind, getattr(h, "distance", None), text[:120],
                )

        if not backfilled:
            return refs

        # 软偏好 rerank：取同源 user 的软偏好，按 tag/content 命中度对召回段加权排序。
        try:
            soft_prefs = await soft_pref_repo.list_for_user(self.session, user_id)
        except Exception:  # noqa: BLE001
            soft_prefs = []

        def _score(text: str) -> int:
            s = 0
            for p in soft_prefs:
                tag = (p.tag or "").strip()
                if tag and tag in text:
                    s += int(p.weight)
                else:
                    content_head = (p.content or "")[:12]
                    if content_head and content_head in text:
                        s += int(p.weight)
            return s

        backfilled.sort(key=lambda x: _score(x[0]), reverse=True)

        for text, _kind in backfilled:
            context.project_context.append(f"[记忆召回] {text}")

        logger.info("[S1] 向量语义召回 %d 条（已回MySQL+rerank）", len(backfilled))
        return refs

    async def _load_user_preferences(self, context: TurnContext) -> list[str]:
        """旧 user_preferences 集合召回（兼容保留；重置后通常为空）。"""
        refs: list[str] = []
        query = (context.clean_message or "").strip()
        if not query:
            return refs
        try:
            hits = await _rag_retrieve(
                settings.chroma_collection_user_preferences,
                query, top_k=3,
                where={"user_id": int(context.user.user_id)},
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


__all__ = ["S1RecallStage"]
