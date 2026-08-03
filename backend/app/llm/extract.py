"""记忆写入 LLM 提取（固定 Schema 输出，仅 JSON，不落库）。

见 docs/plan-memory-v2-landing.md §2.2 / §4。职责单一：把本轮 user+assistant 压缩提炼为
固定结构 JSON——LLM **绝不**生成 SQL、绝不直连向量库（失控防护，红线#4）。落库动作 100%
在代码侧（见 app/core/memory_write.py）。

压缩格式要求（标题/正文分离 + 多意图分存）：
  - 本轮会话摘要 session_summary 拆成 精简标题 title（≤40 字，作向量索引）+ 压缩正文 body；
  - project_exps 列表内每段独立成 memories 行（各自 title/body），多意图按意图分段；
  - user_facts / project_facts 为结构化强事实（零容错）；user_prefs 为软偏好（仅 rerank）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.llm import LLMError, chat_completion

logger = logging.getLogger("app.llm.extract")

# LLM 输出契约（非 DB 模型）。所有字段可空/可空列表，解析层做防御。
EXTRACTION_SCHEMA: dict[str, Any] = {
    "user_facts": [
        {"category": "preference|taboo|permission|geo", "key_name": str, "value": str, "confidence": int}
    ],
    "user_prefs": [
        {"tag": str, "content": str, "weight": int}
    ],
    "project_facts": [
        {"category": "stack|version|domain|constraint|status", "key_name": str, "value": str}
    ],
    "project_exps": [
        {"kind": str, "title": str, "body": str, "payload": dict}
    ],
    "session_summary": {
        "title": str,
        "body": str,
        "highlights": [str],
    },
}

_SYSTEM = (
    "你是记忆提取器。阅读本轮对话（用户消息 + 助手回复），提炼为结构化记忆 JSON。\n"
    "严格要求：\n"
    "1. 只输出一个 JSON 对象，字段固定为 user_facts / user_prefs / project_facts / "
    "project_exps / session_summary；不要输出任何解释、不要使用 Markdown 代码块包裹。\n"
    "2. user_facts：用户强事实（城市/禁忌/偏好/权限等确定性信息），零容错；"
    "category ∈ {preference,taboo,permission,geo}，key_name 简短键名，value 具体值，confidence 0-100。\n"
    "3. project_facts：项目强事实（技术栈/版本/域名/约束/状态）；"
    "category ∈ {stack,version,domain,constraint,status}。\n"
    "4. user_prefs：模糊/场景化软偏好（如'科技风偏好深色背景'），tag 为场景标签，weight 0-100；"
    "这些不进入强事实，仅用于后续召回重排。\n"
    "5. project_exps：项目过程经验，每段独立（多意图必须分段）；title 为≤40字精简标题，"
    "body 为压缩正文（200字内）；kind 标识类型（如 build/edit/publish/error）。\n"
    "6. session_summary：本轮会话摘要；title 为≤40字精简标题（将作为向量索引），"
    "body 为压缩正文（承接相邻轮次的要点，200字内）。\n"
    "7. 没有对应内容时该字段给空列表/空对象，不要编造。\n"
    "8. 不要在 summary/body 里复述原始长文，必须压缩。"
)


def _empty_extraction() -> dict[str, Any]:
    return {
        "user_facts": [],
        "user_prefs": [],
        "project_facts": [],
        "project_exps": [],
        "session_summary": {"title": "", "body": "", "highlights": []},
    }


def _coerce(raw: Any) -> dict[str, Any]:
    """把 LLM 返回（可能含 code fence / 多余文本）规范成固定结构，缺失字段补默认。"""
    if not isinstance(raw, dict):
        return _empty_extraction()
    out = _empty_extraction()
    out["user_facts"] = raw.get("user_facts") or []
    out["user_prefs"] = raw.get("user_prefs") or []
    out["project_facts"] = raw.get("project_facts") or []
    out["project_exps"] = raw.get("project_exps") or []
    ss = raw.get("session_summary") or {}
    if isinstance(ss, dict):
        out["session_summary"] = {
            "title": str(ss.get("title") or ""),
            "body": str(ss.get("body") or ""),
            "highlights": ss.get("highlights") or [],
        }
    return out


def _strip_fence(text: str) -> str:
    """去掉可能的 ```json ... ``` 包裹。"""
    s = text.strip()
    if s.startswith("```"):
        # 去掉首行 ```json 与结尾 ```
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


async def llm_extract(
    *,
    user_text: str,
    assistant_text: str,
    project_id: int | None = None,
    conversation_id: int | None = None,
) -> dict[str, Any]:
    """调用一次 LLM 抽取本轮记忆，返回固定结构 dict（已规范/补默认）。

    失败/异常一律返回空结构（fail-soft），不抛错——记忆提取是增强项，绝不可反噬主链路。
    """
    if not user_text and not assistant_text:
        return _empty_extraction()

    user_block = f"【用户消息】\n{user_text}\n" if user_text else ""
    assistant_block = f"【助手回复】\n{assistant_text}\n" if assistant_text else ""
    scope = ""
    if project_id is not None:
        scope += f"\n当前项目 id={project_id}。"
    if conversation_id is not None:
        scope += f"\n当前会话 id={conversation_id}。"
    messages = [
        {"role": "system", "content": _SYSTEM + scope},
        {"role": "user", "content": user_block + assistant_block + "\n请输出记忆 JSON。"},
    ]
    try:
        raw_text = await chat_completion(messages, temperature=0.2, max_tokens=1024, timeout=30.0)
    except LLMError as exc:
        logger.warning("[extract] LLM 调用失败，跳过记忆提取: %s", exc)
        return _empty_extraction()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[extract] 记忆提取异常(已忽略): %s", exc, exc_info=True)
        return _empty_extraction()

    try:
        parsed = json.loads(_strip_fence(raw_text))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("[extract] LLM 返回非 JSON，忽略: %s", exc)
        return _empty_extraction()
    return _coerce(parsed)


__all__ = ["EXTRACTION_SCHEMA", "llm_extract"]
