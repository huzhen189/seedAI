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
from app.llm.extract import llm_extract
from app.models import Message
from app.ragstore import safe_upsert_bg

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
) -> None:
    """把本轮对话压缩提炼并落库（MySQL 主 + 向量辅）。fail-soft：任何异常仅记日志。

    设计为「不反噬主链路」——s7 以 ``asyncio.create_task`` 异步调用，本函数内部再吞掉异常。
    """
    try:
        await _do_persist(
            user_id=user_id,
            project_id=project_id,
            conversation_id=conversation_id,
            user_text=user_text,
            assistant_text=assistant_text,
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
) -> None:
    extraction = await llm_extract(
        user_text=user_text,
        assistant_text=assistant_text,
        project_id=project_id,
        conversation_id=conversation_id,
    )

    # 回查本轮 user 消息 id（用于 source_message_id 双向关联）。拿不到则留空。
    source_message_id: int | None = None
    async with transaction() as session:
        if conversation_id is not None:
            row = (
                await session.execute(
                    select(Message.id)
                    .where(Message.conversation_id == conversation_id, Message.role == "user")
                    .order_by(Message.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            source_message_id = row

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

        # 软偏好（仅 rerank，不进 prompt）。
        soft_pref_ids: list[int] = []
        if extraction["user_prefs"]:
            rows = await soft_pref_repo.upsert_many(
                session,
                [
                    {
                        "user_id": user_id,
                        "tag": p["tag"],
                        "content": str(p["content"]),
                        "weight": int(p.get("weight", 50)),
                    }
                    for p in extraction["user_prefs"]
                ],
            )
            soft_pref_ids = [r.id for r in rows]

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
        soft_pref_ids=soft_pref_ids,
        user_id=user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )


async def _upsert_vectors(
    *,
    memory_rows: list[dict],
    memory_ids: list[int],
    soft_pref_ids: list[int],
    user_id: int,
    project_id: int | None,
    conversation_id: int | None,
    source_message_id: int | None,
) -> None:
    """把 memories 行（title）与软偏好行（content）写入向量库（仅索引+元数据）。

    向量 documents 只放精简标题/文本，正文永远在 MySQL（§1.3）。命中后经
    metadatas.(source_type, source_id) 回 MySQL 取正文。
    """
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

    # 软偏好向量：source_type=user_soft_pref，命中后仅用于 rerank（不进 prompt）。
    if soft_pref_ids:
        # 重新取 content 作为文档（软偏好无独立 title，用 content 前 40 字作索引）。
        # 为避免再查库，这里用占位：实际 rerank 在 s1 内用软偏好文本加权，向量仅作召回入口。
        # 为简化，软偏好不单独建向量点，仅依赖 user_preferences 集合既有召回即可；
        # 若需精确再召回软偏好，可在 s1 直接读 MySQL user_soft_preferences（已这样做）。
        pass

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
