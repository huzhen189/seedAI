from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.contracts import ActionItem, BoundedPlan, Domain, IntentBundle, IntentItem, SpeechAct
from app.core.ids import new_ulid


def test_bounded_plan_rejects_more_than_three_user_actions() -> None:
    actions = [
        ActionItem(
            id=f"a{index}",
            intent_id="site_edit",
            domain=Domain.SITE,
            speech_act=SpeechAct.EDIT,
        )
        for index in range(4)
    ]
    with pytest.raises(ValidationError):
        BoundedPlan(action_items=actions)


def test_intent_bundle_requires_primary_to_reference_an_item() -> None:
    item = IntentItem(
        id="i1",
        domain=Domain.CHAT,
        speech_act=SpeechAct.ASK,
        intent_id="chat_answer",
        executable=False,
        confidence=0.9,
    )
    with pytest.raises(ValidationError, match="primary_id"):
        IntentBundle(primary_id="missing", items=[item])


def test_ulid_is_fixed_width_and_monotonic_within_a_process() -> None:
    first = new_ulid()
    second = new_ulid()
    assert len(first) == 26
    assert len(second) == 26
    assert first < second
