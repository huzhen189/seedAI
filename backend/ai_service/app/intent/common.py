"""意图模块共享常量与校验(单一来源)。

rules.py / semantic.py 原先各自重复定义 VALID_LEVEL1/2/INDUSTRIES,
抽到此处避免漂移(§五-4)。
"""

from __future__ import annotations

# 有效的意图值域(单一来源)
VALID_LEVEL1 = frozenset({"learn", "code", "build", "doc", "translate", "unsupported"})
VALID_LEVEL2 = frozenset({
    "explain", "debug", "compare", "casual",
    "snippet", "component", "fix", "refactor",
    "page", "site", "modify", "game",
    "readme", "tutorial", "plan",
    "text", "code_lang", "design", "search",
})
VALID_INDUSTRIES = frozenset({
    "restaurant", "ecommerce", "gov", "edu", "health",
    "finance", "game", "personal", "corp", "tech", "media", "other", "none",
})

# 旧版意图名 → 新版 (level1, level2)
OLD_TO_LEVELS = {
    "build_site": ("build", "site"),
    "build_page": ("build", "page"),
    "code_snippet": ("code", "snippet"),
    "learn_explain": ("learn", "explain"),
    "learn_casual": ("learn", "casual"),
}


def is_valid_level1(v: str) -> bool:
    return v in VALID_LEVEL1


def is_valid_level2(v: str) -> bool:
    return v in VALID_LEVEL2


def is_valid_industry(v: str) -> bool:
    return v in VALID_INDUSTRIES


def normalize_industry(v: str | None) -> str:
    """行业字段归一化: 非法值 → other。"""
    if not v or v not in VALID_INDUSTRIES:
        return "other"
    return v
