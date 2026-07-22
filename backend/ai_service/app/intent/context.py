"""上下文模块: 历史关联检测 + 意图修正。

三层兜底: 1.WebLLM前端hint 2.Chroma向量检索 3.零依赖关键词
输出 ContextResult {has_context, hint, correction, source}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("ai_service.intent.context")


@dataclass
class ContextResult:
    has_context: bool = False
    hint: str = ""
    correction: dict | None = None  # {level1, level2, reason} 可选的意图修正
    source: str = "none"            # "webllm"|"chroma"|"fallback"|"none"
    chroma_context: str = ""        # v0.9.0: 项目记忆 + 用户偏好(额外上下文)


def run_context(messages: list[dict], conversation_id: int | None = None,
                frontend_hint: str = "",
                user_id: int | None = None,
                project_id: int | None = None) -> ContextResult:
    """上下文检测入口。v0.9.0: 新增 Chroma 项目记忆/用户偏好来源。"""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last:
        logger.info("[上下文] 输入为空,跳过")
        return ContextResult()

    # --- Chroma 项目/用户偏好(v0.9.0, 最先跑, 非阻塞) ---
    chroma_ctx = ""
    if user_id is not None or project_id is not None:
        try:
            from ..knowledge.chroma import retrieve_user_preferences, retrieve_project_memory
            parts = []
            if user_id is not None:
                prefs = retrieve_user_preferences(user_id, last)
                if prefs:
                    parts.append("用户偏好: " + "; ".join(p["content"][:100] for p in prefs[:3]))
            if project_id is not None:
                mems = retrieve_project_memory(project_id, last)
                if mems:
                    parts.append("项目记忆: " + "; ".join(m["content"][:100] for m in mems[:3]))
            if parts:
                chroma_ctx = " | ".join(parts)
                logger.info("[上下文] 来源=Chroma项目/用户 | %s", chroma_ctx[:150])
        except Exception as e:
            logger.debug("[上下文] Chroma项目/用户检索异常: %s", e)

    # 1. 前端 WebLLM hint
    if frontend_hint:
        logger.info("[上下文] 来源=前端WebLLM | 内容=%.60s", frontend_hint)
        return ContextResult(has_context=True, hint=frontend_hint, source="webllm",
                           chroma_context=chroma_ctx)

    # 2. Chroma 向量检索
    if conversation_id:
        try:
            from ..knowledge.chroma import find_relevant_messages
            logger.info("[上下文] 尝试Chroma向量检索 conv=%s", conversation_id)
            relevant_ids = find_relevant_messages(last, conversation_id)
            if relevant_ids:
                relevant = [m for m in messages if m.get("_msg_id") in relevant_ids]
                ctx_text = " ".join(m.get("content", "")[:200] for m in relevant[-6:])
                hint = _summarize_context(ctx_text)
                if hint:
                    logger.info("[上下文] 来源=Chroma向量 | 相关消息=%d | 摘要=%.60s",
                               len(relevant_ids), hint)
                    correction = _infer_correction(hint)
                    return ContextResult(has_context=True, hint=hint,
                                       correction=correction, source="chroma",
                                       chroma_context=chroma_ctx)
            else:
                logger.info("[上下文] Chroma未找到相关消息 conv=%s", conversation_id)
        except Exception as e:
            logger.debug("[上下文] Chroma检索异常: %s", e)

    # 3. 零依赖兜底
    logger.info("[上下文] 使用零依赖兜底(关键词匹配)")
    for m in reversed(messages):
        if m.get("role") == "assistant":
            hint = _summarize_context(m.get("content", ""))
            if hint:
                logger.info("[上下文] 来源=关键词兜底 | 摘要=%.60s", hint)
                correction = _infer_correction(hint)
                return ContextResult(has_context=True, hint=hint,
                                   correction=correction, source="fallback",
                                   chroma_context=chroma_ctx)
            break
    logger.info("[上下文] 所有来源均未检测到上下文")
    return ContextResult(chroma_context=chroma_ctx)


# 话题 → 关键词(打分制聚合, 零 LLM)。覆盖全部行业 + 主要意图。
_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "网站制作": ["网站", "官网", "建站", "主页", "首页", "落地页", "门户", "门户站", "作品集", "portfolio"],
    "页面制作": ["页面", "单页", "网页", "html", "着陆页", "静态页", "h5"],
    "前端开发": ["前端", "组件", "css", "样式", "布局", "导航", "页脚", "响应式", "vue", "react", "javascript"],
    "游戏开发": ["游戏", "小游戏", "互动游戏", "canvas", "贪吃蛇", "俄罗斯方块"],
    "电商": ["电商", "商城", "购物", "商品", "订单", "支付", "店铺", "购物车"],
    "餐饮建站": ["餐饮", "餐厅", "饭店", "美食", "外卖", "菜单", "菜品"],
    "教育建站": ["教育", "课程", "培训", "学校", "学生", "课件"],
    "医疗建站": ["医疗", "医院", "诊所", "医生", "挂号", "预约", "健康"],
    "金融建站": ["金融", "银行", "保险", "理财", "证券", "基金"],
    "政务建站": ["政务", "政府", "公安", "社保", "税务", "审批"],
    "旅游建站": ["旅游", "酒店", "景点", "攻略", "机票", "民宿"],
    "科技建站": ["科技", "saas", "ai", "人工智能", "芯片", "物联网"],
    "媒体建站": ["媒体", "视频", "直播", "新闻", "博客", "公众号"],
    "个人建站": ["个人", "简历", "博客", "作品集", "名片"],
    "企业建站": ["企业", "公司", "官网", "品牌", "集团"],
    "需求分析": ["需求", "方案", "规划", "功能清单", "用户画像", "竞品"],
    "代码生成": ["代码", "函数", "脚本", "接口", "api", "类", "模块"],
    "代码修复": ["修复", "报错", "bug", "error", "debug", "traceback", "异常", "崩溃"],
    "代码重构": ["重构", "优化", "评审", "review", "性能", "慢", "卡"],
    "文档": ["文档", "readme", "教程", "tutorial", "说明书", "设计文档"],
    "翻译": ["翻译", "translate", "汉化", "本地化", "译文"],
    "设计": ["配色", "ui", "ux", "设计稿", "原型", "视觉", "风格", "主题色", "图标"],
    "搜索": ["搜索", "查资料", "搜一下", "上网查", "查一下"],
    "教程讲解": ["教程", "概念", "原理", "区别", "对比", "为什么", "怎么", "如何"],
    "天气": ["天气", "温度", "下雨", "湿度", "气温"],
}


def _summarize_context(text: str) -> str:
    """从上下文文本提取话题摘要(关键词打分聚合, 不调LLM)。

    改进点(相对旧版):
    - 打分制: 统计每个话题命中关键词数, 取最高, 不再受列表顺序影响(去首匹配偏差);
    - 多话题合并: 当第二高话题与最高接近(差值≤1)时, 合并为 "A / B", 给分类器更丰富上下文;
    - 窗口放大到 1200 字(Chroma 路径已拼接多消息, 旧版 500 会截断早期上下文);
    - 词表覆盖全部 13 行业 + 主要意图(旧版大量常见意图无 topic)。
    """
    t = text[:1200].lower()
    if not t:
        return ""
    scores: dict[str, int] = {}
    for topic, kws in _TOPIC_KEYWORDS.items():
        n = sum(1 for kw in kws if kw in t)
        if n:
            scores[topic] = n
    if not scores:
        return ""
    ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    top_topic, top_n = ranked[0]
    if len(ranked) > 1 and ranked[1][1] >= top_n - 1:
        return f"{top_topic} / {ranked[1][0]}"
    return top_topic


# 话题 → 意图修正(供 _infer_correction)。覆盖 _TOPIC_KEYWORDS 主要话题。
_CORRECTION_MAP: list[tuple[str, dict]] = [
    ("网站制作", {"level1": "build", "level2": "site", "reason": "上条在讨论建站"}),
    ("页面制作", {"level1": "build", "level2": "page", "reason": "上条在讨论网页"}),
    ("前端开发", {"level1": "build", "level2": "site", "reason": "上条在讨论前端"}),
    ("游戏开发", {"level1": "build", "level2": "game", "reason": "上条在讨论游戏"}),
    ("电商", {"level1": "build", "level2": "site", "reason": "上条在讨论电商"}),
    ("餐饮建站", {"level1": "build", "level2": "site", "reason": "上条在讨论餐饮"}),
    ("教育建站", {"level1": "build", "level2": "site", "reason": "上条在讨论教育"}),
    ("医疗建站", {"level1": "build", "level2": "site", "reason": "上条在讨论医疗"}),
    ("金融建站", {"level1": "build", "level2": "site", "reason": "上条在讨论金融"}),
    ("政务建站", {"level1": "build", "level2": "site", "reason": "上条在讨论政务"}),
    ("旅游建站", {"level1": "build", "level2": "site", "reason": "上条在讨论旅游"}),
    ("科技建站", {"level1": "build", "level2": "site", "reason": "上条在讨论科技"}),
    ("媒体建站", {"level1": "build", "level2": "site", "reason": "上条在讨论媒体"}),
    ("个人建站", {"level1": "build", "level2": "site", "reason": "上条在讨论个人站"}),
    ("企业建站", {"level1": "build", "level2": "site", "reason": "上条在讨论企业站"}),
    ("需求分析", {"level1": "build", "level2": "requirement", "reason": "上条在讨论需求"}),
    ("代码生成", {"level1": "code", "level2": "snippet", "reason": "上条在讨论写代码"}),
    ("代码修复", {"level1": "code", "level2": "fix", "reason": "上条在讨论修复"}),
    ("代码重构", {"level1": "code", "level2": "refactor", "reason": "上条在讨论重构"}),
    ("文档", {"level1": "doc", "level2": "readme", "reason": "上条在讨论文档"}),
    ("翻译", {"level1": "translate", "level2": "text", "reason": "上条在讨论翻译"}),
    ("设计", {"level1": "learn", "level2": "design", "reason": "上条在讨论设计"}),
    ("搜索", {"level1": "learn", "level2": "search", "reason": "上条在讨论搜索查资料"}),
    ("教程讲解", {"level1": "learn", "level2": "explain", "reason": "上条在讨论讲解"}),
]


def _infer_correction(hint: str) -> dict | None:
    """从上下文摘要推测意图修正(支持多话题, 取最具体的匹配)。"""
    if not hint:
        return None
    for topic, correction in _CORRECTION_MAP:
        if topic in hint:
            return correction
    return None
