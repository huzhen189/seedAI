"""记忆写入主链路（同步双写 MySQL 主 + 异步向量辅，事件驱动）。

见 docs/plan-memory-v2-landing.md §2。由 s7 在本轮回填完成、响应已交付之后调用
（不在 token 流内，红线#5）。流程：

  1) 一次 LLM 提取（app.llm.extract.llm_extract）→ 固定 Schema JSON；
  2) 代码层解析后分库（落库 100% 在代码侧，红线#4）：
       - user_facts / project_facts → MySQL L2 强事实（幂等 UPSERT）；
       - user_prefs → MySQL user_soft_preferences（仅 rerank）；
       - project_exps / session_summary → MySQL memories 行（title+正文，真相）；
       - 本轮过程事件 → MySQL project_events（审计，不进 prompt）；
       - 可选：更新本轮 user 消息 summary 列（标题+正文）；
  3) 向量库只存精简标题（documents=[title]）+ metadatas(source_type/source_id/...)，
     命中后回 MySQL 取正文（§1.3）。向量写入 fail-soft、后台派发、可丢可补。

全部 MySQL 写入在同一事务内（不丢），向量在事务提交后异步派发。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.config import settings
from app.db import transaction
from app.db.repositories import (
    memories as memory_repo,
    project_events as project_event_repo,
    project_facts as project_fact_repo,
    user_facts as user_fact_repo,
    user_soft_preferences as soft_pref_repo,
)
from app.core.memory_hints import merge_hints
from app.db.repositories.qc_scores import qc_score_repo
from app.llm.extract import llm_extract
from app.models import Message

logger = logging.getLogger("app.core.memory_write")


def _title(title: str) -> str:
    """收敛标题长度，作为向量索引串（≤40 字，超长截断）。"""
    t = (title or "").strip()
    return t[:40]


async def persist_and_extract(
    *,
    user_id: int,
    project_id: int | None,
    conversation_id: int | None,
    user_text: str,
    assistant_text: str,
    trace_id: str | None = None,
    hints: list[dict[str, Any]] | None = None,
) -> None:
    """把本轮对话压缩提炼并落库（MySQL 主 + 向量辅），并落一条聊天级 QC 评分。

    fail-soft：任何异常仅记日志，绝不反噬主链路。
    S7 对**所有 turn**统一派发本任务（不论意图类型/数量）；记忆落库仍受「执行态已提交」
    约束（由调用方决定 text 是否非空），但 QC 评分始终落库（审计/展示，与执行态无关）。
    """
    try:
        await _do_persist(
            user_id=user_id,
            project_id=project_id,
            conversation_id=conversation_id,
            user_text=user_text,
            assistant_text=assistant_text,
            trace_id=trace_id,
            hints=hints,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[memory_write] 记忆提炼落库失败(已忽略): %s", exc, exc_info=True)


async def _do_persist(
    *,
    user_id: int,
    project_id: int | None,
    conversation_id: int | None,
    user_text: str,
    assistant_text: str,
    trace_id: str | None = None,
    hints: list[dict[str, Any]] | None = None,
) -> None:
    extraction = await llm_extract(
        user_text=user_text,
        assistant_text=assistant_text,
        project_id=project_id,
        conversation_id=conversation_id,
    )
    # 批次 B：把状态机确定性产出的 memory_hints 并入抽取结果（LLM 抽取之后确定性追加，
    # 保证结构化信号 100% 落库，不依赖 LLM 是否"听懂"提示）。
    merge_hints(extraction, hints, project_id)

    # 回查本轮 user 消息 id（用于 source_message_id 双向关联）。
    # 改：按本 turn 的 turn_id 精确定位（trace_id==turn_id），不再取「会话内最新用户消息」——
    # 否则异步写入延迟时（上一轮记忆写入慢于本轮插入）会把本 turn 记忆错挂到后一条消息上。
    # trace_id 为空时留 None（不再回退 conversation 最新，避免错挂）。
    source_message_id: int | None = None
    async with transaction() as session:
        if trace_id is not None:
            row = (
                await session.execute(
                    select(Message.id)
                    .where(Message.turn_id == trace_id, Message.role == "user")
                    .order_by(Message.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            source_message_id = row

        # ── 聊天级 QC 评分落库（审计/展示，与执行态无关） ──
        # 复用 llm_extract 的 qc 字段，不额外调 LLM；trace_id==turn_id，缺则跳过。
        qc = extraction.get("qc") or {}
        qc_overall = float(qc.get("overall") or 0.0)
        if trace_id and qc_overall > 0:
            try:
                await qc_score_repo.upsert(
                    session,
                    trace_id=trace_id,
                    model_id=None,
                    conversation_id=conversation_id,
                    result={
                        "scores": qc.get("scores") or {},
                        "overall": qc_overall,
                        "needs_review": bool(qc.get("needs_review", False)),
                        "safety_risk": str(qc.get("safety_risk") or "low"),
                        "rationale": str(qc.get("rationale") or ""),
                    },
                )
                logger.info("[memory_write] 聊天级 QC 已落库 trace=%s overall=%.1f safety=%s",
                            trace_id, qc_overall, str(qc.get("safety_risk") or "low"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[memory_write] QC 落库失败(已忽略): %s", exc)

        # L2 强事实（幂等 UPSERT，零容错）。
        if extraction["user_facts"]:
            await user_fact_repo.upsert_many(
                session,
                [
                    {
                        "user_id": user_id,
                        "category": f["category"],
                        "key_name": f["key_name"],
                        "value": str(f["value"]),
                        "confidence": int(f.get("confidence", 90)),
                    }
                    for f in extraction["user_facts"]
                ],
            )
        if project_id is not None and extraction["project_facts"]:
            await project_fact_repo.upsert_many(
                session,
                [
                    {
                        "project_id": project_id,
                        "category": f["category"],
                        "key_name": f["key_name"],
                        "value": str(f["value"]),
                    }
                    for f in extraction["project_facts"]
                ],
            )

        # 软偏好（仅 rerank，不进 prompt，不写向量库——rerank 时由 s1 直接读 MySQL）。
        # 容错：LLM 返回的某项可能缺 ``content``（键名漂移/漏字段），用 .get 兜底并跳过空项，
        # 避免单条坏数据触发 KeyError 让整批记忆写失败（之前 mem_write 静默全崩）。
        if extraction["user_prefs"]:
            prefs = []
            for p in extraction["user_prefs"]:
                content = p.get("content")
                if not content:
                    logger.warning("[memory_write] 跳过缺 content 的 user_pref: tag=%s", p.get("tag"))
                    continue
                prefs.append(
                    {
                        "user_id": user_id,
                        "tag": p["tag"],
                        "content": str(content),
                        "weight": int(p.get("weight", 50)),
                    }
                )
            if prefs:
                await soft_pref_repo.upsert_many(session, prefs)

        # 项目过程事件（审计，不进 prompt）。仅在项目上下文存在时记录。
        if project_id is not None:
            detail = f"轮次记忆提炼\nuser: {user_text[:300]}\nassistant: {assistant_text[:300]}"
            await project_event_repo.insert_event(
                session,
                project_id=project_id,
                conversation_id=conversation_id,
                kind="other",
                detail=detail,
                source_message_id=source_message_id,
            )

        # memories 行：project_exps（多意图分段）+ session_summary。
        memory_rows: list[dict] = []
        for exp in extraction["project_exps"]:
            title = _title(exp.get("title"))
            body = str(exp.get("body") or "")
            memory_rows.append(
                {
                    "user_id": user_id,
                    "project_id": project_id,
                    "conversation_id": conversation_id,
                    "kind": "proj_exp",
                    "source_type": "message",
                    "source_message_id": source_message_id,
                    "title": title,
                    "summary": f"{title}\n\n{body}",
                    "payload": exp.get("payload") or {},
                }
            )
        ss = extraction["session_summary"]
        ss_title = _title(ss.get("title"))
        ss_body = str(ss.get("body") or "")
        if ss_title or ss_body:
            memory_rows.append(
                {
                    "user_id": user_id,
                    "project_id": project_id,
                    "conversation_id": conversation_id,
                    "kind": "conv_summary",
                    "source_type": "message",
                    "source_message_id": source_message_id,
                    "title": ss_title,
                    "summary": f"{ss_title}\n\n{ss_body}",
                    "payload": {"highlights": ss.get("highlights") or []},
                }
            )

        # 本轮消息 summary 列（标题+正文，双轨）。
        if source_message_id is not None and (ss_title or ss_body):
            msg = await session.get(Message, source_message_id)
            if msg is not None:
                msg.summary = f"{ss_title}\n\n{ss_body}"

        memory_ids: list[int] = []
        if memory_rows:
            inserted = await memory_repo.insert_many(session, memory_rows)
            memory_ids = [m.id for m in inserted]

    # ── 事务已提交：派发向量写入（fail-soft 后台，可丢可补） ──
    await _upsert_vectors(
        memory_rows=memory_rows,
        memory_ids=memory_ids,
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )


async def _upsert_vectors(
    *,
    memory_rows: list[dict],
    memory_ids: list[int],
    user_id: int,
    project_id: int | None,
    conversation_id: int | None,
    source_message_id: int | None,
) -> None:
    """把 memories 行（title）写入向量库（仅索引+元数据）。

    向量 documents 只放精简标题/文本，正文永远在 MySQL（§1.3）。命中后经
    metadatas.(source_type, source_id) 回 MySQL 取正文。
    （软偏好不写向量库，rerank 由 S1 直接读 MySQL user_soft_preferences。）
    """
    from app.ragstore import safe_upsert_bg  # 惰性：避免无向量依赖环境导入即拉起 numpy

    docs: list[str] = []
    metas: list[dict] = []
    ids: list[str] = []

    for row, mid in zip(memory_rows, memory_ids, strict=False):
        docs.append(row["title"] or "")
        metas.append(
            {
                "source_type": row["source_type"],
                "source_id": mid,
                "user_id": user_id,
                "project_id": project_id,
                "conversation_id": conversation_id,
                "kind": row["kind"],
                "source_message_id": source_message_id,
                "embedding_status": "ready",
            }
        )
        ids.append(f"mem_{mid}")

    if docs:
        asyncio.create_task(
            safe_upsert_bg(
                settings.chroma_collection_memory,
                docs,
                metadatas=metas,
                ids=ids,
            )
        )


__all__ = ["persist_and_extract"]
