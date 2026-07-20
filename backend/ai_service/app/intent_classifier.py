"""两级意图分类器 + 行业感知。

一次 LLM 调用同时出: {level1, level2, confidence, industry}
level1 6 类, level2 17 类, industry 12 类。
失败退化为关键词兜底(含 level2 细判)。

prompt ~350 token, 回复 ~30 token, 耗时 <1.2s。
"""

from __future__ import annotations

import json
import logging
import re

from .providers import get_chat_model, resolve_fallback_order


# ---- 两级分类 prompt ----
INTENT_SYSTEM = (
    "你是智能建站助手小胡的意图分类器。根据用户输入, 只返回 JSON, 不要额外文字。\n"
    '{"level1": "...", "level2": "...", "confidence": 0.0~1.0, "industry": "..."}\n\n'
    "level1(6选1): learn|code|build|doc|translate|unsupported\n\n"
    "level2(只选对应 level1 下的):\n"
    "learn→explain(解释概念)|debug(排查报错)|compare(技术对比)|casual(闲聊)|design(UI设计配色)|search(搜索查资料)\n"
    "code→snippet(写函数片段)|component(写UI组件)|fix(修Bug)|refactor(重构/评审)\n"
    "build→page(单页)|site(完整站)|modify(修改已有)|game(互动游戏)\n"
    "doc→readme(README)|tutorial(教程)|plan(方案设计)\n"
    "translate→text(翻译)|code_lang(跨语言翻译)\n"
    "unsupported→无子类\n"
    "(注意: 后端开发/数据库/App/游戏引擎/运维部署等非网页前端需求 → unsupported)\n\n"
    "industry(12选1, build/doc 时必填, 其他填 none):\n"
    "restaurant(餐饮)|ecommerce(电商)|gov(政务)|edu(教育)|health(医疗)\n"
    "|finance(金融)|game(游戏)|personal(个人)|corp(企业)|tech(科技)|media(媒体)|other\n\n"
    "checkpoint_relation(5选1, 仅当存在断点时返回, 否则填 none):\n"
    "- resume: 用户要继续上次的工作(如'继续''接着做')\n"
    "- correct: 用户在旧基础上改(如'改导航''换个颜色')\n"
    "- override: 用户要重来(如'重新做一个')\n"
    "- unrelated: 说另一件事,和断点无关\n"
    "- unclear: 无法判断\n"
    "- none: 不存在断点或不需要判断\n\n"
    "用户输入: "
)

logger = logging.getLogger("ai_service.intent")


# 有效的 level1/level2/industry 值
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

# 旧 intent 名 → (level1, level2) 兼容映射(存量统计/前端过渡)
OLD_TO_LEVELS: dict[str, tuple[str, str]] = {
    "chat": ("learn", "explain"),
    "code": ("code", "snippet"),
    "generate": ("build", "site"),
    "modify": ("build", "modify"),
    "game": ("build", "game"),
    "doc": ("doc", "readme"),
    "translate": ("translate", "text"),
}


def detect_context(messages: list[dict], conversation_id: int | None = None,
                   frontend_hint: str = "") -> str:
    """独立上下文检测: 1.前端hint 2.Chroma向量 3.兜底关键词。
    返回上下文描述字符串, 供 classify() 拼入 prompt。
    """
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last:
        return ""

    # 1. 前端 context_hint 优先
    if frontend_hint:
        logger.info("[上下文] 来源=frontend hint=%.60s", frontend_hint)
        return frontend_hint

    # 2. Chroma 向量检索
    if conversation_id:
        try:
            from .rag import find_relevant_messages
            relevant_ids = find_relevant_messages(last, conversation_id)
            if relevant_ids:
                relevant = [m for m in messages if m.get("_msg_id") in relevant_ids]
                ctx_text = " ".join(m.get("content", "")[:200] for m in relevant[-6:])
                hint = _summarize_context(ctx_text)
                if hint:
                    logger.info("[上下文] 来源=chroma hint=%.60s", hint)
                    return hint
        except Exception:
            pass

    # 3. 零依赖兜底
    for m in reversed(messages):
        if m.get("role") == "assistant":
            hint = _summarize_context(m.get("content", ""))
            if hint:
                logger.info("[上下文] 来源=fallback hint=%.60s", hint)
                return hint
            break
    return ""


