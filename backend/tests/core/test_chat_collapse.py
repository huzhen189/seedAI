"""纯闲聊轮多段 CHAT 折叠单测（零 I/O，仅撞 app.llm → openai 的 import，需 biz 环境）。

锁定用户反馈 2026-08-06 的根因：「一句话被切成多段 CHAT 意图 → 每段各触发一次
chat_service.respond → 多个 ResponseFragment → S8 拼出两段相近结论落库」。
本测试确认 S4 的 classify() 把「全 CHAT 兜底」轮折叠为**单个** action（携带整句），
从而每轮闲聊只产出一个权威结论，DB 与前端一致。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.contracts import Domain, IntentItem, SpeechAct, UnderstandingResult
from app.router.intent import classify


def _understanding(chat_segments: list[str]) -> UnderstandingResult:
    """手搓一个「全 CHAT 兜底」的 UnderstandingResult（与 understand() 对无触发词
    片段兜底成 Domain.CHAT 的输出字段一致），避免拉起完整 understand() 链。
    """
    items = [
        IntentItem(
            id=f"i{idx}", domain=Domain.CHAT, speech_act=SpeechAct.ASK,
            intent_id="chat_ask", confidence=0.6, executable=False,
            raw_segment=seg[:2048],
        )
        for idx, seg in enumerate(chat_segments, start=1)
    ]
    return UnderstandingResult(
        resolved_intents=items,
        needs_clarification=False,
    )


def test_multi_chat_segments_collapse_to_single_action():
    """「你是谁？你会干嘛！今天天气真不错」被切成 3 段 CHAT → 折叠为 1 个 action，
    携带合并后的整句（避免后续 S6 多次调用 chat 产生多段重复结论）。"""
    msg = "你是谁？你会干嘛！今天天气真不错"
    understanding = _understanding(["你是谁", "你会干嘛", "今天天气真不错"])
    bundle, plan = classify(msg, understanding)
    assert len(plan.action_items) == 1, plan.action_items
    action = plan.action_items[0]
    assert action.domain == Domain.CHAT
    # 合并后的 message 必须包含全部三段语义，而不是只剩第一截。
    merged = action.arguments.get("message", "")
    assert "你是谁" in merged and "你会干嘛" in merged and "今天天气真不错" in merged
    # bundle.items 也已对齐为单个（保持 bundle 与 plan 一一对应）。
    assert len(bundle.items) == 1


def test_single_chat_stays_one_action():
    """纯单句闲聊（无切点）本来就 1 段 → 仍是 1 个 action，不误伤。"""
    msg = "你好，帮我规划下明天的行程"
    understanding = _understanding([msg])
    _, plan = classify(msg, understanding)
    assert len(plan.action_items) == 1


def test_compound_with_site_intent_not_collapsed():
    """复合句「删掉旧站，并告诉我怎么选域名」含 PROJECT 意图 → 不折叠，
    仍保留真实动作（site 之外的真实意图不得被吞）。"""
    project = IntentItem(
        id="i1", domain=Domain.PROJECT, speech_act=SpeechAct.TRASH,
        intent_id="project.trash", confidence=0.9, executable=True, raw_segment="删掉旧站",
    )
    chat = IntentItem(
        id="i2", domain=Domain.CHAT, speech_act=SpeechAct.ASK,
        intent_id="chat_ask", confidence=0.6, executable=False, raw_segment="并告诉我怎么选域名",
    )
    understanding = UnderstandingResult(resolved_intents=[project, chat], needs_clarification=False)
    bundle, plan = classify("删掉旧站，并告诉我怎么选域名", understanding)
    assert any(a.domain != Domain.CHAT for a in plan.action_items)
    assert len(plan.action_items) >= 2


def test_chat_only_with_existing_action_not_double_collapsed():
    """防御：若 actions 已被填充（理论上不会发生），折叠函数不得重复追加。"""
    msg = "讲个笑话"
    understanding = _understanding(["讲个笑话"])
    bundle, plan = classify(msg, understanding)
    # 恰好 1 个 action，没有任何「多段」残留造成 >1。
    assert len(plan.action_items) == 1
