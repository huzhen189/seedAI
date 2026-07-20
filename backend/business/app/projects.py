"""项目管理 / 会话管理 / 搜索(均按 user_id 隔离 / Repository 层统一访问)。

- 项目(Project) 1—N 会话(Conversation) 1—N 消息(Message)。
- 所有写操作先校验归属(user_id),非本人 404。
- 删除项目级联删会话与消息;删除会话级联删消息。
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .cache import cache_get, cache_set, cache_invalidate
from .config import settings
from .db import get_db
from .models import Artifact, Conversation, Message, Project
from .repos.business_repos import artifact_repo, conv_repo, message_repo, project_repo
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


def _fix_content(content: str) -> str:
    """解包 messages.content 中的 JSON 碎片 {"data":"x"}{"data":"y"} → "xy"。
    兼容: 单层 {"data":"text"} / 多层拼接 / 纯文本 / 结构化 {"type":"site",...}
    """
    if not content or not content.startswith('{"data":'):
        return content
    # 多段拼接: {"data":"x"}{"data":"y"}...
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
    # 单层 JSON
    try:
        obj = json.loads(content)
        if isinstance(obj, dict) and "data" in obj:
            return obj.get("data", content)
    except Exception:
        pass
    return content


router = APIRouter(prefix="/api", tags=["projects"])


# ---------- 项目 ----------
@router.get("/projects", response_model=list[ProjectResp])
async def list_projects(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await project_repo.list_by_user(db, user.id)


@router.post("/projects", response_model=ProjectResp, status_code=201)
async def create_project(
    req: CreateProjectReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await project_repo.create(db, user_id=user.id, name=req.name)


@router.patch("/projects/{project_id}", response_model=ProjectResp)
async def rename_project(
    project_id: int,
    req: RenameReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await project_repo.update(db, proj, name=req.name)


# ---------- 分享(⑤-b) ----------
@router.post("/projects/{project_id}/share", response_model=ProjectResp)
async def share_project(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    if not proj.share_id:
        proj.share_id = uuid.uuid4().hex
    return await project_repo.update(db, proj, share_id=proj.share_id, is_public=True)


@router.delete("/projects/{project_id}/share", response_model=ProjectResp)
async def unshare_project(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    return await project_repo.update(db, proj, share_id=None, is_public=False)


@router.get("/share/{share_id}", response_model=ProjectResp)
async def get_shared_project(
    share_id: str,
    db: AsyncSession = Depends(get_db),
):
    proj = await project_repo.get_by_share_id(db, share_id)
    if proj is None or not proj.is_public:
        raise HTTPException(status_code=404, detail="share not found or not public")
    return proj


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    # 级联: 删消息 → 删会话 → 删项目
    conv_ids = (
        (await db.execute(select(Conversation.id).where(Conversation.project_id == project_id)))
        .scalars().all()
    )
    if conv_ids:
        await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await db.execute(delete(Conversation).where(Conversation.project_id == project_id))
    await project_repo.delete(db, proj)
    await cache_invalidate(f"conv:list:{project_id}:*")
    return None


# ---------- 会话 ----------
@router.get("/conversations", response_model=list[ConversationResp])
async def list_conversations(
    project_id: int = Query(...),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cache_key = f"conv:list:{project_id}:{user.id}"
    cached = await cache_get(cache_key)
    if cached:
        try:
            return [ConversationResp.model_validate(r) for r in json.loads(cached)]
        except Exception:
            pass
    rows = await conv_repo.list_by_project(db, project_id, user.id)
    try:
        raw = json.dumps(
            [ConversationResp.model_validate(r).model_dump(mode="json") for r in rows],
            default=str,
        )
        await cache_set(cache_key, raw, ttl=300)
    except Exception:
        pass
    return rows


@router.post("/conversations", response_model=ConversationResp, status_code=201)
async def create_conversation(
    req: CreateConversationReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await project_repo.get_by(db, id=req.project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    conv = await conv_repo.create(db, project_id=req.project_id, user_id=user.id, title=req.title)
    await cache_invalidate(f"conv:list:{req.project_id}:*")
    return conv


@router.get("/conversations/{conversation_id}", response_model=ConversationResp)
async def get_conversation(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await conv_repo.get_by(db, id=conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    msgs = await message_repo.list_by_conversation(db, conversation_id)
    resp = ConversationResp.model_validate(conv)
    # 解包 content 中的 JSON 碎片(兜底)
    for m in msgs:
        m.content = _fix_content(m.content)
    resp.messages = [MessageResp.model_validate(m) for m in msgs]
    return resp


@router.patch("/conversations/{conversation_id}", response_model=ConversationResp)
async def rename_conversation(
    conversation_id: int,
    req: RenameReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await conv_repo.get_by(db, id=conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return await conv_repo.update(db, conv, title=req.name)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await conv_repo.get_by(db, id=conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    await conv_repo.delete_cascade(db, conv)
    if conv.project_id:
        await cache_invalidate(f"conv:list:{conv.project_id}:*")
    return None


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResp])
async def list_messages(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await conv_repo.get_by(db, id=conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return await message_repo.list_by_conversation(db, conversation_id)


# ---------- 搜索(复杂 LIKE 查询, 不走缓存/Repo) ----------
@router.get("/search", response_model=list[SearchItemResp])
async def search(
    q: str = Query(..., min_length=1),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    like = f"%{q}%"
    results: list[SearchItemResp] = []
    projs = (await db.execute(
        select(Project).where(Project.user_id == user.id, Project.name.like(like))
    )).scalars().all()
    for p in projs:
        results.append(SearchItemResp(type="project", id=p.id, title=p.name, project_id=None))
    convs = (await db.execute(
        select(Conversation).where(Conversation.user_id == user.id, Conversation.title.like(like))
    )).scalars().all()
    for c in convs:
        results.append(SearchItemResp(
            type="conversation", id=c.id,
            title=c.title or "(未命名会话)", project_id=c.project_id,
        ))
    return results


# ---------- 生成产物(Artifact) ----------
@router.get("/projects/{project_id}/artifacts")
async def list_artifacts(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    rows = await artifact_repo.list_by_project(db, project_id)
    return [
        {
            "id": a.id, "title": a.title, "trace_id": a.trace_id,
            "repo": a.repo, "preview_url": a.preview_url,
            "download_url": a.download_url, "status": a.status,
            "files": a.files,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


@router.post("/projects/{project_id}/retry-upload")
async def retry_upload(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重传 COS 失败的产物: 找 status=uploading 的 artifact, 读本地 HTML 重新上传, 更新 DB。"""
    import httpx
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    artifacts = await artifact_repo.list_by(db, project_id=project_id, status="uploading")
    results = []
    for art in artifacts:
        if not art.trace_id:
            continue
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.post(
                    f"{settings.ai_service_url}/retry-upload",
                    json={"trace_id": art.trace_id},
                )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    art.preview_url = data["url"]
                    art.download_url = data["url"]
                    art.status = "done"
                    await db.commit()
                    results.append({"id": art.id, "ok": True, "url": data["url"]})
                    continue
            results.append({"id": art.id, "ok": False, "error": f"AI service {resp.status_code}"})
        except Exception as e:
            results.append({"id": art.id, "ok": False, "error": str(e)})
    return {"results": results}


@router.get("/projects/{project_id}/pending-uploads")
async def pending_uploads(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """检查是否有待重传的产物。"""
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    rows = await artifact_repo.list_by(db, project_id=project_id, status="uploading")
    return {"count": len(rows), "ids": [a.id for a in rows]}