def classify(messages: list[dict], model_id: str = "deepseek", checkpoint_info: dict | None = None,
             context_hint: str = "") -> dict:
    """纯分类(上下文已在外部检测好)。"""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last.strip():
        return _default()

    # 注入上下文
    if context_hint:
        last = f"用户输入: {last}\n上下文: {context_hint}"

    # 构建 prompt
    sys_prompt = INTENT_SYSTEM
    if checkpoint_info:
        ck = checkpoint_info
        sys_prompt = INTENT_SYSTEM.replace(
            "用户输入: ",
            f"断点: 阶段={ck.get('stage','?')} 进度={ck.get('pct',0)}% 标题=\"{ck.get('title','')}\"\n"
            f"用户输入: ",
        )

    # LLM 分类
    logger.info("意图分类开始 model=%s input=%.200s", model_id, last)
    order = resolve_fallback_order(model_id)
    for mid in order:
        try:
            chat = get_chat_model(mid, streaming=False)
            resp = chat.invoke([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": last[:500]},
            ])
            raw = (resp.content or "").strip()
            logger.info("意图分类 LLM 原始返回(%s): %s", mid, raw[:200])
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            l1 = data.get("level1", data.get("intent", ""))
            l2 = data.get("level2", "")
            confidence = float(data.get("confidence", 0.5))
            industry = data.get("industry", "none") or "none"
            ck_rel = data.get("checkpoint_relation", "none") or "none"

            if industry not in VALID_INDUSTRIES:
                industry = "other"
            if ck_rel not in ("resume", "correct", "override", "unrelated", "unclear", "none"):
                ck_rel = "none"
            if l1 in VALID_LEVEL1 and l2 in VALID_LEVEL2:
                return {"level1": l1, "level2": l2, "confidence": confidence, "industry": industry, "checkpoint_relation": ck_rel}
            old_intent = data.get("intent", "")
            if old_intent in OLD_TO_LEVELS:
                l1, l2 = OLD_TO_LEVELS[old_intent]
                return {"level1": l1, "level2": l2, "confidence": confidence, "industry": industry, "checkpoint_relation": ck_rel}
            break
        except Exception as e:
            logger.warning("意图分类 %s 失败: %s", mid, e)
            continue

    logger.info("意图分类 降级为关键词匹配 input=%.200s", last)
    result = _keyword_fallback(last)
    logger.info("意图分类 关键词结果 -> %s/%s industry=%s", result["level1"], result["level2"], result.get("industry"))
    return result


def _default(l1="learn", l2="casual", conf=1.0, ind="none") -> dict:
    return {"level1": l1, "level2": l2, "confidence": conf, "industry": ind, "checkpoint_relation": "none"}


