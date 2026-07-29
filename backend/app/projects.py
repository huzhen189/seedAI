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
from shared.artifacts import repo_path, trash_dir
logger = logging.getLogger("business.projects")


def _move_artifacts_to_trash(user_id: int, project_id: int) -> bool:
    """删除项目时把整站产物目录(含 git 仓库)物理移动到回收区。

    - 目标: {ARTIFACT_DIR}/.trash/{project_id}_{ts}/
    - 失败静默不抛(不影响软删主流程): 仅记日志, 留给后续清理/运维处理。
    - 回收区目录可整体移回(.trash -> 原 uid/pid 布局)实现"恢复", 见 restore_project。
    返回 True 表示成功移动, False 表示无产物或移动失败。
    """
    import shutil

    src = repo_path(user_id, project_id)
    if not src.exists():
        logger.info("删除项目 %s 无本地产物目录, 跳过回收", project_id)
        return False
    from datetime import datetime as _dt
    ts = _dt.utcnow().strftime("%Y%m%d%H%M%S")
    dest_root = trash_dir()
    dest = dest_root / f"{project_id}_{ts}" / str(user_id) / str(project_id)
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        # 目标已存在(极端重名)则追加随机后缀, 避免覆盖
        if dest.exists():
            dest = dest.with_name(dest.name + "_" + uuid.uuid4().hex[:6])
        shutil.move(str(src), str(dest))
        logger.info("项目 %s 产物已移入回收区: %s", project_id, dest)
        return True
    except Exception as e:  # noqa: BLE001 - 回收失败不阻断软删
        logger.error("项目 %s 产物移入回收区失败(不影响软删): %s", project_id, e)
        return False


def _restore_artifacts_from_trash(user_id: int, project_id: int) -> bool:
    """恢复项目时把回收区中的产物目录移回原 uid/pid 布局。

    回收时布局: .trash/{project_id}_{ts}/{user_id}/{project_id}/
    移回目标:   {ARTIFACT_DIR}/{user_id}/{project_id}/
    - 取该 project 最新的回收条目(按目录名 ts 排序)。
    - 失败静默不抛(不影响取消软删主流程); 若无回收条目则 no-op 返回 False。
    """
    import shutil

    root = trash_dir()
    if not root.exists():
        return False
    # 收集匹配 {project_id}_* 的回收条目
    entries = sorted(
        (p for p in root.iterdir()
         if p.is_dir() and p.name.startswith(f"{project_id}_")),
        key=lambda p: p.name,
        reverse=True,  # 最新 ts 在前
    )
    if not entries:
        logger.info("恢复项目 %s 未找到回收区条目, 跳过", project_id)
        return False
    src = entries[0] / str(user_id) / str(project_id)
    if not src.exists():
        logger.warning("恢复项目 %s 回收条目结构异常(缺 %s), 跳过", project_id, src)
        return False
    dest = repo_path(user_id, project_id)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # 已有产物(异常并存), 不覆盖, 直接放弃本次恢复移动
            logger.warning("恢复项目 %s 目标已存在产物, 跳过移回", project_id)
            return False
        shutil.move(str(src), str(dest))
        # 若回收条目下已无其他项目目录, 顺手清理空壳 .trash/{project_id}_{ts}
        parent_entry = src.parent
        if parent_entry.exists() and not any(parent_entry.iterdir()):
            parent_entry.rmdir()
        logger.info("项目 %s 产物已从回收区移回: %s", project_id, dest)
        return True
    except Exception as e:  # noqa: BLE001 - 恢复失败不阻断软删取消
        logger.error("项目 %s 产物从回收区移回失败(不影响恢复): %s", project_id, e)
        return False


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
    # 同时把本地产物目录(含 git 仓库)物理移入回收区(.trash), 既不真删(可恢复),
    # 又能堵住"软删项目仍可被 nginx 同源直链读取"的泄露风险(配合 /api/artifacts-auth)。
    await project_repo.update(
        db, proj,
        deleted_at=datetime.utcnow(),
        is_public=False,
        share_id=None,
    )
    # 回收失败静默: 不阻断软删主流程
    _move_artifacts_to_trash(user.id, project_id)
    await cache_invalidate(f"conv:list:{project_id}:*")
    return None


@router.post("/projects/{project_id}/restore", response_model=ProjectResp)
async def restore_project(
    project_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """取消软删除: 清除 deleted_at 标志, 项目重新出现在列表; 同时把回收区中的产物目录移回原布局(可恢复)。"""
    proj = await project_repo.get_by(db, id=project_id, user_id=user.id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    if proj.deleted_at is None:
        return proj  # 原本未删, 幂等返回
    # 取消软删(不恢复分享状态——公开分享需用户重新主动开启, 更安全)
    updated = await project_repo.update(db, proj, deleted_at=None)
    # 回收区产物移回: 失败静默不阻断主流程
    _restore_artifacts_from_trash(user.id, project_id)
    await cache_invalidate(f"conv:list:{project_id}:*")
    return updated


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
@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 修复 GET /api/conversations/{id} 返回 405(原仅定义 PATCH/DELETE, 本路由装饰器在 v2.2.0 重构时丢失)。
    # get_by(id, user_id) 同时做归属校验: 不存在或非本人 -> 404。
    # 注意: 手动构造 dict 返回, 不走 ConversationResp.model_validate——其 messages 字段用了
    # validation_alias="_messages_disabled", from_attributes 读取 ORM 的 messages 关系会触发
    # pydantic "no field messages" 错误。消息列表由前端走专用 GET /api/conversations/{id}/messages 加载。
    conv = await conv_repo.get_by(db, id=conversation_id, user_id=user.id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {
        "id": conv.id,
        "project_id": conv.project_id,
        "user_id": conv.user_id,
        "name": conv.name,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
    }


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
            Trace.status == "running",
        ).order_by(Trace.started_at.desc()).limit(1)
    )).scalar_one_or_none()

    if active_trace:
        # 返回 Trace 真实状态(而非硬编码 running): 配合启动孤儿对账, 被强杀后残留的
        # 孤儿 Trace 已翻 aborted, 此处自然命中不到; 真实在途任务才是 running。
        return {
            "status": active_trace.status,
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
        # P1: 优先按 preview_path 定位本地产物(相对 ARTIFACT_DIR), 兼容旧 anon/<trace> 布局
        if art.preview_path:
            idx = art_dir / art.preview_path
        elif art.trace_id:
            idx = art_dir / "anon" / art.trace_id / "index.html"
        else:
            results.append({"id": art.id, "ok": False, "error": "无可用本地路径"})
            continue
        if not idx.exists():
            results.append({"id": art.id, "ok": False, "error": "本地产物文件不存在"})
            continue
        try:
            from ..agent.tools.cos_upload import cos_upload
            from shared.artifacts import cos_key_for, to_rel_path
            # COS key 与本地相对路径同规则(previews 前缀), 发布即直传同 key
            if art.preview_path:
                cos_key = "previews/" + art.preview_path
            elif art.trace_id:
                cos_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/anon/{art.trace_id}/index.html"
            else:
                cos_key = ""
            if not cos_key:
                results.append({"id": art.id, "ok": False, "error": "无可用 COS key"})
                continue
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
