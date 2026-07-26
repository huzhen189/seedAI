"""项目管理 / 会话管理 / 搜索(均按 user_id 隔离 / Repository 层统一访问)。

- 项目(Project) 1—N 会话(Conversation) 1—N 消息(Message)。
- 所有写操作先校验归属(user_id),非本人 404。
- 删除项目为软删除(置 deleted_at 标志),保留全部关联数据与统计;前端过滤不显示。
- 删除会话级联删消息。
"""
import logging

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .cache import cache_delete, cache_get, cache_invalidate, cache_set
from .config import settings
from .db import get_db
from .models import Artifact, Conversation, Message, Project, Trace
from .repos.business_repos import artifact_repo, conv_repo, message_repo, project_repo
from .schemas import (
    AutoStartReq,
    ConversationResp,
    CreateConversationReq,
    CreateProjectReq,
    MessageResp,
    ProjectResp,
    RenameReq,
    SearchItemResp,
)
from .security import CurrentUser, get_current_user
logger = logging.getLogger("business.projects")


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
    if proj is None or not proj.is_public or proj.deleted_at is not None:
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
    # 软删除: 仅打 deleted_at 标志, 保留全部关联数据(对话/消息/产物/向量库/统计),
    # 前端与列表查询据此过滤不显示; 同时撤销公开分享, 避免已隐藏项目仍可经分享链接访问。
    # 行保留以维持统计与历史记录的连续性(硬删会影响统计与其他流程)。
    await project_repo.update(
        db, proj,
        deleted_at=datetime.utcnow(),
        is_public=False,
        share_id=None,
    )
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
    conv = await conv_repo.create(db, project_id=req.project_id, user_id=user.id, name=req.name or "新对话")
    await cache_invalidate(f"conv:list:{req.project_id}:*")
    return conv


@router.post("/auto-start", status_code=201)
async def auto_start(
    req: AutoStartReq,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """首条对话无项目时: 按对话文本自动创建一个项目 + 首个会话(同名),返回两者。

    前端在未选中任何项目时发起首条对话调用本接口,再用返回的 conversation_id 走 /api/chat。
    项目名与会话名均取对话文本(截断到 128 字符,保留可读性)。
    """
    name = req.text.strip()[:128] or "未命名项目"
    proj = await project_repo.create(db, user_id=user.id, name=name)
    conv = await conv_repo.create(db, project_id=proj.id, user_id=user.id, name=name)
    await cache_invalidate(f"conv:list:{proj.id}:*")
    return {
        "project": ProjectResp.model_validate(proj),
        "conversation": ConversationResp.model_validate(conv),
    }
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
    return await conv_repo.update(db, conv, name=req.name)


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
        select(Project).where(
            Project.user_id == user.id,
            Project.deleted_at.is_(None),
            Project.name.like(like),
        )
    )).scalars().all()
    for p in projs:
        results.append(SearchItemResp(type="project", id=p.id, title=p.name, project_id=None))
    convs = (await db.execute(
        select(Conversation).where(Conversation.user_id == user.id, Conversation.name.like(like))
    )).scalars().all()
    for c in convs:
        results.append(SearchItemResp(
            type="conversation", id=c.id,
            title=c.name or "(未命名会话)", project_id=c.project_id,
        ))
    return results


