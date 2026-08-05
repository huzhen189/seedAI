"""工作区 HTTP 层: 项目 / 会话 / 消息 / 自动建链。

直接落在「新真相模型」(app.models.content: Project / Conversation / Message) 上,
不经由 app.db.repositories.* —— 那些仓储仍停留在旧 schema(config / deleted_at /
turn_no / repo 等列在 v2 真相模型中已不存在), 会在后续 Task#51 收口中统一对齐。
本文件只服务 frontend/src/api/projects.ts 与 chat.ts 的契约, 不参与十阶段编排。

事务边界: 写操作走 transaction(); 只读端点用 get_db 依赖。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import ColumnElement

from app.db import get_db, transaction
from app.db.repositories.runtime import deployments_repo
from app.config import settings
from app.models import Conversation, Deployment, Message, Project
from app.security import CurrentUser, get_current_user

router = APIRouter(prefix="/api", tags=["workspace"])

logger = __import__("logging").getLogger("app.api.workspace")


# ---------------------------------------------------------------- 序列化


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


async def _project_view(p: Project, session: AsyncSession) -> dict[str, Any]:
    """项目视图序列化。线上地址(published_url)需惰性读取当前 active_deployment 的
    object_prefix 拼接 COS 公开域名得到; 未部署则为 null。"""
    spec: dict[str, Any] = p.site_spec if isinstance(p.site_spec, dict) else {}
    published_url: str | None = None
    if p.active_deployment_id and settings.cos_preview_domain:
        deployment = await session.get(Deployment, p.active_deployment_id)
        if deployment and deployment.object_prefix:
            published_url = (
                f"{settings.cos_preview_domain.rstrip('/')}/{deployment.object_prefix.strip('/')}/index.html"
            )
    return {
        "id": p.id,
        "user_id": p.user_id,
        "name": p.name,
        "status": p.status,
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
        # Artifact/Deployment 分离(规范 §10.4): head 是最新可预览版本,
        # published 是当前对外线上版本, 两者可以不同 —— 前端据此显示「有未发布改动」。
        "head_artifact_id": p.head_artifact_id,
        "published_artifact_id": p.published_artifact_id,
        "active_deployment_id": p.active_deployment_id,
        "has_unpublished_changes": bool(
            p.head_artifact_id is not None and p.head_artifact_id != p.published_artifact_id
        ),
        "published_url": published_url,
        # 旧前端字段: 需求文档存于 site_spec.requirement_doc(JSON 字符串)。
        "requirement_doc": spec.get("requirement_doc"),
    }


def _conversation_view(c: Conversation) -> dict[str, Any]:
    return {
        "id": c.id,
        "project_id": c.project_id,
        "user_id": c.user_id,
        "name": c.name,
        "status": c.status,
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
    }


def _message_view(m: Message) -> dict[str, Any]:
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "role": m.role,
        "content": m.content,
        # 旧前端字段: model_slot / turn_id 映射到 model_id / trace_id。
        "model_id": m.model_slot,
        "trace_id": m.turn_id,
        "created_at": _iso(m.created_at),
    }


# ---------------------------------------------------------------- 请求模型


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class RenamePayload(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ConversationCreate(BaseModel):
    project_id: int = Field(ge=1)
    name: str | None = None


class AutoStartPayload(BaseModel):
    text: str = Field(min_length=1)


# ---------------------------------------------------------------- 项目


@router.get("/projects")
async def list_projects(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Project)
            .where(Project.user_id == user.id, Project.status.in_(["draft", "active"]))
            .order_by(Project.updated_at.desc())
        )
    ).scalars().all()
    return [await _project_view(p, session) for p in rows]


@router.post("/projects")
async def create_project(
    payload: ProjectCreate,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    async with transaction() as session:
        project = Project(user_id=user.id, name=payload.name, status="active")
        session.add(project)
        await session.flush()
        await session.refresh(project)
        view = await _project_view(project, session)
    return view


@router.patch("/projects/{project_id}")
async def rename_project(
    project_id: int,
    payload: RenamePayload,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    async with transaction() as session:
        project = await session.get(Project, project_id)
        if project is None or project.user_id != user.id:
            raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
        if project.status not in ("draft", "active"):
            raise HTTPException(status_code=409, detail={"code": "PROJECT_NOT_EDITABLE", "status": project.status})
        project.name = payload.name
        project.lock_version += 1
        view = await _project_view(project, session)
    return view


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    """项目软删: status -> trashed(禁硬删, 见长期约定)。"""
    async with transaction() as session:
        project = await session.get(Project, project_id)
        if project is None or project.user_id != user.id:
            raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
        if project.status not in ("draft", "active"):
            raise HTTPException(status_code=409, detail={"code": "PROJECT_NOT_DELETABLE", "status": project.status})
        project.status = "trashed"
        project.lock_version += 1
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- 会话


@router.get("/conversations")
async def list_conversations(
    project_id: int = Query(ge=1),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    rows = (
        await session.execute(
            select(Conversation)
            .where(Conversation.project_id == project_id, Conversation.user_id == user.id)
            .order_by(Conversation.updated_at.desc())
        )
    ).scalars().all()
    return [_conversation_view(c) for c in rows]


@router.post("/conversations")
async def create_conversation(
    payload: ConversationCreate,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    async with transaction() as session:
        project = await session.get(Project, payload.project_id)
        if project is None or project.user_id != user.id:
            raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
        conversation = Conversation(
            project_id=payload.project_id,
            user_id=user.id,
            name=payload.name or "新对话",
            status="active",
        )
        session.add(conversation)
        await session.flush()
        await session.refresh(conversation)
        view = _conversation_view(conversation)
    return view


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
    return _conversation_view(conversation)


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: int,
    payload: RenamePayload,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    async with transaction() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user.id:
            raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
        conversation.name = payload.name
        conversation.version += 1
        view = _conversation_view(conversation)
    return view


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    """会话硬删: 级联清消息(FK ondelete=CASCADE)。
    注: 真相模型无 trashed 状态, 会话按硬删处理; 仅「项目」受软删约束。"""
    async with transaction() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user.id:
            raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
        await session.delete(conversation)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.asc())
            .limit(2000)
        )
    ).scalars().all()
    return [_message_view(m) for m in rows]


# ---------------------------------------------------------------- 自动建链


@router.post("/auto-start")
async def auto_start(
    payload: AutoStartPayload,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """首条对话无项目时: 按文本自动建项目 + 会话, 返回 {project, conversation}。"""
    async with transaction() as session:
        name = payload.text.strip()[:40] or "未命名项目"
        project = Project(user_id=user.id, name=name, status="active")
        session.add(project)
        await session.flush()
        await session.refresh(project)
        conversation = Conversation(
            project_id=project.id,
            user_id=user.id,
            name="新对话",
            status="active",
        )
        session.add(conversation)
        await session.flush()
        await session.refresh(conversation)
        project_view = await _project_view(project, session)
        conversation_view = _conversation_view(conversation)
    return {"project": project_view, "conversation": conversation_view}


# ---------------------------------------------------------------- 搜索


def _like(column: Any, q: str) -> ColumnElement[bool]:
    """构造安全的 LIKE 条件: 转义 LIKE 元字符, 防止用户输入的 % / _ 变成通配符。"""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.like(f"%{escaped}%", escape="\\")


@router.get("/search")
async def search_entities(
    q: str = Query(min_length=1, max_length=128),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """全局搜索: 项目名 + 会话名 -> SearchItem[]。

    契约见 frontend/src/types.ts::SearchItem —— {type, id, title, project_id}。
    project 项的 project_id 置为自身 id, 便于前端统一跳转。
    """
    keyword = q.strip()
    if not keyword:
        return []

    projects = (
        await session.execute(
            select(Project)
            .where(
                Project.user_id == user.id,
                Project.status.in_(["draft", "active"]),
                _like(Project.name, keyword),
            )
            .order_by(Project.updated_at.desc())
            .limit(10)
        )
    ).scalars().all()

    conversations = (
        await session.execute(
            select(Conversation)
            .where(Conversation.user_id == user.id, _like(Conversation.name, keyword))
            .order_by(Conversation.updated_at.desc())
            .limit(10)
        )
    ).scalars().all()

    items: list[dict[str, Any]] = [
        {"type": "project", "id": p.id, "title": p.name, "project_id": p.id} for p in projects
    ]
    items += [
        {"type": "conversation", "id": c.id, "title": c.name, "project_id": c.project_id}
        for c in conversations
    ]
    return items


@router.get("/search/messages")
async def search_messages(
    q: str = Query(min_length=1, max_length=128),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """消息全文搜索 -> MessageSearchResult[]。

    问答配对走 turn_id: 真相模型对 messages 建有 UniqueConstraint(turn_id, role),
    同一轮的 user / assistant 消息共享同一个 turn_id, 因此可精确聚合成「提问 + 回复」,
    无需按 id 邻接猜测。turn_id 为空的历史消息降级为单条展示。
    """
    keyword = q.strip()
    if not keyword:
        return []

    hits = (
        await session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.user_id == user.id,
                Message.role.in_(["user", "assistant"]),
                _like(Message.content, keyword),
            )
            .order_by(Message.id.desc())
            .limit(40)
        )
    ).scalars().all()
    if not hits:
        return []

    # 命中消息所属的轮次 -> 一次性取回该轮的全部消息, 供问答配对(避免 N+1)。
    turn_ids = {m.turn_id for m in hits if m.turn_id}
    pairs: dict[str, dict[str, Message]] = {}
    if turn_ids:
        siblings = (
            await session.execute(
                select(Message).where(
                    Message.turn_id.in_(turn_ids), Message.role.in_(["user", "assistant"])
                )
            )
        ).scalars().all()
        for m in siblings:
            if m.turn_id:
                pairs.setdefault(m.turn_id, {})[m.role] = m

    # 会话标题 + 项目名: 单次批量查询。
    conv_ids = {m.conversation_id for m in hits}
    meta_rows = (
        await session.execute(
            select(Conversation.id, Conversation.name, Project.id, Project.name)
            .join(Project, Project.id == Conversation.project_id)
            .where(Conversation.id.in_(conv_ids))
        )
    ).all()
    meta = {row[0]: {"conv_title": row[1], "project_id": row[2], "project_name": row[3]} for row in meta_rows}

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        # 同一轮的提问与回复可能双双命中, 按轮次去重, 只保留一条。
        dedup_key = f"t:{hit.turn_id}" if hit.turn_id else f"m:{hit.id}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        pair = pairs.get(hit.turn_id or "", {})
        user_msg = pair.get("user")
        ai_msg = pair.get("assistant")
        if user_msg is None and hit.role == "user":
            user_msg = hit
        if ai_msg is None and hit.role == "assistant":
            ai_msg = hit

        info = meta.get(hit.conversation_id)
        if info is None:  # 会话已删但消息残留(理论不该发生), 跳过而非 500。
            continue
        anchor = user_msg or hit
        results.append(
            {
                "message_id": anchor.id,
                "conversation_id": hit.conversation_id,
                "project_id": info["project_id"],
                "project_name": info["project_name"],
                "conv_title": info["conv_title"],
                "user_text": user_msg.content if user_msg else "",
                "ai_reply": ai_msg.content if ai_msg else "",
                "created_at": _iso(anchor.created_at),
            }
        )
        if len(results) >= 20:
            break
    return results
