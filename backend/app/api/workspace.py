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

from app.db import get_db, transaction
from app.models import Conversation, Message, Project
from app.security import CurrentUser, get_current_user

router = APIRouter(prefix="/api", tags=["workspace"])

logger = __import__("logging").getLogger("app.api.workspace")


# ---------------------------------------------------------------- 序列化


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _project_view(p: Project) -> dict[str, Any]:
    spec: dict[str, Any] = p.site_spec if isinstance(p.site_spec, dict) else {}
    return {
        "id": p.id,
        "user_id": p.user_id,
        "name": p.name,
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
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
    return [_project_view(p) for p in rows]


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
        view = _project_view(project)
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
        view = _project_view(project)
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
        project_view = _project_view(project)
        conversation_view = _conversation_view(conversation)
    return {"project": project_view, "conversation": conversation_view}
