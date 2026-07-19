"""意图分类器:轻量 LLM 调用, 把用户输入归到 7 类之一。

输出: {"intent": "chat|doc|generate|modify|translate|code|unsupported", "confidence": 0.0~1.0}

prompt 约 100 token, 回复约 20 token, 耗时 < 1s。
"""

from __future__ import annotations

import json
import logging
import re

from .providers import get_chat_model, resolve_fallback_order


INTENT_SYSTEM = (
    "你是意图分类器。根据用户输入, 只返回一个 JSON, 不要额外文字。\n"
    "{\"intent\": \"chat|doc|generate|modify|translate|code|game|unsupported\", \"confidence\": 0.0~1.0}\n\n"
    "分类规则:\n"
    "- chat: 闲聊、知识问答、解释概念、日常对话\n"
    "- doc: 生成文档、产品构思、方案计划、说明、教程\n"
    "- generate: 新建网站、页面、落地页、Web 应用\n"
    "- modify: 修改/优化/调整已有网站或页面\n"
    "- translate: 翻译文本到其他语言\n"
    "- code: 编写代码片段/函数/脚本(不是完整网页)\n"
    "- game: 生成互动小游戏(贪吃蛇、打砖块、射击、2048等)\n"
    "- unsupported: 以上都不匹配\n\n"
    "用户输入: "
)

logger = logging.getLogger("ai_service.intent")


def classify(messages: list[dict], model_id: str = "hy3") -> dict:
    """分类用户意图, 返回 {intent, confidence}。失败则退化为关键词兜底。"""
    # 取最近一条用户消息
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last.strip():
        return {"intent": "chat", "confidence": 1.0}

    # 上下文捷径: 当前会话已有 HTML 输出 → 直接判 modify
    has_html = any(
        m.get("role") == "assistant" and ("<html" in (m.get("content") or "").lower())
        for m in messages
    )
    if has_html:
        return {"intent": "modify", "confidence": 0.99}

    # LLM 分类
    order = resolve_fallback_order(model_id)
    for mid in order:
        try:
            chat = get_chat_model(mid, streaming=False)
            resp = chat.invoke([
                {"role": "system", "content": INTENT_SYSTEM},
                {"role": "user", "content": last[:500]},
            ])
            raw = (resp.content or "").strip()
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            intent = data.get("intent", "")
            confidence = float(data.get("confidence", 0.5))
            if intent in (
                "chat", "doc", "generate", "modify",
                "translate", "code", "game", "unsupported",
            ):
                return {"intent": intent, "confidence": confidence}
            # 无效分类 → 关键词兜底
            break
        except Exception as e:
            logger.warning("意图分类 %s 失败: %s", mid, e)
            continue

    # 关键词兜底(分类器全失败时)
    return _keyword_fallback(last)


def _keyword_fallback(text: str) -> dict:
    t = text.lower()
    if any(w in t for w in ("翻译", "translate", "译成")):
        return {"intent": "translate", "confidence": 0.7}
    if any(w in t for w in (
        "游戏", "game", "贪吃蛇", "打砖块", "坦克大战", "射击",
        "2048", "消消乐", "弹球", "飞机大战", "小游戏",
    )):
        return {"intent": "game", "confidence": 0.7}
    if any(w in t for w in (
        "网站", "页面", "网页", "落地页", "主页", "官网",
        "site", "landing", "homepage", "web",
    )):
        return {"intent": "generate", "confidence": 0.7}
    if any(w in t for w in ("代码", "函数", "脚本", "写一个", "snippet", "编程")):
        return {"intent": "code", "confidence": 0.7}
    if any(w in t for w in (
        "文档", "计划", "方案", "构思", "说明", "教程",
        "doc", "plan", "tutorial",
    )):
        return {"intent": "doc", "confidence": 0.7}
    return {"intent": "chat", "confidence": 0.5}
