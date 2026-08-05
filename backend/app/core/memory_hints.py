"""记忆提示（memory_hints）并入逻辑 —— 纯函数、零外部依赖。

``memory_hints`` 是状态机（``app.core.transition.plan_round``）确定性产出的
「本轮值得沉淀为长期记忆的结构化线索」，与 LLM 自由文本抽取互补：LLM 抽的是
弱信号，状态机给的是强结构化事实（如「本任务承接自哪段前情」「用户偏好的网站类型」）。

本模块刻意**不 import 任何 app 内部重依赖**（llm / ragstore / db），以便单测时
无需 openai / numpy 即可直接导入验证。
"""
from __future__ import annotations

from typing import Any


def merge_hints(
    extraction: dict[str, Any],
    hints: list[dict[str, Any]] | None,
    project_id: int | None,
) -> None:
    """把状态机确定性产出的 memory_hints 并入 LLM 抽取结果（就地修改 ``extraction``）。

    在 ``llm_extract`` 之后调用，确保这些结构化信号**确定性**落库，而不依赖 LLM
    是否从自由文本中自行推断出相同事实。hint 种类：
      - ``user_fact``   → 并入 user_facts（强事实，幂等 UPSERT）；
      - ``user_pref``   → 并入 user_prefs（软偏好，仅 rerank）；
      - ``project_fact``→ 仅当 project_id 存在时并入 project_facts；
      - ``project_exp`` → 仅当 project_id 存在时并入 project_exps（落 memories 行）。
    未知种类或字段缺失静默忽略（防御：避免坏数据让整批记忆写失败）。
    """
    if not hints:
        return
    for h in hints:
        if not isinstance(h, dict):
            continue
        kind = h.get("kind")
        if kind == "user_fact":
            extraction["user_facts"].append(
                {
                    "category": str(h.get("category", "preference")),
                    "key_name": str(h.get("key_name", "unknown")),
                    "value": str(h.get("value", "")),
                    "confidence": int(h.get("confidence", 90)),
                }
            )
        elif kind == "user_pref":
            content = h.get("content")
            if not content:
                continue
            extraction["user_prefs"].append(
                {
                    "tag": str(h.get("tag", "general")),
                    "content": str(content),
                    "weight": int(h.get("weight", 50)),
                }
            )
        elif kind == "project_fact":
            if project_id is None:
                continue
            extraction["project_facts"].append(
                {
                    "category": str(h.get("category", "status")),
                    "key_name": str(h.get("key_name", "unknown")),
                    "value": str(h.get("value", "")),
                }
            )
        elif kind == "project_exp":
            if project_id is None:
                continue
            extraction["project_exps"].append(
                {
                    "title": str(h.get("title", ""))[:40],
                    "body": str(h.get("body", "")),
                    "payload": h.get("payload") or {},
                }
            )


__all__ = ["merge_hints"]
