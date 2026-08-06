from __future__ import annotations

import asyncio

from app.core.contracts import (
    ActionItem,
    ActiveTask,
    BoundedPlan,
    Domain,
    SessionInfo,
    SpeechAct,
    StageStatus,
    TargetRef,
    UserIdentity,
)
from app.core.ids import new_ulid
from app.core.stages.s5_validate import S5ValidateStage
from app.core.transition import RoundPlan
from app.core.turn_context import TurnContext


class _FakeProject:
    """标量返回为“存在”时，guard 需要的是带 .status 的 Project 替身。"""

    def __init__(self, status="active"):
        self.status = status


class _FakeResult:
    """guard 用 ``session.execute(stmt).scalar_one_or_none()``，这里包一层。"""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """最小替身：模拟 S5/guard 目标校验用到的两类 DB 查询。

      - load_project（execute().scalar_one_or_none）：按 project_exists 返回
        _FakeProject（带 .status）或 None（项目不存在/越权）；
      - has_ready_artifact（scalar）：直接返回 site_ready（已建成站点是否存在）。

    两者独立，正好覆盖“项目在但站点未建成”这类 target_site_missing 场景。
    """

    def __init__(self, *, project_exists: bool = True, project_status: str = "active",
                 site_ready: bool = True):
        self._project_exists = project_exists
        self._project_status = project_status
        self._site_ready = site_ready

    async def execute(self, *args, **kwargs):
        value = _FakeProject(self._project_status) if self._project_exists else None
        return _FakeResult(value)

    async def scalar(self, *args, **kwargs):
        # 生产里 has_ready_artifact 用 scalar 取 artifact.id（int）或 None；
        # 用 _site_ready 决定返回具体 id 还是 None。
        return 1 if self._site_ready else None

    def add(self, *args, **kwargs):
        # 审批卡创建路径会调用；测试只验证到 PAUSED，无需真实落库。
        return None

    async def flush(self, *args, **kwargs):
        return None


def _plan(kind: str, domain: Domain, speech: SpeechAct, *, target_id: str | None = None) -> BoundedPlan:
    return BoundedPlan(action_items=[
        ActionItem(
            id="a1",
            intent_id="i1",
            domain=domain,
            speech_act=speech,
            target=TargetRef(id=target_id) if target_id else TargetRef(),
        )
    ])


def _round(kind: str, domain: Domain) -> RoundPlan:
    return RoundPlan(task=ActiveTask(id="t", kind=kind, domain=domain), agenda=[], action="execute")


def _ctx(round_plan: RoundPlan, *, prior_project_id=1, session=None) -> TurnContext:
    ctx = TurnContext(
        schema_version="1.0",
        trace_id="guard-trace",
        stream_id=new_ulid(),
        turn_id=new_ulid(),
        client_msg_id="cm-1",
        run_epoch=0,
        fencing_token="fence-0",
        user=UserIdentity(user_id=1),
        session=SessionInfo(conversation_id=1, project_id=prior_project_id),
        clean_message="操作一下",
    )
    ctx.round_plan = round_plan
    ctx.prior_project_id = prior_project_id
    return ctx


def _run(ctx: TurnContext, session) -> StageStatus:
    ctx.intent_bundle = None
    ctx.validation = None
    ctx.response_fragments = []
    stage = S5ValidateStage(session=session)
    return asyncio.run(stage.run(ctx)).status


# ── site/edit：项目在但无已建成站点 → 打回 target_site_missing ─────────
def test_s5_edit_missing_site_is_clarified():
    session = _FakeSession(project_exists=True, site_ready=False)
    ctx = _ctx(_round("site_edit", Domain.SITE), session=session)
    ctx.plan = _plan("site_edit", Domain.SITE, SpeechAct.EDIT)
    status = _run(ctx, session)
    assert status == StageStatus.COMPLETED
    assert ctx.validation.status == "clarify"
    assert "target_site_missing" in ctx.validation.reason_codes


# ── site/edit：项目在且站点已建成 → 放行 ───────────────────────────────
def test_s5_edit_existing_site_passes():
    session = _FakeSession(project_exists=True, site_ready=True)
    ctx = _ctx(_round("site_edit", Domain.SITE), session=session)
    ctx.plan = _plan("site_edit", Domain.SITE, SpeechAct.EDIT)
    status = _run(ctx, session)
    assert status == StageStatus.COMPLETED
    assert ctx.validation.status == "pass"


# ── site/review：统一校验表覆盖，同样需已建成站点 ──────────────────────
def test_s5_review_missing_site_is_clarified():
    session = _FakeSession(project_exists=True, site_ready=False)
    ctx = _ctx(_round("site_build", Domain.SITE), session=session)
    ctx.plan = _plan("site_build", Domain.SITE, SpeechAct.REVIEW)
    status = _run(ctx, session)
    assert status == StageStatus.COMPLETED
    assert ctx.validation.status == "clarify"
    assert "target_site_missing" in ctx.validation.reason_codes


# ── project/publish：目标项目不存在 → 执行前（审批卡创建之前）即被打回 ──
def test_s5_publish_missing_project_is_clarified():
    session = _FakeSession(project_exists=False)
    ctx = _ctx(_round("site_build", Domain.PROJECT), session=session)
    ctx.plan = _plan("site_build", Domain.PROJECT, SpeechAct.PUBLISH)
    status = _run(ctx, session)
    assert status == StageStatus.COMPLETED
    assert ctx.validation.status == "clarify"
    assert "project_not_found" in ctx.validation.reason_codes


# ── project/trash：目标项目存在 → 通过前置校验（后续进审批闸门 PAUSED）──
def test_s5_trash_existing_project_passes_target_check():
    session = _FakeSession(project_exists=True, site_ready=True)
    ctx = _ctx(_round("site_build", Domain.PROJECT), session=session)
    ctx.plan = _plan("site_build", Domain.PROJECT, SpeechAct.TRASH)
    status = _run(ctx, session)
    # 前置满足后继续往下走：trash 高危 → 进入审批闸门（PAUSED），
    # 无论哪种，都不会带 project_not_found / target_* 打回。
    assert status in (StageStatus.PAUSED, StageStatus.COMPLETED)
    assert ctx.validation is not None
    assert ctx.validation.status not in ("clarify",)
    assert not any(c.startswith("project_not_found") or c.startswith("target_") for c in (ctx.validation.reason_codes or []))


# ── site/create：纯新建，不需要既有资源 → 直接放行 ─────────────────────
def test_s5_create_needs_no_target():
    session = _FakeSession(project_exists=False, site_ready=False)
    ctx = _ctx(_round("site_build", Domain.SITE), session=session)
    ctx.plan = _plan("site_build", Domain.SITE, SpeechAct.CREATE)
    status = _run(ctx, session)
    assert status == StageStatus.COMPLETED
    assert ctx.validation.status == "pass"


# ── 无 session 退化：不抛错，直接放行（旧行为保持） ───────────────────
def test_s5_edit_no_session_does_not_crash():
    ctx = _ctx(_round("site_edit", Domain.SITE), session=None)
    ctx.plan = _plan("site_edit", Domain.SITE, SpeechAct.EDIT)
    status = _run(ctx, None)
    assert status == StageStatus.COMPLETED
    assert ctx.validation.status == "pass"
