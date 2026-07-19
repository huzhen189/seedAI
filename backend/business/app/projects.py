"""项目管理 / 会话管理 / 搜索(均按 user_id 隔离)。

- 项目(Project) 1—N 会话(Conversation) 1—N 消息(Message)。
- 所有写操作先校验归属(user_id),非本人 404。
- 删除项目级联删会话与消息;删除会话级联删消息。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
import uuid
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
        (
            await db.execute(
                select(Project)
                .where(Project.user_id == user.id)
                .order_by(Project.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
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


# ---------- 分享(⑤-b):最小分享 = 生成 COS 预览直链 + 公开开关 ----------
@router.post("/projects/{project_id}/share", response_model=ProjectResp)
async def share_project(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """开启分享:生成 share_id(UUID)并置 is_public=True;返回含 preview_url 的项目。

    最小分享方案(⑤-b,不做独立分享页):前端用返回的 preview_url 直接「复制预览链接」。
    """
    proj = (
        await db.execute(
            select(Project).where(Project.id == project_id, Project.user_id == user.id)
        )
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    if not proj.share_id:
        proj.share_id = uuid.uuid4().hex
    proj.is_public = True
    await db.commit()
    await db.refresh(proj)
    return proj


@router.delete("/projects/{project_id}/share", response_model=ProjectResp)
async def unshare_project(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """关闭分享:清空 share_id 并置 is_public=False。"""
    proj = (
        await db.execute(
            select(Project).where(Project.id == project_id, Project.user_id == user.id)
        )
    ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    proj.share_id = None
    proj.is_public = False
    await db.commit()
    await db.refresh(proj)
    return proj


@router.get("/share/{share_id}", response_model=ProjectResp)
async def get_shared_project(
    share_id: str,
    db: AsyncSession = Depends(get_db),
):
    """公开只读分享入口(无鉴权):仅当 is_public=True 时返回项目(含 preview_url)。

    供未来只读分享页 / 复制链接校验使用。
    """
    proj = (
        await db.execute(select(Project).where(Project.share_id == share_id))
    ).scalar_one_or_none()
    if proj is None or not proj.is_public:
        raise HTTPException(status_code=404, detail="share not found or not public")
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
        (await db.execute(select(Conversation.id).where(Conversation.project_id == project_id)))
        .scalars()
        .all()
    )
    if conv_ids:
        await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await db.execute(delete(Conversation).where(Conversation.project_id == project_id))
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
        (
            await db.execute(
                select(Conversation)
                .where(Conversation.project_id == project_id, Conversation.user_id == user.id)
                .order_by(Conversation.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return rows


@router.post("/conversations", response_model=ConversationResp, status_code=201)
async def create_conversation(
    req: CreateConversationReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = (
        await db.execute(
            select(Project).where(Project.id == req.project_id, Project.user_id == user.id)
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
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.asc())
            )
        )
        .scalars()
        .all()
    )
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
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.asc())
            )
        )
        .scalars()
        .all()
    )
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
        (
            await db.execute(
                select(Project).where(Project.user_id == user.id, Project.name.like(like))
            )
        )
        .scalars()
        .all()
    )
    for p in projs:
        results.append(SearchItemResp(type="project", id=p.id, title=p.name, project_id=None))
    convs = (
        (
            await db.execute(
                select(Conversation).where(
                    Conversation.user_id == user.id, Conversation.title.like(like)
                )
            )
        )
        .scalars()
        .all()
    )
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
