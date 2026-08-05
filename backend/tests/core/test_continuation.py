"""Continuation 解析器单元测试（确定性、零 LLM、可验证）。

覆盖：
  - 用户原场景：前情「买雨伞好还是买雨衣好」→ 当前「做一个网站，根据天气推荐今天用哪个」
    → 应 references，summary 承接雨伞/雨衣讨论，target_slots=["site.brief"]。
  - 无 gist → independent。
  - choice 承接在「多前情且最近一条非选项」时，精准命中较早的选项轮（C 的 disambiguation）。
  - 纯回指、无字面/choice 信号 → 回落链接最近前情（confidence 0.7）。
  - 无关新话题（无回指、无重叠）→ independent（不误连）。
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.continuation import Continuation, resolve_continuation


def _gist_umbrella(turn_id: str = "t_umb") -> dict:
    return {
        "turn_id": turn_id,
        "role": "user",
        "summary": "用户问买雨伞好还是买雨衣好",
        "content": "买雨伞好还是买雨衣好",
    }


def test_user_scenario_references():
    gist = [_gist_umbrella("t1")]
    msg = "那我想做一个网站，根据地理位置实时天气推荐今天应该用哪个"
    c = resolve_continuation(msg, gist)
    assert c.relation == "references"
    assert c.source_turn_id == "t1"
    assert c.summary and "雨伞" in c.summary and "雨衣" in c.summary
    assert c.target_slots == ["site.brief"]
    assert c.confidence >= 0.7


def test_no_gist_is_independent():
    c = resolve_continuation("随便聊聊", [])
    assert c.relation == "independent"
    assert c.target_slots == []


def test_no_message_is_independent():
    c = resolve_continuation("", [_gist_umbrella()])
    assert c.relation == "independent"


def test_choice_link_beats_recency():
    # 最近一条是不相干的「查北京天气」，较早一条是含选项的「买雨伞好还是买雨衣好」。
    # choice 承接信号应精准命中较早的选项轮，而非最近的无关轮（C 的 disambiguation）。
    gist = [
        {"turn_id": "t_recent", "role": "user",
         "summary": "查下北京明天天气", "content": "查下北京明天天气"},
        _gist_umbrella("t_umb"),
    ]
    msg = "那用哪个"
    c = resolve_continuation(msg, gist)
    assert c.relation == "references"
    assert c.source_turn_id == "t_umb"  # 命中选项轮，而非最近的天气轮
    assert "雨伞" in (c.summary or "")


def test_anaphora_fallback_links_most_recent():
    gist = [
        {"turn_id": "t_recent", "role": "user",
         "summary": "我们刚才聊了项目进度", "content": "我们刚才聊了项目进度"},
    ]
    msg = "那个继续说"
    c = resolve_continuation(msg, gist)
    assert c.relation == "references"
    assert c.source_turn_id == "t_recent"
    assert c.confidence == 0.7
    assert c.target_slots == ["site.brief"]


def test_unrelated_new_topic_is_independent():
    gist = [_gist_umbrella("t1")]
    # 无回指词、无字面重叠、无 choice 信号 → 不应误连。
    msg = "帮我查下明天北京天气怎么样"
    c = resolve_continuation(msg, gist)
    assert c.relation == "independent"


def test_cap_limits_candidates():
    # 超过 cap 的更早前情不参与评分。
    gist = [_gist_umbrella(f"t{i}") for i in range(10)]
    # 最近一条(cap 内)命中，第 6 条（超出 cap=5）即便有选项也不应被选。
    gist[5] = {"turn_id": "t_far", "role": "user",
               "summary": "用户问买雨伞好还是买雨衣好", "content": "买雨伞好还是买雨衣好"}
    msg = "那用哪个"
    c = resolve_continuation(msg, gist, cap=5)
    assert c.relation == "references"
    assert c.source_turn_id == "t0"  # 命中 cap 内最近一条(t0 含选项)，而非 t_far


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL CONTINUATION TESTS PASSED")
