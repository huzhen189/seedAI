"""规则模块(SIR 重写): 五维硬信号 + 热更新词表, 零延迟, 不调 LLM。

输出 RuleResult {keywords, pattern, confidence, industry, signals}
  signals = {lexical, verb, completeness, behavior, score}
    - lexical:       关键词密度归一化(0~1)
    - verb:          动作动词强度(强=1 / 中=0.5 / 弱=0.15)
    - completeness:  约束条件计数归一化(0~1)
    - behavior:      行为/项目信号(有需求文档/已建站 → 高)
    - score:         四维特加权总分(0~1), 供 SIR 信念融合

词表 / 权重 / 阈值外置到 ruleset.json, 支持热更新(mtime 轮询, reload 失败回滚)。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from .common import VALID_INDUSTRIES  # noqa: E402

logger = logging.getLogger("ai_service.intent.rules")

_RULESET_PATH = os.path.join(os.path.dirname(__file__), "ruleset.json")

# ── 失败回滚默认(与 ruleset.json 一致) ──
_DEFAULT_WEIGHTS = {"lexical": 0.30, "verb": 0.30, "completeness": 0.30, "behavior": 0.10}
_DEFAULT_RULESET = {
    "weights": _DEFAULT_WEIGHTS,
    "lexical_keywords": ["网站", "网页", "页面", "官网", "建", "开发", "搭建", "前端", "html", "css"],
    "verb_strong": ["做", "开发", "搭建", "实现", "生成", "创建", "帮我做", "给我做", "重构", "修复"],
    "verb_mid": ["想要", "需要", "计划", "考虑", "想", "希望", "打算", "帮我", "请帮我"],
    "verb_weak": ["了解", "咨询", "问问", "怎么样", "难不难", "为什么", "如何", "怎么", "区别"],
    "constraint_patterns": {
        "page_count": [r"\d+\s*个?\s*(页|页面)"],
        "tech_stack": ["react", "vue", "wordpress", "html", "css", "小程序"],
        "feature": ["表单", "导航", "搜索", "评论", "支付", "地图", "登录", "注册", "购物车"],
        "audience": ["b端", "c端", "企业", "公司", "个人", "团队", "政府"],
        "deadline": ["这周", "本周", "尽快", "明天", "下周"],
    },
    "thresholds": {"commit": 0.70, "clarify": 0.40, "clarify_max_rounds": 2},
}

_cache: dict = {"data": _DEFAULT_RULESET, "mtime": 0.0}


def load_ruleset(force: bool = False) -> dict:
    """加载/热重载规则集。失败回滚到 _DEFAULT_RULESET(不改变 _cache['mtime'])。"""
    global _cache
    try:
        mtime = os.path.getmtime(_RULESET_PATH)
    except OSError:
        return _cache["data"]
    if not force and mtime == _cache["mtime"]:
        return _cache["data"]
    try:
        with open(_RULESET_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("weights"), dict):
            raise ValueError("ruleset.weights 缺失或类型错误")
        for k in ("lexical_keywords", "verb_strong", "verb_mid", "verb_weak", "constraint_patterns", "thresholds"):
            if k not in data:
                raise ValueError(f"ruleset 缺字段 {k}")
        _cache = {"data": data, "mtime": mtime}
        logger.info("[规则] 热加载 ruleset.json 成功 weights=%s", data.get("weights"))
    except Exception as e:
        logger.warning("[规则] ruleset 加载失败, 回滚默认: %s", e)
    return _cache["data"]


@dataclass
class RuleResult:
    keywords: list[str] = field(default_factory=list)
    pattern: str = ""          # "build" | "chat" | "" (兼容旧聚合器冲突检测)
    confidence: float = 0.5    # 兼容旧字段 = signals.score
    industry: str = "other"
    signals: dict = field(default_factory=dict)  # {lexical, verb, completeness, behavior, score}


def _last_user_message(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "") or ""
            return c if isinstance(c, str) else ""
    return ""


def _industry_of(t: str) -> str:
    """行业探测(稳定, 内联; 不随热更新变动)。"""
    mapping = [
        ("餐饮", ("餐饮", "餐厅", "饭店", "美食", "外卖", "菜单", "restaurant")),
        ("电商", ("电商", "商城", "购物", "商品", "订单", "支付", "店铺", "ecommerce")),
        ("教育", ("教育", "课程", "培训", "学校", "学生", "edu")),
        ("医疗", ("医疗", "医院", "诊所", "医生", "挂号", "预约", "健康", "health")),
        ("游戏", ("游戏", "game", "小游戏")),
        ("企业", ("企业", "公司", "corp", "官方", "品牌", "集团")),
        ("个人", ("个人", "博客", "简历", "作品集", "portfolio", "personal")),
        ("金融", ("金融", "银行", "保险", "理财", "证券", "基金", "finance")),
        ("政务", ("政务", "政府", "公安", "社保", "税务", "审批", "gov")),
        ("旅游", ("旅游", "酒店", "景点", "攻略", "机票", "民宿", "travel")),
        ("科技", ("科技", "tech", "saas", "ai", "人工智能", "物联网")),
        ("媒体", ("媒体", "视频", "直播", "新闻", "公众号", "media")),
    ]
    for ind, kws in mapping:
        if any(w in t for w in kws):
            return ind
    return "other"


def run_rules(messages: list[dict], project_status: str = "draft",
              has_requirement_doc: bool = False,
              has_conversation_requirement: bool = False) -> RuleResult:
    """五维硬信号计算(零延迟, 不调LLM)。"""
    rs = load_ruleset()
    weights = rs["weights"]
    last = _last_user_message(messages)
    if not last.strip():
        logger.info("[规则] 输入为空→跳过")
        return RuleResult()
    t = last.lower()

    # ── 1. lexical: 关键词密度 ──
    lex_kw = rs["lexical_keywords"]
    hits = [w for w in lex_kw if w in t]
    lexical = min(len(hits) / 2.0, 1.0)

    # ── 2. verb: 动词强度 ──
    verb = 0.15  # 默认弱
    if any(w in t for w in rs["verb_strong"]):
        verb = 1.0
    elif any(w in t for w in rs["verb_mid"]):
        verb = 0.5
    elif any(w in t for w in rs["verb_weak"]):
        verb = 0.15

    # ── 3. completeness: 约束条件计数 ──
    constraint_cats = rs["constraint_patterns"]
    matched_cats = 0
    for cat, pats in constraint_cats.items():
        if cat == "page_count":
            if any(re.search(p, t) for p in pats):
                matched_cats += 1
        else:
            if any(p in t for p in pats):
                matched_cats += 1
    completeness = min(matched_cats / 3.0, 1.0)

    # ── 4. behavior: 项目/行为信号 ──
    if has_requirement_doc:
        behavior = 1.0
    elif has_conversation_requirement:
        behavior = 0.7
    elif project_status in ("building", "done", "review"):
        behavior = 0.8
    else:
        behavior = 0.3

    # ── 加权总分 ──
    wsum = sum(weights.get(k, 0.0) for k in ("lexical", "verb", "completeness", "behavior")) or 1.0
    score = (
        weights.get("lexical", 0.3) * lexical
        + weights.get("verb", 0.3) * verb
        + weights.get("completeness", 0.3) * completeness
        + weights.get("behavior", 0.1) * behavior
    ) / wsum
    score = max(0.0, min(1.0, score))

    # 兼容字段: pattern(方向) + industry
    pattern = "build" if (lexical >= 0.5 or verb >= 0.5) else "chat"
    industry = _industry_of(t)
    if industry not in VALID_INDUSTRIES:
        industry = "other"

    logger.info("[规则] lexical=%.2f verb=%.2f complete=%.2f beh=%.2f → score=%.2f pattern=%s ind=%s",
                lexical, verb, completeness, behavior, score, pattern, industry)
    return RuleResult(
        keywords=hits,
        pattern=pattern,
        confidence=round(score, 3),
        industry=industry,
        signals={
            "lexical": round(lexical, 3),
            "verb": round(verb, 3),
            "completeness": round(completeness, 3),
            "behavior": round(behavior, 3),
            "score": round(score, 3),
        },
    )
