"""目标资源「执行前」前置条件校验 —— 单一真相源。

为什么存在：
  指向既有资源的意图（edit / review / publish / trash / restore / purge）必须在使用
  户态闸门阶段（S5，且在创建审批卡 / S6 落地之前）就验明目标**真实存在且就绪**，
  而不是等到审批通过、进 ops 执行时才报“项目不存在 / 站点未建成”——那等于把
  “前置条件不满足”变成“执行中途报错”，毫无闸门意义。

  同时，执行器（ops.execute / publish）仍应保留 last-mile 防御（并发锁、final check、
  防绕过 S5 直接调用），但判定规则从本模块取，**S5 与 ops 共用同一套字面与语义**，
  不再各写一份导致漂移。

公开 API：
  - SITE_READY_STATES      站点可操作的就绪状态白名单（与 preview 对齐）。
  - PROJECT_BLOCKED_STATES 项目处于这些状态时禁止一切操作（目前仅 purging）。
  - load_project(...)      按 id + user_id 取项目（鉴权），可选 for_update 锁。
  - project_blocked_reason(project)  返回阻塞原因码 / None。
  - has_ready_artifact(...) 目标 project 下是否存在任一就绪站点 Artifact。
  - targeted_action_guard(...) 统一判定“指向既有资源的意图”的前置条件。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.models import Artifact, Project

logger = logging.getLogger(__name__)

# 站点可操作的就绪状态（与 preview.py:_PREVIEWABLE 对齐，单一来源）。
SITE_READY_STATES = ("verified", "preview_ready")
# 项目处于这些状态时禁止一切操作（purging 中表示正在永久删除流程中）。
PROJECT_BLOCKED_STATES = ("purging",)


async def load_project(session, project_id: int, user_id: int, *, lock: bool = False):
    """按 id + user_id 取项目（顺带鉴权），可选 for_update 锁。返回 Project 或 None。

    None 表示“不存在或越权”——调用方用 ``project_blocked_reason`` 统一区分。
    """
    stmt = select(Project).where(Project.id == project_id, Project.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_ready_artifact(session, project_id: int, *, states=SITE_READY_STATES) -> bool:
    """目标 project 下是否存在任一就绪站点 Artifact（指向既有站点的意图用）。"""
    row = await session.scalar(
        select(Artifact.id)
        .where(Artifact.project_id == project_id, Artifact.status.in_(states))
        .limit(1)
    )
    return row is not None


def project_blocked_reason(project: "Project | None") -> "str | None":
    """项目不可操作的原因码；None 表示可操作。

    注意：project 为 None 也归为 'project_not_found'（调用方无需区分“不存在”与“越权”，
    对用户统一提示“不存在或无权访问”）。
    """
    if project is None:
        return "project_not_found"
    if project.status in PROJECT_BLOCKED_STATES:
        return "project_purging"
    return None


async def targeted_action_guard(session, project_id: int, user_id: int, speech_act: str):
    """执行前统一前置校验：目标项目存在 + 鉴权 + 未阻塞，并按意图补验就绪资源。

    Args:
        session:      数据库会话。
        project_id:   目标项目 id（已由上层解析）。
        user_id:      当前用户 id（鉴权）。
        speech_act:   意图动作值（edit / review / publish / trash / restore / purge …）。
    Returns:
        ``(ok, code, text)``：
          - ok=True  → 前置满足，可执行；
          - ok=False → 前置不满足，code/text 供 S5 打回或 ops 直接 failed。

    设计：本函数只负责“给定 speech_act 需要什么资源”的判定，不自行决定哪些意图
    需要校验——范围由调用方（S5._TARGET_REQUIREMENTS）决定，避免两处职责纠缠。
    """
    project = await load_project(session, project_id, user_id)
    blocked = project_blocked_reason(project)
    if blocked is not None:
        if blocked == "project_not_found":
            return False, "project_not_found", "目标项目不存在或无权访问。"
        return False, "project_purging", "项目正在永久删除中，无法执行该操作。"
    # SITE 类 edit/review 除项目可操作外，还需已建成站点。
    if speech_act in ("edit", "review"):
        ready = await has_ready_artifact(session, project_id)
        if not ready:
            return False, "target_site_missing", (
                "我没能找到可以操作的已生成网站（项目可能尚未建好或已被清除）。"
                "是要新建一个网站，还是切换/指定某个已有的项目？"
            )
    return True, "ok", ""
