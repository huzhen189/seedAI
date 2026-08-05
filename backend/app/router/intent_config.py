"""S2 意图识别的配置真相源（触发词表 + 槽位映射）。

此前这些词表/映射是 ``intent.py`` 里的硬编码 tuple，改一个词就要动代码 + 重启。
现在统一收敛到同目录的 ``intent_config.json``，本模块负责加载并把 JSON 结构
转换成 ``intent.py`` 期望的 tuple 形状（零改调用方取值方式）。

热更新：``reload_intent_config()`` 重新读取 JSON，改词后无需重启进程即可生效
（如需文件变更自动热加载，可在进程内挂 watchdog 或暴露 admin 端点调用本函数）。
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

_CONFIG_PATH: Final[Path] = Path(__file__).with_name("intent_config.json")


def _load_raw() -> dict:
    with _CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# 模块级缓存：intent.py 在导入时引用这些名字，reload 时整体替换。
_raw = _load_raw()
_tw = _raw["trigger_words"]
_slot = _raw["slot_maps"]

SITE_WORDS: Final[tuple[str, ...]] = tuple(_tw["site"])
RESEARCH_WORDS: Final[tuple[str, ...]] = tuple(_tw["research"])
PUBLISH_WORDS: Final[tuple[str, ...]] = tuple(_tw["publish"])
PURGE_WORDS: Final[tuple[str, ...]] = tuple(_tw["purge"])
RESTORE_WORDS: Final[tuple[str, ...]] = tuple(_tw["restore"])
TRASH_WORDS: Final[tuple[str, ...]] = tuple(_tw["trash"])
EDIT_WORDS: Final[tuple[str, ...]] = tuple(_tw["edit"])
CREATE_WORDS: Final[tuple[str, ...]] = tuple(_tw["create"])
SOCIAL_WORDS: Final[tuple[str, ...]] = tuple(_tw["social"])

# 槽位映射：tuple[tuple[tuple[str, ...], str], ...] —— 与旧 _THEME_MAP 等形状一致。
_THEME_MAP: Final[tuple[tuple[tuple[str, ...], str], ...]] = tuple(
    (tuple(e["words"]), e["value"]) for e in _slot["theme"]
)
_SITE_TYPE_MAP: Final[tuple[tuple[tuple[str, ...], str], ...]] = tuple(
    (tuple(e["words"]), e["value"]) for e in _slot["site_type"]
)
_SECTION_MAP: Final[tuple[tuple[tuple[str, ...], str], ...]] = tuple(
    (tuple(e["words"]), e["value"]) for e in _slot["section"]
)
_DEPLOY_MAP: Final[tuple[tuple[tuple[str, ...], str], ...]] = tuple(
    (tuple(e["words"]), e["value"]) for e in _slot.get("deploy_target", [])
)
_STYLE_WORDS: Final[tuple[str, ...]] = tuple(_raw["style_words"])


def reload_intent_config() -> None:
    """重新读取 intent_config.json，刷新模块级词表/映射（热更新入口）。

    失败时保留旧配置并告警，绝不因配置错误让意图识别崩溃。
    """
    global SITE_WORDS, RESEARCH_WORDS, PUBLISH_WORDS, PURGE_WORDS, RESTORE_WORDS
    global TRASH_WORDS, EDIT_WORDS, CREATE_WORDS, SOCIAL_WORDS
    global _THEME_MAP, _SITE_TYPE_MAP, _SECTION_MAP, _STYLE_WORDS
    try:
        raw = _load_raw()
        tw = raw["trigger_words"]
        slot = raw["slot_maps"]
        SITE_WORDS = tuple(tw["site"])
        RESEARCH_WORDS = tuple(tw["research"])
        PUBLISH_WORDS = tuple(tw["publish"])
        PURGE_WORDS = tuple(tw["purge"])
        RESTORE_WORDS = tuple(tw["restore"])
        TRASH_WORDS = tuple(tw["trash"])
        EDIT_WORDS = tuple(tw["edit"])
        CREATE_WORDS = tuple(tw["create"])
        SOCIAL_WORDS = tuple(tw["social"])
        _THEME_MAP = tuple((tuple(e["words"]), e["value"]) for e in slot["theme"])
        _SITE_TYPE_MAP = tuple((tuple(e["words"]), e["value"]) for e in slot["site_type"])
        _SECTION_MAP = tuple((tuple(e["words"]), e["value"]) for e in slot["section"])
        _DEPLOY_MAP = tuple((tuple(e["words"]), e["value"]) for e in slot.get("deploy_target", []))
        _STYLE_WORDS = tuple(raw["style_words"])
        logger.info("[intent_config] 热更新成功：%d 组触发词 + %d 类槽位映射",
                    len(tw), len(slot))
    except Exception as exc:  # noqa: BLE001 — 配置错误不能中断识别链路
        logger.warning("[intent_config] 热更新失败，沿用旧配置: %s", exc)


__all__ = [
    "SITE_WORDS", "RESEARCH_WORDS", "PUBLISH_WORDS", "PURGE_WORDS", "RESTORE_WORDS",
    "TRASH_WORDS", "EDIT_WORDS", "CREATE_WORDS", "SOCIAL_WORDS",
    "_THEME_MAP", "_SITE_TYPE_MAP", "_SECTION_MAP", "_DEPLOY_MAP", "_STYLE_WORDS",
    "reload_intent_config",
]
