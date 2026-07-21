"""规则模块: 关键词/格式校验, 零延迟, 不调LLM。

输出 RuleResult {keywords, pattern, confidence, industry}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("ai_service.intent.rules")

# 有效的值域
VALID_LEVEL1 = frozenset({"learn", "code", "build", "doc", "translate", "unsupported"})
VALID_LEVEL2 = frozenset({
    "explain", "debug", "compare", "casual",
    "snippet", "component", "fix", "refactor",
    "page", "site", "modify", "game",
    "readme", "tutorial", "plan",
    "text", "code_lang",
})
VALID_INDUSTRIES = frozenset({
    "restaurant", "ecommerce", "gov", "edu", "health",
    "finance", "game", "personal", "corp", "tech", "media", "other", "none",
})

OLD_TO_LEVELS = {
    "build_site": ("build", "site"),
    "build_page": ("build", "page"),
    "code_snippet": ("code", "snippet"),
    "learn_explain": ("learn", "explain"),
    "learn_casual": ("learn", "casual"),
}


@dataclass
class RuleResult:
    keywords: list[str] = field(default_factory=list)
    pattern: str = ""
    confidence: float = 0.7
    industry: str = "other"


def run_rules(messages: list[dict]) -> RuleResult:
    """规则模块入口: 关键词+格式检测, 不调LLM。"""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last.strip():
        return RuleResult()

    t = last.lower()

    # 行业探测
    industry = "other"
    if any(w in t for w in ("餐饮", "餐厅", "饭店", "美食", "外卖", "菜单", "restaurant")):
        industry = "restaurant"
    elif any(w in t for w in ("电商", "商城", "购物", "商品", "订单", "支付", "店铺", "ecommerce")):
        industry = "ecommerce"
    elif any(w in t for w in ("教育", "课程", "培训", "学校", "学生", "edu")):
        industry = "edu"
    elif any(w in t for w in ("医疗", "医院", "诊所", "医生", "挂号", "健康", "health")):
        industry = "health"
    elif any(w in t for w in ("游戏", "game", "小游戏")):
        industry = "game"
    elif any(w in t for w in ("企业", "公司", "corp", "官方")):
        industry = "corp"
    elif any(w in t for w in ("个人", "博客", "简历", "作品集", "portfolio", "personal")):
        industry = "personal"
    elif any(w in t for w in ("金融", "银行", "保险", "finance")):
        industry = "finance"
    elif any(w in t for w in ("旅游", "酒店", "景点", "travel")):
        industry = "travel"
    elif any(w in t for w in ("科技", "tech", "SaaS", "AI")):
        industry = "tech"
    elif any(w in t for w in ("媒体", "媒体", "视频", "直播", "media")):
        industry = "media"

    # 意图关键词匹配
    _L1 = lambda: ""  # noqa: E731
    keywords = []

    # build 关键词
    build_kw = ["建", "做", "生成", "写", "开发", "创建", "网站", "网页", "页面", "html",
                "前端", "首页", "导航栏", "页脚", "响应式", "设计一个", "帮我做", "给我做"]
    code_kw = ["代码", "函数", "组件", "修复", "bug", "报错", "error", "fix", "改一下", "改下",
               "优化", "重构", "评审", "review"]
    learn_kw = ["是什么", "怎么", "如何", "教程", "学习", "概念", "介绍", "对比", "区别",
                "你好", "嗨", "hello", "hi", "谢谢", "天气", "温度", "聊", "?"
               ]

    if any(w in t for w in build_kw):
        keywords.extend([w for w in build_kw if w in t])
        if any(w in t for w in ("修改", "改", "modify")):
            return RuleResult(keywords=keywords, pattern="build", confidence=0.7, industry=industry)
        return RuleResult(keywords=keywords, pattern="build", confidence=0.7, industry=industry)

    if any(w in t for w in code_kw):
        keywords.extend([w for w in code_kw if w in t])
        if any(w in t for w in ("修复", "bug", "报错", "error", "fix")):
            return RuleResult(keywords=keywords, pattern="code", confidence=0.7, industry=industry)
        return RuleResult(keywords=keywords, pattern="code", confidence=0.7, industry=industry)

    if any(w in t for w in learn_kw):
        keywords.extend([w for w in learn_kw if w in t])
        return RuleResult(keywords=keywords, pattern="learn", confidence=0.7, industry=industry)

    if any(w in t for w in ("翻译", "translate")):
        keywords.append("翻译")
        return RuleResult(keywords=keywords, pattern="translate", confidence=0.7, industry=industry)

    return RuleResult(keywords=keywords, pattern="learn", confidence=0.5, industry=industry)
