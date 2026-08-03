"""意图 / 动作的中文展示标签（前端 ThinkingTrail 唯一真相源）。

为什么放后端而不是前端硬编码：
  意图 id 由 ``Domain × SpeechAct`` 组合而成（见 ``router/intent.py``），未来新增域或新增
  speech_act 时只需改这里一处；前端只负责渲染下发的 ``label``，不再维护第二份映射表
  （历史上前端各自映射过一版，改后端就漏改前端，线上出现英文 intent_id 直出）。

对外只暴露 ``intent_label()`` / ``plan_item_payload()`` / ``intent_payload()`` 三个函数，
分别服务于：S2 的「识别到的意图」列表、S4 的「执行计划」列表、S6 的子任务事件。
"""
from __future__ import annotations

from typing import Any

# 组合级精确文案：优先命中，读起来像「人话」而不是「域·动作」的机械拼接。
_INTENT_LABELS: dict[str, str] = {
    "chat_ask": "对话答疑",
    "chat_discuss": "交流讨论",
    "site_create": "构建网站",
    "site_edit": "修改网站",
    "site_review": "检查网站",
    "research_ask": "资料检索",
    "research_discuss": "资料讨论",
    "project_publish": "发布上线",
    "project_trash": "移入回收站",
    "project_restore": "恢复项目",
    "project_purge": "彻底删除",
    "project_review": "查看项目",
}

# 兜底拼接用的分量词典（新增枚举但忘了配组合文案时，仍然是中文而不是英文 id）。
_DOMAIN_LABELS: dict[str, str] = {
    "chat": "对话",
    "site": "网站",
    "research": "检索",
    "project": "项目",
}

_SPEECH_LABELS: dict[str, str] = {
    "ask": "答疑",
    "discuss": "讨论",
    "create": "创建",
    "edit": "修改",
    "review": "检查",
    "confirm_pending_action": "确认操作",
    "cancel": "取消",
    "publish": "发布",
    "trash": "移入回收站",
    "restore": "恢复",
    "purge": "彻底删除",
}


def intent_label(intent_id: str, domain: str = "", speech_act: str = "") -> str:
    """把 ``chat_ask`` 之类的内部 id 翻成中文短语。

    三级兜底：精确组合 → ``域·动作`` 拼接 → 原样返回 id（保证永不返回空串）。
    """
    hit = _INTENT_LABELS.get(intent_id)
    if hit:
        return hit
    dom = _DOMAIN_LABELS.get(domain, "")
    act = _SPEECH_LABELS.get(speech_act, "")
    if dom and act:
        return f"{dom}·{act}"
    return dom or act or intent_id or "处理请求"


def intent_payload(item: Any) -> dict[str, Any]:
    """``ResolvedIntent`` → SSE 用的意图字典（S2 阶段事件 / done 事件复用）。"""
    domain = item.domain.value
    speech_act = item.speech_act.value
    intent_id = item.intent_id
    return {
        "domain": domain,
        "intent_id": intent_id,
        "speech_act": speech_act,
        "label": intent_label(intent_id, domain, speech_act),
        "executable": bool(item.executable),
    }


def plan_item_payload(action: Any, status: str = "pending") -> dict[str, Any]:
    """``ActionItem`` → SSE 用的执行计划条目。

    ``id`` 与 S6 ``task`` 事件的 ``task_id`` 同源（都是 ActionItem.id），前端据此把
    子任务实时状态回填到计划列表对应行，不需要额外的关联键。
    """
    domain = action.domain.value
    speech_act = action.speech_act.value
    intent_id = action.intent_id
    return {
        "id": action.id,
        "domain": domain,
        "intent_id": intent_id,
        "speech_act": speech_act,
        "label": intent_label(intent_id, domain, speech_act),
        "status": status,
    }
