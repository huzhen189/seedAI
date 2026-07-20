"""Project / Conversation / Message / Artifact Repository。"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import delete as sqldelete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Artifact, Conversation, Message, Project
from .base import BaseRepo

logger = logging.getLogger("business.repo")


class ProjectRepo(BaseRepo[Project]):
    model = Project
    cache_prefix = "proj"
    cache_ttl = 1500
    cache_enabled = True

    async def list_by_user(self, db: AsyncSession, user_id: int) -> list[Project]:
        return await self.list_by(db, user_id=user_id)

    async def get_by_share_id(self, db: AsyncSession, share_id: str) -> Optional[Project]:
        return await self.get_by(db, share_id=share_id)


class ConversationRepo(BaseRepo[Conversation]):
    model = Conversation
    cache_prefix = "conv"
    cache_ttl = 600
    cache_enabled = True

    async def list_by_project(self, db: AsyncSession, project_id: int, user_id: int) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.project_id == project_id, Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_with_messages(self, db: AsyncSession, conv_id: int) -> Optional[Conversation]:
        """查会话 + 关联消息(不缓存, 消息实时性要求高)。"""
        conv = await db.get(Conversation, conv_id)
        return conv

    async def delete_cascade(self, db: AsyncSession, conv: Conversation) -> None:
        """删会话 + 级联删消息。"""
        await db.execute(sqldelete(Message).where(Message.conversation_id == conv.id))
        await db.delete(conv)
        await db.commit()
        if self.cache_enabled:
            await self._cache_del(conv)


class MessageRepo(BaseRepo[Message]):
    model = Message
    cache_prefix = "msg"
    cache_ttl = 0
    cache_enabled = False  # 消息不缓存

    @staticmethod
    def _normalize(content: str) -> str:
        """万能解包: 提取 AI 消息的纯文本, 兼容所有已知格式。
        - {"data": "text"} → "text"
        - {"data":"a"}{"data":"b"}... → "ab..."  (多个 JSON 拼接)
        - <!DOCTYPE html> → 原样
        """
        if not content:
            return content
        if content.startswith('{"data":'):
            # 格式: {"data":"x"}{"data":"y"}... 多个 JSON 对象直接拼接
            # 不能直接 json.loads, 需要逐段解析
            import re, json
            parts = []
            pos = 0
            while True:
                start = content.find('{"data":', pos)
                if start == -1:
                    break
                end = content.find('}', start)
                if end == -1:
                    break
                try:
                    seg = json.loads(content[start:end + 1])
                    if isinstance(seg, dict) and "data" in seg:
                        parts.append(seg["data"])
                except Exception:
                    pass
                pos = end + 1
            if parts:
                return "".join(parts)
            # 单层 JSON: {"data": "text"}
            try:
                obj = json.loads(content)
                if isinstance(obj, dict) and "data" in obj:
                    return obj.get("data", content)
            except Exception:
                pass
        return content

    async def list_by_conversation(self, db: AsyncSession, conv_id: int) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.id.asc())
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())
        for m in rows:
            m.content = self._normalize(m.content)
        return rows

    async def get_by_trace(self, db: AsyncSession, trace_id: str, role: str) -> Optional[Message]:
        stmt = select(Message).where(Message.trace_id == trace_id, Message.role == role)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_assistant(self, db: AsyncSession, conv_id: int, trace_id: str,
                                content: str, model_id: str) -> Message:
        """幂等 upsert assistant 消息(重连/续传防重复)。入库前自动解包 JSON。"""
        content = self._normalize(content)
        existing = await self.get_by_trace(db, trace_id, "assistant")
        if existing:
            existing.content = content
            existing.model_id = model_id
            await db.commit()
            await db.refresh(existing)
            return existing
        msg = self.model(conversation_id=conv_id, role="assistant",
                         content=content, model_id=model_id, trace_id=trace_id)
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        return msg

    async def delete_by_conversation(self, db: AsyncSession, conv_id: int) -> None:
        await db.execute(sqldelete(Message).where(Message.conversation_id == conv_id))
        await db.commit()


class ArtifactRepo(BaseRepo[Artifact]):
    model = Artifact
    cache_prefix = "art"
    cache_ttl = 1500
    cache_enabled = True

    async def list_by_project(self, db: AsyncSession, project_id: int) -> list[Artifact]:
        return await self.list_by(db, project_id=project_id)


# 模块级单例
project_repo = ProjectRepo()
conv_repo = ConversationRepo()
message_repo = MessageRepo()
artifact_repo = ArtifactRepo()
