"""规则模块: 关键词/格式校验, 零延迟, 不调LLM。

输出 RuleResult {keywords, pattern, confidence, industry}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("ai_service.intent.rules")

# 有效值域统一来自 intent/common(单一来源, 避免与 semantic.py 重复定义)
from .common import VALID_LEVEL1, VALID_LEVEL2, VALID_INDUSTRIES, OLD_TO_LEVELS  # noqa: E402


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
        logger.info("[规则] 输入为空→跳过")
        return RuleResult()

    t = last.lower()
    logger.info("[规则] 检测 input=%.60s", last)

    # 行业探测
    industry = "other"
    if any(w in t for w in ("餐饮", "餐厅", "饭店", "美食", "外卖", "菜单", "restaurant")):
        industry = "restaurant"
    elif any(w in t for w in ("电商", "商城", "购物", "商品", "订单", "支付", "店铺", "ecommerce")):
        industry = "ecommerce"
    elif any(w in t for w in ("教育", "课程", "培训", "学校", "学生", "edu")):
        industry = "edu"
    elif any(w in t for w in ("医疗", "医院", "诊所", "医生", "挂号", "预约", "健康", "health")):
        industry = "health"
    elif any(w in t for w in ("游戏", "game", "小游戏")):
        industry = "game"
    elif any(w in t for w in ("企业", "公司", "corp", "官方", "品牌", "集团")):
        industry = "corp"
    elif any(w in t for w in ("个人", "博客", "简历", "作品集", "portfolio", "personal")):
        industry = "personal"
    elif any(w in t for w in ("金融", "银行", "保险", "理财", "证券", "基金", "finance")):
        industry = "finance"
    elif any(w in t for w in ("政务", "政府", "公安", "社保", "税务", "审批", "gov")):
        industry = "gov"
    elif any(w in t for w in ("旅游", "酒店", "景点", "攻略", "机票", "民宿", "travel")):
        industry = "travel"
    elif any(w in t for w in ("科技", "tech", "saas", "ai", "人工智能", "物联网")):
        industry = "tech"
    elif any(w in t for w in ("媒体", "视频", "直播", "新闻", "公众号", "media")):
        industry = "media"

    # 意图关键词匹配
    keywords = []

    # build 关键词(建站/页面/前端)
    # 注意: 不含过泛单字「写/做/生成/创建」(会与 doc/code/translate 撞车且 build 先判,
    # 导致"写文档"误判为 build); 改用组合词(做一个/帮我做/搭建/搞一个) + 名词关键词保召回。
    build_kw = ["建", "开发", "搭建", "搞一个", "弄一个", "整一个",
                "来一个", "做一个", "仿站", "复刻", "1:1",
                "网站", "网页", "页面", "官网", "主页", "首页", "落地页", "门户", "h5", "静态站",
                "html", "前端", "导航栏", "页脚", "响应式", "设计一个", "帮我做", "给我做"]
    # code 关键词(写码/修复/重构)
    code_kw = ["代码", "函数", "脚本", "组件", "模块", "类", "class", "接口", "api", "typescript", "ts",
               "css", "样式", "动画",
               "修复", "bug", "报错", "error", "fix", "调试", "debug", "traceback", "stack", "异常", "崩溃",
               "改一下", "改下", "优化", "重构", "评审", "review", "性能", "慢", "卡"]
    # learn 关键词(讲解/设计/搜索/闲聊)
    learn_kw = ["是什么", "怎么", "如何", "为什么", "原理", "底层", "怎么实现", "如何实现",
                "教程", "例子", "示例", "demo", "案例", "推荐", "哪个好", "选择", "区别", "对比",
                "学习", "概念", "介绍", "讲解",
                "配色", "ui", "ux", "设计稿", "原型", "视觉", "风格", "主题色", "图标",
                "搜索", "查资料", "搜一下", "上网查", "查一下",
                "你好", "嗨", "hello", "hi", "谢谢", "天气", "温度", "聊", "?"]
    # doc 关键词(文档/教程/方案)
    doc_kw = ["readme", "文档", "说明书", "设计文档", "需求文档", "方案文档", "教程文档",
              "tutorial", "写文档", "生成文档"]
    # translate 关键词(翻译/本地化)
    translate_kw = ["翻译", "translate", "译成", "汉化", "本地化", "译文"]

    if any(w in t for w in build_kw):
        keywords = [w for w in build_kw if w in t]
        logger.info("[规则] 命中: build 关键词=%s industry=%s conf=0.7", keywords[:5], industry)
        return RuleResult(keywords=keywords, pattern="build", confidence=0.7, industry=industry)
    if any(w in t for w in code_kw):
        keywords = [w for w in code_kw if w in t]
        logger.info("[规则] 命中: code 关键词=%s industry=%s conf=0.7", keywords[:5], industry)
        return RuleResult(keywords=keywords, pattern="code", confidence=0.7, industry=industry)
    if any(w in t for w in doc_kw):
        keywords = [w for w in doc_kw if w in t]
        logger.info("[规则] 命中: doc 关键词=%s industry=%s conf=0.7", keywords[:5], industry)
        return RuleResult(keywords=keywords, pattern="doc", confidence=0.7, industry=industry)
    if any(w in t for w in translate_kw):
        keywords = [w for w in translate_kw if w in t]
        logger.info("[规则] 命中: translate 关键词=%s industry=%s conf=0.7", keywords[:5], industry)
        return RuleResult(keywords=keywords, pattern="translate", confidence=0.7, industry=industry)
    if any(w in t for w in learn_kw):
        keywords = [w for w in learn_kw if w in t]
        logger.info("[规则] 命中: learn 关键词=%s industry=%s conf=0.7", keywords[:5], industry)
        return RuleResult(keywords=keywords, pattern="learn", confidence=0.7, industry=industry)

    logger.info("[规则] 未命中关键词→默认learn conf=0.5")
    return RuleResult(keywords=[], pattern="learn", confidence=0.5, industry=industry)
