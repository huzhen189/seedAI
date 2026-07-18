"""项目管理 / 会话管理 / 搜索(均按 user_id 隔离)。

- 项目(Project) 1—N 会话(Conversation) 1—N 消息(Message)。
- 所有写操作先校验归属(user_id),非本人 404。
- 删除项目级联删会话与消息;删除会话级联删消息。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .models import Conversation, Message, Project
from .schemas import (
    ConversationResp,
    CreateConversationReq,
    CreateProjectReq,
    MessageResp,
    ProjectResp,
    RenameReq,
    SearchItemResp,
)
from .security import CurrentUser, get_current_user

router = APIRouter(prefix="/api", tags=["projects"])


# ---------- 项目 ----------
@router.get("/projects", response_model=list[ProjectResp])
async def list_projects(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Project)
            .where(Project.user_id == user.id)
            .order_by(Project.updated_at.desc())
        )
    ).scalars().all()
    return rows


@router.post("/projects", response_model=ProjectResp, status_code=201)
async def create_project(
    req: CreateProjectReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = Project(user_id=user.id, name=req.name)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


@router.patch("/projects/{project_id}", response_model=ProjectResp)
async def rename_project(
    project_id: int,
    req: RenameReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = (
        await db.execute(
            select(Project).where(Project.id == project_id, Project.user_id == user.id)
        )
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    proj.name = req.name
    await db.commit()
    await db.refresh(proj)
    return proj


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = (
        await db.execute(
            select(Project).where(Project.id == project_id, Project.user_id == user.id)
        )
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    conv_ids = (
        await db.execute(
            select(Conversation.id).where(Conversation.project_id == project_id)
        )
    ).scalars().all()
    if conv_ids:
        await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await db.execute(
            delete(Conversation).where(Conversation.project_id == project_id)
        )
    await db.delete(proj)
    await db.commit()
    return None


# ---------- 会话 ----------
@router.get("/conversations", response_model=list[ConversationResp])
async def list_conversations(
    project_id: int = Query(...),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Conversation)
            .where(Conversation.project_id == project_id, Conversation.user_id == user.id)
            .order_by(Conversation.updated_at.desc())
        )
    ).scalars().all()
    return rows


@router.post("/conversations", response_model=ConversationResp, status_code=201)
async def create_conversation(
    req: CreateConversationReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = (
        await db.execute(
            select(Project).where(
                Project.id == req.project_id, Project.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    conv = Conversation(project_id=req.project_id, user_id=user.id, title=req.title)
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.get("/conversations/{conversation_id}", response_model=ConversationResp)
async def get_conversation(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    msgs = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.asc())
        )
    ).scalars().all()
    resp = ConversationResp.model_validate(conv)
    resp.messages = [MessageResp.model_validate(m) for m in msgs]
    return resp


@router.patch("/conversations/{conversation_id}", response_model=ConversationResp)
async def rename_conversation(
    conversation_id: int,
    req: RenameReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    conv.title = req.name
    await db.commit()
    await db.refresh(conv)
    return conv


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    await db.execute(delete(Message).where(Message.conversation_id == conversation_id))
    await db.delete(conv)
    await db.commit()
    return None


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResp])
async def list_messages(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    msgs = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.asc())
        )
    ).scalars().all()
    return msgs


# ---------- 搜索 ----------
@router.get("/search", response_model=list[SearchItemResp])
async def search(
    q: str = Query(..., min_length=1),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    like = f"%{q}%"
    results: list[SearchItemResp] = []
    projs = (
        await db.execute(
            select(Project).where(Project.user_id == user.id, Project.name.like(like))
        )
    ).scalars().all()
    for p in projs:
        results.append(
            SearchItemResp(type="project", id=p.id, title=p.name, project_id=None)
        )
    convs = (
        await db.execute(
            select(Conversation).where(
                Conversation.user_id == user.id, Conversation.title.like(like)
            )
        )
    ).scalars().all()
    for c in convs:
        results.append(
            SearchItemResp(
                type="conversation",
                id=c.id,
                title=c.title or "(未命名会话)",
                project_id=c.project_id,
            )
        )
    return results
