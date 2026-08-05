from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── 软偏好 rerank 关键词提取（低成本、纯子串，非语义） ──────────────────
# 把 tag / content 拆成多个候选关键词：按标点/空白切分；长且无空格的中文片段
# 再用 2~4 字滑窗补齐，避免「喜欢极简留白」这类无标点短语永远匹配不到。
_PREF_SPLIT_RE = re.compile(r"[\s,，。、；;.!?！？:：\n\t]+")


def _is_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s)


def _pref_keywords(p) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for src in (getattr(p, "tag", ""), getattr(p, "content", "")):
        s = (src or "").strip()
        if not s:
            continue
        for piece in _PREF_SPLIT_RE.split(s):
            piece = piece.strip()
            if len(piece) >= 2:
                if piece not in seen:
                    seen.add(piece)
                    out.append(piece)
            # 较长且无空格的中文片段：滑窗补 2/3/4 字 n-gram（命中率↑，避免盲区）
            if len(piece) >= 6 and _is_cjk(piece):
                for n in (2, 3, 4):
                    for i in range(len(piece) - n + 1):
                        gram = piece[i : i + n]
                        if gram not in seen:
                            seen.add(gram)
                            out.append(gram)
    # 去掉单字噪声（"的""a" 之类）
    return [k for k in out if len(k) >= 2]

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.contracts import Domain, RecallResult, SirState, StageId, StageStatus
from app.core.transition import migrate_legacy_sir
from app.core.turn_context import TurnContext
from app.db import transaction
from app.db.repositories import (
    conversations as conversation_repo,
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

# 结构化前情窗口上限：用户定「最多 5 条」，与 chat 短期记忆窗口一致。
CONTEXT_GIST_LIMIT = 5


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

        # 2) SIR 基态（v2：按会话级 canonical 指针精确加载 + 旧快照升格）
        try:
            await self._load_sir_base(context)
            if context.sir_base_snapshot_id is not None:
                hit = True
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
        #    **每次都执行，不再受 ``hit`` 门控**：``hit`` 仅表示「存在 SIR 快照/回溯
        #    上下文」，与「是否应基于 clean_message 召回历史记忆」无关。旧逻辑在会话首条
        #    消息（尚无 SIR 快照）时 ``hit=False`` 直接跳过向量召回，导致记忆召回形同虚设
        #    （如重置后首条「Air Jordan」整轮未执行召回）。现改为无条件执行；空消息/空库
        #    自然无命中，由召回函数自身 early-return。
        vector_refs: list[str] = []
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

        # 6) 结构化前情窗口（context_gist）：取最近 N 条对话(排除本轮)，供 T2 承接解析与
        #    S5 上下文澄清。只存结构化摘要(summary 或 content 截断)，不塞原始 transcript。
        try:
            await self._load_context_gist(context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[S1] 上下文 gist 加载失败(忽略): %s", exc, exc_info=True)

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

    # ── SIR 基态加载（三级优先级 + 旧快照升格） ───────────────────────────────
    async def _load_sir_base(self, context: TurnContext) -> None:
        """确定"上一轮结束时的状态"到底是哪一条快照，并升格为 v2 状态机结构。

        三级优先级（顺序即语义，不可调换）：
          1. **回溯控制**（``prior_turn_id`` 非空）：correct/supplement 的定义就是
             "回滚到那一轮结束时"，必须按 turn 取，不能被会话指针盖过；
          2. **会话 canonical 指针**（``conversations.canonical_sir_snapshot_id``）：
             S7 在轮末回写，指向该轮真正固化的规范态 —— 每会话恰好一个当前 SIR；
          3. **兜底最新一条**：仅用于指针尚未建立的历史会话。旧实现只有这一级，
             而 S3/S5 同一轮可能各落一条快照，"最新"未必是规范态，并发轮次下还会串轮。
        """
        assert self.session is not None
        snap = None
        source = ""
        if context.prior_turn_id is not None:
            snap = await sir_repo.latest_for_turn(self.session, context.prior_turn_id)
            source = "retro_turn"
        if snap is None:
            canonical_id = await conversation_repo.current_sir_snapshot_id(
                self.session, context.session.conversation_id
            )
            if canonical_id:
                snap = await sir_repo.get(self.session, int(canonical_id))
                source = "canonical"
        if snap is None:
            snap = await sir_repo.latest_for_conversation(
                self.session, context.session.conversation_id
            )
            source = "latest_fallback"
        if snap is None or not isinstance(snap.snapshot, dict):
            return

        state = SirState.model_validate(snap.snapshot)
        # 旧快照（无 task、只有 pending）升格：不迁则续答识别失效，
        # 老会话会在版本升级那一刻把正在进行的收集流程断掉。
        state = migrate_legacy_sir(state, origin_turn_id=snap.turn_id or context.turn_id)
        context.sir_base = state
        context.sir_base_snapshot_id = snap.id
        task = state.task
        logger.info(
            "[S1] 加载 SIR 基态 snapshot=%s(src=%s) turn=%s retro=%s\n"
            "  task=%s phase=%s goal=%.60s\n"
            "  slots=%s\n  agenda=%s\n  constraints=%s",
            snap.id, source, context.turn_id, context.prior_turn_id,
            task.id if task else None,
            task.phase.value if task else "idle",
            task.goal if task else "",
            state.slots,
            [(a.action, a.slot_key) for a in state.agenda],
            state.constraints,
        )

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
            logger.info(
                "[S1] L2 用户强事实 %d 条 标题=%s",
                len(facts), [f"{f.category}/{f.key_name}" for f in facts],
            )
        project_id = int(context.session.project_id) if context.session.project_id else None
        if project_id is not None:
            pf = await project_fact_repo.list_for_project(self.session, project_id)
            if pf:
                lines = [f"- {f.category}/{f.key_name}：{f.value}" for f in pf]
                context.user_context.append(
                    "【强事实·项目事实(零容错，不可被语义召回覆盖)】\n" + "\n".join(lines)
                )
                logger.info(
                    "[S1] L2 项目强事实 %d 条 标题=%s",
                    len(pf), [f"{f.category}/{f.key_name}" for f in pf],
                )

    # ── L5 向量语义召回 + 软偏好 rerank（命中回 MySQL 取正文） ──────────────────
    async def _load_memory_recall(self, context: TurnContext) -> list[str]:
        refs: list[str] = []
        query = (context.clean_message or "").strip()
        if not query:
            return refs
        user_id = int(context.user.user_id)
        project_id = int(context.session.project_id) if context.session.project_id else None
        # Chroma 铁律：顶层 where 只允许一个操作符，多条件须用 $and 包裹，否则
        # ``ValueError: Expected where to have exactly one operator``。记忆集合按
        # (user_id, project_id) 双维度隔离召回，必须用 $and 合成。
        where_conds = [{"user_id": user_id}]
        if project_id is not None:
            where_conds.append({"project_id": project_id})
        where: dict = {"$and": where_conds} if len(where_conds) > 1 else where_conds[0]
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
                # 注：软偏好(source_type=user_soft_pref)从不写向量库，故此处不会命中；
                # rerank 所需软偏好由下方 list_for_user 直接读 MySQL，不进此回查路径。
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

        # 预提取每条软偏好的关键词集合（tag+content 拆词+中文滑窗），只算一次。
        pref_kw = [(int(p.weight), _pref_keywords(p)) for p in soft_prefs]

        def _score(text: str) -> int:
            s = 0
            for weight, kws in pref_kw:
                # 每条软偏好只计一次权重：命中任一词即 +weight（不再整串前缀限制）。
                if any(k in text for k in kws):
                    s += weight
            return s

        backfilled.sort(key=lambda x: _score(x[0]), reverse=True)

        for text, _kind in backfilled:
            context.project_context.append(f"[记忆召回] {text}")

        logger.info(
            "[S1] 向量语义召回执行 query=%r top_k=%d -> %d 条（已回MySQL+rerank）",
            query[:80], settings.memory_recall_top_k, len(backfilled),
        )
        return refs

    async def _load_context_gist(self, context: TurnContext) -> None:
        """结构化前情窗口：取最近 CONTEXT_GIST_LIMIT 条对话(排除本轮)，最近优先。

        每条 = {turn_id, role, summary, content}。summary 优先用异步蒸馏摘要(语义更凝练)，
        回落 content 截断。这是承接解析的确定性输入——不依赖模型、可回滚（随 TurnContext 重置）。
        任何异常都不应影响主链路，已在调用方 try 包裹。
        """
        assert self.session is not None
        conv_id = context.session.conversation_id
        if not conv_id:
            return
        cur_turn = context.turn_id
        rows = (
            await self.session.execute(
                select(
                    Message.id, Message.turn_id, Message.role,
                    Message.content, Message.summary,
                ).where(
                    Message.conversation_id == conv_id,
                    Message.role.in_(["user", "assistant"]),
                    # 排除本轮自身（当前句未进 gist，避免自我承接）。
                    (Message.turn_id.is_(None)) | (Message.turn_id != cur_turn),
                )
                .order_by(desc(Message.id))
                .limit(CONTEXT_GIST_LIMIT)
            )
        ).all()
        # 倒序：index 0 = 最近一条前情（承接解析按近因加成）。
        gist: list[dict] = []
        for mid, turn_id, role, content, summary in reversed(rows):
            text = (summary or content or "").strip()
            if not text:
                continue
            gist.append({
                "turn_id": turn_id or f"msg:{mid}",
                "role": role,
                "summary": text[:200],
                "content": (content or "")[:400],
            })
        context.context_gist = gist
        if gist:
            logger.info(
                "[S1] 上下文 gist %d 条 最近=%s topic≈%.40s",
                len(gist), gist[0].get("turn_id"), gist[0].get("summary", ""),
            )

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
