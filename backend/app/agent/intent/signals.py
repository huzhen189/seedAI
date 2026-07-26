"""意图控制信号(删除 / 重置等控制流短路)的单一来源, v1.2.0 收敛。

为什么独立成模块:
  - 原 delete / reset 关键词硬编码在 cascade.py 内部, 与「规则目录」理念冲突,
    调整需改代码(违反单一来源, C5)。
  - 现统一从 rules_catalog.json 的 `control_signals` 段加载(带缓存 + 内置兜底),
    与 rulesmatcher 的规则目录同处一份配置, 改词只需改 JSON。

对外:
  - is_delete_signal(text): 是否删除操作(排除「删除项目/工程」这类非生成物删除)。
  - is_reset_signal(text):  是否退出建站/澄清信号。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("ai_service.intent.signals")

_RULES_PATH = Path(__file__).resolve().parent / "rules_catalog.json"

# 兜底(与 rules_catalog.json control_signals 保持一致; JSON 缺失时启用)
_DELETE_KEYWORDS = ["删除", "删掉", "删了", "删", "移除", "清空", "去掉", "干掉"]
_DELETE_EXCLUDE = ["项目", "工程"]
_RESET_PHRASES = (
    "随便聊聊", "不用了", "聊天而已", "不做了", "当我没说", "就聊聊天",
    "只是随便问问", "不用帮我做", "不用做了", "只是问问", "当我没听见",
    "算了不做了", "不用管了", "退出建站", "取消建站", "重新开始", "重置",
    "退出", "结束", "回到聊天",
)


@lru_cache(maxsize=1)
def load_control_signals() -> dict:
    """加载 control_signals(带缓存); 失败回退内置兜底, 不影响主流程。"""
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cs = data.get("control_signals", {})
        return {
            "delete_keywords": list(cs.get("delete_keywords", _DELETE_KEYWORDS)),
            "delete_exclude": list(cs.get("delete_exclude", _DELETE_EXCLUDE)),
            "reset_phrases": list(cs.get("reset_phrases", _RESET_PHRASES)),
        }
    except Exception as e:  # pragma: no cover
        logger.error("[信号] 加载失败, 用内置兜底: %s", e)
        return {
            "delete_keywords": list(_DELETE_KEYWORDS),
            "delete_exclude": list(_DELETE_EXCLUDE),
            "reset_phrases": list(_RESET_PHRASES),
        }


def is_delete_signal(text: str) -> bool:
    """是否删除操作: 命中删除词 且 非「删除项目/工程」(项目级删除走别处)。"""
    if not text:
        return False
    sig = load_control_signals()
    hit_delete = any(k in text for k in sig["delete_keywords"])
    if not hit_delete:
        return False
    excluded = any(k in text for k in sig["delete_exclude"])
    return not excluded


def is_reset_signal(text: str) -> bool:
    """是否退出建站/澄清信号。"""
    if not text:
        return False
    sig = load_control_signals()
    return any(p in text for p in sig["reset_phrases"])
