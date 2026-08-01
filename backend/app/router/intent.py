"""S2/S4 的确定性优先结构化意图理解。"""

from __future__ import annotations

from app.core.contracts import (
    ActionItem,
    BoundedPlan,
    Domain,
    IntentBundle,
    IntentItem,
    RiskLevel,
    SpeechAct,
    TargetRef,
    TargetType,
    UnderstandingResult,
    UtteranceFrame,
)


SITE_WORDS = ("网站", "网页", "页面", "首页", "落地页", "官网", "组件", "导航")
RESEARCH_WORDS = ("搜索", "研究", "调研", "查一下", "资料", "趋势")
PUBLISH_WORDS = ("发布", "上线", "部署")
PURGE_WORDS = ("彻底删除", "清空项目", "purge")
TRASH_WORDS = ("删除", "回收", "移除")
EDIT_WORDS = ("修改", "改成", "调整", "优化", "替换")
CREATE_WORDS = ("创建", "生成", "做一个", "搭建", "帮我做")


def understand(message: str) -> UnderstandingResult:
    text = message.lower()
    domain = Domain.CHAT
    speech = SpeechAct.ASK
    executable = False
    target = TargetRef()
    risk = RiskLevel.LOW

    if any(word in text for word in PURGE_WORDS):
        domain, speech, executable, target, risk = Domain.PROJECT, SpeechAct.PURGE, True, TargetRef(type=TargetType.PROJECT), RiskLevel.CRITICAL
    elif any(word in text for word in PUBLISH_WORDS):
        domain, speech, executable, target, risk = Domain.PROJECT, SpeechAct.PUBLISH, True, TargetRef(type=TargetType.ARTIFACT), RiskLevel.CRITICAL
    elif any(word in text for word in RESEARCH_WORDS):
        domain, speech, executable = Domain.RESEARCH, SpeechAct.ASK, True
    elif any(word in text for word in SITE_WORDS):
        domain, target = Domain.SITE, TargetRef(type=TargetType.PROJECT)
        if any(word in text for word in EDIT_WORDS):
            speech, executable = SpeechAct.EDIT, True
        elif any(word in text for word in CREATE_WORDS):
            speech, executable = SpeechAct.CREATE, True
        else:
            speech = SpeechAct.DISCUSS
    elif any(word in text for word in TRASH_WORDS):
        domain, speech, executable, target, risk = Domain.PROJECT, SpeechAct.TRASH, True, TargetRef(type=TargetType.PROJECT), RiskLevel.HIGH

    intent_id = f"{domain.value}_{speech.value}"
    candidate = IntentItem(
        id="i1",
        domain=domain,
        speech_act=speech,
        intent_id=intent_id,
        target=target,
        arguments={"message": message},
        confidence=0.9,
        executable=executable,
        risk_hint=risk,
    )
    return UnderstandingResult(
        utterance_frame=UtteranceFrame(domain_hint=domain, speech_act=speech, target=target, executable=executable, confidence=0.9),
        intent_candidates=[],
        needs_clarification=False,
    ).model_copy(update={"intent_candidates": []})


def classify(message: str, understanding: UnderstandingResult) -> tuple[IntentBundle, BoundedPlan]:
    frame = understanding.utterance_frame
    intent = IntentItem(
        id="i1",
        domain=frame.domain_hint or Domain.CHAT,
        speech_act=frame.speech_act or SpeechAct.ASK,
        intent_id=f"{(frame.domain_hint or Domain.CHAT).value}_{(frame.speech_act or SpeechAct.ASK).value}",
        target=frame.target,
        arguments={"message": message},
        confidence=frame.confidence,
        executable=frame.executable,
        risk_hint=RiskLevel.CRITICAL if frame.speech_act in {SpeechAct.PUBLISH, SpeechAct.PURGE} else RiskLevel.HIGH if frame.speech_act == SpeechAct.TRASH else RiskLevel.LOW,
    )
    bundle = IntentBundle(primary_id=intent.id, items=[intent])
    actions: list[ActionItem] = []
    if intent.executable:
        actions.append(ActionItem(id="a1", intent_id=intent.intent_id, domain=intent.domain, speech_act=intent.speech_act, target=intent.target, arguments=intent.arguments))
    return bundle, BoundedPlan(action_items=actions)
