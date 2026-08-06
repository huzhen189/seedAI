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


class _FakeSession:
    """最小替身：模拟 S5 目标校验用到的 ``scalar`` 查询。

    ``scalar_return`` 统一作为本次查询的返回值：
      - None        → 查不到目标（悬空 edit / 项目不存在）；
      - 非 None     → 查到目标（已建成站点 / 项目在库）。
    测试按场景构造对应的 plan（site_built 用 edit/review，project 用 publish/trash 等），
    S5 每次只会触发其配置表要求的一类查询，故单一返回值足够。
    """

    def __init__(self, *, scalar_return=None):
        self._scalar = scalar_return

    async def scalar(self, *args, **kwargs):
        return self._scalar


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


# ── site/edit：目标 project 无已建成站点 → 打回 ─────────────────────────
def test_s5_edit_missing_site_is_clarified():
    session = _FakeSession(scalar_return=None)
    ctx = _ctx(_round("site_edit", Domain.SITE), session=session)
    ctx.plan = _plan("site_edit", Domain.SITE, SpeechAct.EDIT)
    status = _run(ctx, session)
    assert status == StageStatus.COMPLETED
    assert ctx.validation.status == "clarify"
    assert "target_site_missing" in ctx.validation.reason_codes


# ── site/edit：目标站点存在 → 放行 ──────────────────────────────────────
def test_s5_edit_existing_site_passes():
    session = _FakeSession(scalar_return=42)
    ctx = _ctx(_round("site_edit", Domain.SITE), session=session)
    ctx.plan = _plan("site_edit", Domain.SITE, SpeechAct.EDIT)
    status = _run(ctx, session)
    assert status == StageStatus.COMPLETED
    assert ctx.validation.status == "pass"


# ── site/review：统一校验表覆盖，同样需已建成站点 ──────────────────────
def test_s5_review_missing_site_is_clarified():
    session = _FakeSession(scalar_return=None)
    ctx = _ctx(_round("site_build", Domain.SITE), session=session)
    ctx.plan = _plan("site_build", Domain.SITE, SpeechAct.REVIEW)
    status = _run(ctx, session)
    assert status == StageStatus.COMPLETED
    assert ctx.validation.status == "clarify"
    assert "target_site_missing" in ctx.validation.reason_codes


# 注：project 类高危动作（publish/trash/restore/purge）在 S5 早期即被审批闸门 PAUSED，
# 目标存在性由 project_ops.execute 执行期统一兜底，不在此校验表内（见 s5_validate.py 注释）。


# ── site/create：纯新建，不需要既有资源 → 直接放行 ─────────────────────
def test_s5_create_needs_no_target():
    session = _FakeSession(scalar_return=None)
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
