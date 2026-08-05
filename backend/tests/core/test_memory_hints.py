"""memory_hints 并入记忆抽取结果的纯函数单测（不触 DB / 不触向量库）。

直接测 ``app.core.memory_write._merge_hints``：它把状态机确定性产出的结构化提示
并入 LLM 抽取结果。落在 LLM 抽取**之后**，保证这些信号确定性落库，不依赖 LLM 是否
从自由文本自行推断出相同事实。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.memory_hints import merge_hints as _merge_hints


def _empty_extraction() -> dict:
    return {
        "user_facts": [],
        "user_prefs": [],
        "project_facts": [],
        "project_exps": [],
        "session_summary": {"title": "", "body": "", "highlights": []},
        "qc": {},
    }


def test_user_fact_hint_appended():
    ext = _empty_extraction()
    _merge_hints(
        ext,
        [{"kind": "user_fact", "category": "preference",
          "key_name": "city", "value": "上海", "confidence": 95}],
        project_id=None,
    )
    assert len(ext["user_facts"]) == 1
    assert ext["user_facts"][0]["key_name"] == "city"
    assert ext["user_facts"][0]["confidence"] == 95


def test_user_pref_hint_appended():
    ext = _empty_extraction()
    _merge_hints(
        ext,
        [{"kind": "user_pref", "tag": "site_type",
          "content": "偏好网站类型：电商", "weight": 60}],
        project_id=None,
    )
    assert len(ext["user_prefs"]) == 1
    assert ext["user_prefs"][0]["tag"] == "site_type"
    assert ext["user_prefs"][0]["weight"] == 60


def test_project_scoped_hints_skipped_without_project():
    ext = _empty_extraction()
    _merge_hints(
        ext,
        [
            {"kind": "project_fact", "category": "status", "key_name": "phase", "value": "built"},
            {"kind": "project_exp", "title": "X", "body": "Y", "payload": {}},
        ],
        project_id=None,
    )
    # 无项目上下文时，项目级提示被丢弃（不会污染会话级记忆）。
    assert ext["project_facts"] == []
    assert ext["project_exps"] == []


def test_project_scoped_hints_appended_with_project():
    ext = _empty_extraction()
    _merge_hints(
        ext,
        [
            {"kind": "project_fact", "category": "status", "key_name": "phase", "value": "built"},
            {"kind": "project_exp", "title": "建站承接前情", "body": "Z",
             "payload": {"continuation_source": "t1"}},
        ],
        project_id=42,
    )
    assert len(ext["project_facts"]) == 1
    assert ext["project_facts"][0]["value"] == "built"
    assert len(ext["project_exps"]) == 1
    assert ext["project_exps"][0]["title"] == "建站承接前情"


def test_none_hints_is_noop():
    ext = _empty_extraction()
    _merge_hints(ext, None, project_id=1)
    assert ext["user_facts"] == [] and ext["user_prefs"] == []


def test_unknown_kind_ignored():
    ext = _empty_extraction()
    _merge_hints(ext, [{"kind": "bogus", "foo": "bar"}], project_id=1)
    assert ext["user_facts"] == [] and ext["user_prefs"] == []