def _keyword_fallback(text: str) -> dict:
    """关键词兜底, 返回 {level1, level2, confidence, industry, checkpoint_relation}。"""
    t = text.lower()
    dr = lambda l1, l2, i="none": {"level1": l1, "level2": l2, "confidence": 0.7, "industry": i, "checkpoint_relation": "none"}  # noqa: E731

    # 先探测行业
    industry = "other"
    if any(w in t for w in ("餐饮", "餐厅", "饭店", "美食", "外卖", "菜单")):
        industry = "restaurant"
    elif any(w in t for w in ("电商", "商城", "购物", "商品", "店铺", "淘宝")):
        industry = "ecommerce"
    elif any(w in t for w in ("政府", "政务", "机关", "机构", "办事", "大厅", "民政")):
        industry = "gov"
    elif any(w in t for w in ("教育", "学校", "培训", "课程", "学生", "教师", "大学")):
        industry = "edu"
    elif any(w in t for w in ("医疗", "医院", "健康", "诊所", "医生", "预约", "挂号")):
        industry = "health"
    elif any(w in t for w in ("金融", "银行", "保险", "理财", "证券", "基金", "贷款")):
        industry = "finance"
    elif any(w in t for w in ("游戏", "game", "小游戏")):
        industry = "game"
    elif any(w in t for w in ("个人", "博客", "简历", "作品集", "portfolio")):
        industry = "personal"
    elif any(w in t for w in ("企业", "公司", "集团", "品牌", "corp")):
        industry = "corp"
    elif any(w in t for w in ("科技", "SaaS", "软件", "平台", "系统", "App", "IT")):
        industry = "tech"
    elif any(w in t for w in ("媒体", "视频", "抖音", "直播", "娱乐")):
        industry = "media"

    c = 0.7  # noqa: E221

    # level2 细判(按关键词组, 用互斥优先级)
    if any(w in t for w in ("翻译", "translate", "译成")):
        return dr("translate", "text")

    if any(w in t for w in ("贪吃蛇", "打砖块", "坦克大战", "射击", "消消乐", "弹球", "飞机大战", "2048")):
        return dr("build", "game", "game")
    if any(w in t for w in ("游戏", "game", "小游戏")):
        return dr("build", "game", "game")

    if any(w in t for w in ("修改", "改成", "换成", "调整", "modify")):
        return dr("build", "modify", industry)
    if any(w in t for w in ("落地页", "主页", "landing")):
        return dr("build", "page", industry)
    if any(w in t for w in ("网站", "官网", "商城", "后台", "页面", "网页", "site", "web")):
        return dr("build", "site", industry)

    if any(w in t for w in ("readme", "README", "说明")):
        return dr("doc", "readme", industry)
    if any(w in t for w in ("教程", "指南", "tutorial", "步骤")):
        return dr("doc", "tutorial", industry)
    # NEW: 设计/搜索/修复
    if any(w in t for w in ("配色", "色调", "主色", "UI", "样式风格", "设计风格", "布局风格", "排版建议")):
        return dr("learn", "design", industry)
    if any(w in t for w in ("搜索", "查一下", "搜一下", "帮我搜", "search", "找一下")):
        return dr("learn", "search", industry)
    if any(w in t for w in ("修复", "修一下", "帮我修", "debug", "fix", "改bug", "不生效", "修bug")):
        return dr("code", "fix")
    if any(w in t for w in ("评审", "review", "检查代码", "看下代码", "优化建议", "能不能更好")):
        return dr("code", "refactor")

    # 非网页前端 → unsupported
    if any(w in t for w in ("后端", "API", "数据库", "爬虫", "App", "iOS", "安卓", "Unity", "游戏引擎")):
        return dr("unsupported", "", i="other")

    if any(w in t for w in ("方案", "计划", "设计", "架构")):
        return dr("doc", "plan", industry)

    if any(w in t for w in ("组件", "按钮", "表单", "卡片", "component", "modal")):
        return dr("code", "component")
    if any(w in t for w in ("修复", "bug", "改一下", "不对", "有问题", "fix", "error", "报错")):
        return dr("code", "fix")
    if any(w in t for w in ("重构", "优化", "改进", "refactor")):
        return dr("code", "refactor")
    if any(w in t for w in ("代码", "函数", "脚本", "写一个", "snippet", "算法", "编程")):
        return dr("code", "snippet")

    if any(w in t for w in ("报错", "错误", "error", "异常", "exception", "崩溃", "排查")):
        return dr("learn", "debug")
    if any(w in t for w in ("对比", "区别", "比较", "选型", "vs", "哪个好")):
        return dr("learn", "compare")
    if any(w in t for w in ("你好", "谢谢", "再见", "哈哈", "hello", "hi", "我是谁", "你是谁", "我叫", "聊天", "聊聊", "在吗", "怎么样", "可以吗")):
        return dr("learn", "casual")

    return dr("learn", "explain")


def _summarize_context(text: str) -> str:
    """从 assistant 回复中提取简短主题摘要。
    取前 500 字做关键词匹配, 不调 LLM(零延迟)。
    """
    t = text[:500].lower()
    keywords = [
        ("天气", "天气"), ("温度", "天气"), ("下雨", "天气"), ("城市", "天气查询"),
        ("网站", "网站制作"), ("网页", "网页制作"),
        ("编程", "编程学习"), ("代码", "代码"), ("翻译", "翻译"),
        ("教程", "教程"), ("文档", "文档"), ("游戏", "游戏开发"),
        ("商城", "电商"), ("个人站", "个人网站"), ("博客", "博客"),
        ("简历", "简历"), ("模板", "模板"), ("前端", "前端开发"),
        ("颜色", "设计搭配"), ("配色", "设计搭配"), ("字体", "设计"),
        ("布局", "页面布局"), ("部署", "部署上线"),
    ]
    for keyword, label in keywords:
        if keyword in t:
            return label
    return ""
