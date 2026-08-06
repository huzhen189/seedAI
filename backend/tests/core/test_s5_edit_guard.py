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
    ValidationResult,
)
from app.core.ids import new_ulid
from app.core.stages.s5_validate import S5ValidateStage
from app.core.transition import RoundPlan
from app.core.turn_context import TurnContext


class _FakeSession:
    """最小替身：仅实现 S5 edit 兜底用到的 ``scalar`` 查询。

    ``scalar_return`` 模拟 ``select(Artifact.id)...limit(1)`` 的结果：
      - None  → 目标 project 没有任何已建成 site（悬空 edit）；
      - 非 None → 查到 verified/preview_ready 的 artifact（真实 edit 目标）。
    """

    def __init__(self, scalar_return):
        self._scalar = scalar_return

    async def scalar(self, *args, **kwargs):
        return self._scalar


def _site_edit_plan() -> RoundPlan:
    task = ActiveTask(
        id="task_x",
        kind="site_edit",
        domain=Domain.SITE,
        phase="ready",
    )
    return RoundPlan(task=task, agenda=[], action="execute")


def _bounded_plan() -> BoundedPlan:
    """真实进入 S5 的前置：含一个 site+edit 的低风险 action_item。"""
    return BoundedPlan(
        action_items=[
            ActionItem(
                id="a1",
                intent_id="i1",
                domain=Domain.SITE,
                speech_act=SpeechAct.EDIT,
                target=TargetRef(),
            )
        ]
    )


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
        clean_message="改一下网站",
    )
    ctx.round_plan = round_plan
    ctx.prior_project_id = prior_project_id
    return ctx


def _safe_run(ctx: TurnContext, session) -> StageStatus:
    ctx.plan = _bounded_plan()      # 绕过 line31 的「无 action_item→NO_OP」
    ctx.intent_bundle = None
    ctx.validation = None
    ctx.response_fragments = []
    stage = S5ValidateStage(session=session)
    result = asyncio.run(stage.run(ctx))
    return result.status


def test_s5_edit_missing_target_is_clarified():
    """被判 site_edit 但目标 project 无已建成 artifact → 打回澄清，不放行。"""
    session = _FakeSession(scalar_return=None)
    ctx = _ctx(_site_edit_plan(), session=session)
    status = _safe_run(ctx, session)
    assert status == StageStatus.COMPLETED   # clarify 分支 → result COMPLETED
    assert ctx.validation is not None
    assert ctx.validation.status == "clarify", "edit 目标缺失应打回 clarify"
    assert "edit_target_missing" in ctx.validation.reason_codes
    assert any(f.status == "clarify" for f in ctx.response_fragments), \
        "应产出一条 clarify 响应片段"


def test_s5_edit_existing_target_passes():
    """被判 site_edit 且目标 project 确有已建成 artifact → 放行，S6 执行。"""
    session = _FakeSession(scalar_return=42)
    ctx = _ctx(_site_edit_plan(), session=session)
    status = _safe_run(ctx, session)
    assert status == StageStatus.COMPLETED
    assert ctx.validation is not None
    assert ctx.validation.status == "pass", "edit 目标存在应正常放行"


def test_s5_edit_no_session_does_not_crash():
    """无 db session 的退化路径：兜底分支不执行，直接低风险放行（不抛错）。"""
    ctx = _ctx(_site_edit_plan(), session=None)
    status = _safe_run(ctx, None)
    assert status == StageStatus.COMPLETED
    assert ctx.validation is not None
    assert ctx.validation.status == "pass"