# ---------- 消息内容深度搜索 ----------
@router.get("/search/messages")
async def search_messages(
    q: str = Query(..., min_length=1),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """搜索消息内容: 匹配用户消息 + AI 回复, 返回相匹配的 Q&A 对以及上下文(项目名/会话标题)."""
    like = f"%{q}%"
    # 查找匹配的 user 消息(仅搜索用户发送的内容, 避免匹配过于泛化)
    rows = (await db.execute(
        select(Message, Conversation.name, Conversation.project_id, Project.name)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .join(Project, Conversation.project_id == Project.id)
        .where(
            Conversation.user_id == user.id,
            Project.deleted_at.is_(None),
            Message.role == "user",
            Message.content.like(like),
        )
        .order_by(Message.created_at.desc())
        .limit(30)
    )).all()

    results = []
    for msg, conv_name, project_id, project_name in rows:
        # 找紧随其后的第一条 AI 回复(AI 对这条问题的回答)
        ai_reply_row = (await db.execute(
            select(Message.content)
            .where(
                Message.conversation_id == msg.conversation_id,
                Message.role == "assistant",
                Message.id > msg.id,
            )
            .order_by(Message.id.asc())
            .limit(1)
        )).first()
        ai_reply = ai_reply_row[0] if ai_reply_row else ""

        # 截断过长的内容用于列表展示
        user_snippet = _fix_content(msg.content)[:120]
        ai_snippet = _fix_content(ai_reply)[:120]

        results.append({
            "message_id": msg.id,
            "conversation_id": msg.conversation_id,
            "project_id": project_id,
            "project_name": project_name or "",
            "conv_title": conv_name or "(未命名)",
            "user_text": user_snippet,
            "ai_reply": ai_snippet,
            "created_at": msg.created_at.isoformat() if msg.created_at else "",
        })

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


@router.get("/conversations/{conversation_id}/status")
async def get_conversation_status(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """会话状态查询: 前端刷新后据此判断生成中/已完成/空闲, 恢复 UI 状态。

    优先级: 活跃 Trace(RUNNING) > 断点暂停 > 产物落库(已完成) > 空闲。
    """
    # ① 查活跃 Trace: 是否有进行中的生成
    active_trace = (await db.execute(
        select(Trace).where(
            Trace.conversation_id == conversation_id,
            Trace.user_id == user.id,
            Trace.status == "processing",
        ).order_by(Trace.started_at.desc()).limit(1)
    )).scalar_one_or_none()

    if active_trace:
        return {
            "status": "processing",
            "active_trace_id": active_trace.trace_id,
            "started_at": active_trace.started_at.isoformat() if active_trace.started_at else None,
        }

    # ② 查断点暂停
    from .cache import ck_get
    try:
        ck = await ck_get(conversation_id)
        if ck and ck.get("status") == "paused":
            return {
                "status": "paused",
                "stage": ck.get("stage", "?"),
            }
    except Exception:
        pass

    # ③ 查产物落库: 有 artifact → 已完成
    try:
        conv = await conv_repo.get_by(db, id=conversation_id, user_id=user.id)
        if conv:
            count = (await db.execute(
                select(Artifact).where(Artifact.project_id == conv.project_id)
            )).scalars().all()
            if len(count) > 0:
                return {"status": "done", "has_artifacts": True}
    except Exception:
        pass

    return {"status": "idle", "has_artifacts": False}


def _doc_to_markdown(doc: dict) -> str:
    """把旧格式(或任意)需求文档字典转成可读 Markdown, 避免下载文件扩展名与内容格式不一致。"""
    lines: list[str] = []
    brand = doc.get("brand") or {}
    if isinstance(brand, dict):
        name = brand.get("name") or "需求文档"
        lines.append(f"# {name}")
        if brand.get("slogan"):
            lines.append(f"> {brand['slogan']}")
        if brand.get("intro"):
            lines += ["", brand["intro"]]
    else:
        lines.append("# 需求文档")
    if doc.get("target_user"):
        lines += ["", "## 目标用户", "", str(doc["target_user"])]
    pages = doc.get("pages") or []
    if pages:
        lines += ["", "## 页面结构", ""]
        for p in pages:
            if not isinstance(p, dict):
                lines.append(f"- {p}")
                continue
            lines.append(f"### {p.get('title', '页面')}")
            for s in (p.get("sections") or []):
                if isinstance(s, dict):
                    lines.append(f"- **{s.get('name', '')}**: {s.get('content', '')}")
    feats = doc.get("features") or []
    if feats:
        lines += ["", "## 功能清单", ""]
        for f in feats:
            lines.append(f"- {f}")
    if doc.get("design_style"):
        lines += ["", "## 设计风格", "", str(doc["design_style"])]
    cs = doc.get("color_scheme") or {}
    if isinstance(cs, dict) and cs:
        lines.append("")
        lines.append("色值: " + "  ".join(f"{k}={v}" for k, v in cs.items()))
    return "\n".join(lines)


@router.get("/projects/{project_id}/requirement-doc")
async def download_requirement_doc(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """下载需求文档。统一返回 Markdown(.md): 含 PM 详细报告(report)则用报告正文, 否则把结构化文档转成可读 Markdown。模型原始输出(raw_llm_output)一并拼接。requirement_doc 落库于 projects 表, 未单独传 COS。"""
    import json
    from fastapi.responses import Response

    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    doc = proj.requirement_doc
    if not doc:
        raise HTTPException(status_code=404, detail="no requirement doc")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except Exception:
            pass
    # 模型原始输出(若有)——让下载文件不再单调, 一并返回文本结果
    raw_out = ""
    if isinstance(doc, dict) and isinstance(doc.get("raw_llm_output"), str) and doc["raw_llm_output"].strip():
        raw_out = doc["raw_llm_output"]
    # 统一返回 Markdown(.md): 有 report 用报告正文, 否则把结构化文档转成可读 Markdown
    if isinstance(doc, dict) and isinstance(doc.get("report"), str) and doc["report"].strip():
        text = doc["report"]
    elif isinstance(doc, dict):
        text = _doc_to_markdown(doc)
    else:
        text = json.dumps(doc, ensure_ascii=False, indent=2)
    if raw_out:
        text = text.rstrip() + "\n\n---\n\n## 原始生成内容（模型原始输出）\n\n```text\n" + raw_out + "\n```\n"
    filename = f"requirement_doc_{project_id}.md"
    media = "text/markdown; charset=utf-8"
    return Response(
        content=text,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/projects/{project_id}/retry-upload")
async def retry_upload(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重传 COS 失败的产物: 找 status=uploading 的 artifact, 读本地 HTML 重新上传, 更新 DB。
    单进程合并后直接读本地产物目录并调 COS(不再经 httpx 转发到独立 AI 服务)。"""
    import os
    from pathlib import Path

    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    artifacts = await artifact_repo.list_by(db, project_id=project_id, status="uploading")
    results = []
    art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
    for art in artifacts:
        if not art.trace_id:
            continue
        idx = art_dir / "anon" / art.trace_id / "index.html"
        if not idx.exists():
            results.append({"id": art.id, "ok": False, "error": "本地产物文件不存在"})
            continue
        try:
            from ..agent.tools.cos_upload import cos_upload
            cos_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/anon/{art.trace_id}/index.html"
            res = cos_upload(str(idx), cos_key)
            if res.get("ok"):
                art.preview_url = res["url"]
                art.download_url = res["url"]
                art.status = "done"
                await db.commit()
                results.append({"id": art.id, "ok": True, "url": res["url"]})
            else:
                results.append({"id": art.id, "ok": False, "error": res.get("error", "COS 上传失败")})
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


# ---- 消息游标分页(前端 localStorage 缓存 + 上拉加载) ----
@router.get("/projects/{project_id}/messages")
async def list_messages(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    before_id: int | None = Query(None, description="游标: 加载此 id 之前的消息"),
    limit: int = Query(10, ge=1, le=50),
):
    """游标分页获取项目消息(跨所有会话)。"""
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    conv_ids = (await db.execute(
        select(Conversation.id).where(Conversation.project_id == project_id)
    )).scalars().all()
    if not conv_ids:
        return []
    q = select(Message).where(Message.conversation_id.in_(conv_ids))
    if before_id is not None:
        q = q.where(Message.id < before_id)
    q = q.order_by(Message.id.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    result = [{"id": r.id, "conversation_id": r.conversation_id,
               "role": r.role, "content": r.content,
               "trace_id": r.trace_id, "created_at": str(r.created_at) if r.created_at else None}
              for r in reversed(rows)]
    return result


# ---- Project System Prompt ----
@router.get("/projects/{project_id}/prompt")
async def get_project_prompt(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取项目级 System Prompt。"""
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"project_id": project_id, "system_prompt": proj.system_prompt or ""}


@router.put("/projects/{project_id}/prompt")
async def update_project_prompt(
    project_id: int,
    body: dict,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """追加或替换项目级 System Prompt。"""
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    mode = body.get("mode", "append")  # append | replace
    content = body.get("content", "")
    if mode == "replace":
        proj.system_prompt = content[:4000]
    else:
        existing = proj.system_prompt or ""
        proj.system_prompt = (existing + "\n" + content)[:4000]
    await db.commit()
    return {"ok": True, "len": len(proj.system_prompt or "")}


# ---------- 删除产物(高危操作) ----------
@router.delete("/projects/{project_id}/artifacts")
async def delete_all_artifacts(
    project_id: int,
    confirmed: bool = Query(False),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除项目下所有产物(需 confirmed=true 确认)。"""
    if not confirmed:
        raise HTTPException(status_code=400, detail="高频操作需 confirmed=true 确认")
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    count = await artifact_repo.delete_all(db, project_id=project_id)
    logger.info("已删除项目 %s 的全部 %s 个产物", project_id, count)
    # 清除 site_generated 缓存, 防止删除后 cascade 仍认为站点已生成导致空弹窗
    try:
        conversations = await conv_repo.list_by(db, project_id=project_id)
        for c in conversations:
            await cache_delete(f"site_generated:{c.id}")
    except Exception:
        pass
    return {"ok": True, "deleted": count}


@router.delete("/projects/{project_id}/artifacts/files")
async def delete_single_file(
    project_id: int,
    name: str = Query(..., description="要删除的文件名(如 index.html)"),
    confirmed: bool = Query(False),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除项目中特定文件的产物记录(需 confirmed=true 确认)。仅允许删除自家项目文件。"""
    if not confirmed:
        raise HTTPException(status_code=400, detail="请确认后再执行")
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    deleted = await artifact_repo.delete_file(db, project_id=project_id, filename=name)
    if not deleted:
        return {"ok": True, "deleted": 0, "note": f"未找到文件 {name}"}
    logger.info("已删除项目 %s 的文件 %s", project_id, name)
    return {"ok": True, "deleted": 1, "name": name}
