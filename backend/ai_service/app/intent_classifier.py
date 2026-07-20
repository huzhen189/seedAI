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
        logger.info("[上下文检测] 输入为空, 跳过")
        return ""

    # 1. 前端 context_hint 优先
    if frontend_hint:
        logger.info("[上下文检测] 来源=前端WebLLM | 内容=%.60s", frontend_hint)
        return frontend_hint

    # 2. Chroma 向量检索
    if conversation_id:
        try:
            from .rag import find_relevant_messages
            logger.info("[上下文检测] 尝试Chroma向量检索 conv=%s", conversation_id)
            relevant_ids = find_relevant_messages(last, conversation_id)
            if relevant_ids:
                relevant = [m for m in messages if m.get("_msg_id") in relevant_ids]
                ctx_text = " ".join(m.get("content", "")[:200] for m in relevant[-6:])
                hint = _summarize_context(ctx_text)
                if hint:
                    logger.info("[上下文检测] 来源=Chroma向量 | 相关消息=%d条 | 摘要=%.60s",
                               len(relevant_ids), hint)
                    return hint
            else:
                logger.info("[上下文检测] Chroma未找到相关消息 conv=%s", conversation_id)
        except Exception as e:
            logger.debug("[上下文检测] Chroma检索异常: %s", e)
            pass

    # 3. 零依赖兜底
    logger.info("[上下文检测] 使用零依赖兜底(关键词匹配)")
    for m in reversed(messages):
        if m.get("role") == "assistant":
            hint = _summarize_context(m.get("content", ""))
            if hint:
                logger.info("[上下文检测] 来源=关键词兜底 | 摘要=%.60s", hint)
                return hint
            break
    logger.info("[上下文检测] 所有来源均未检测到上下文")


def classify(messages: list[dict], model_id: str = "deepseek", checkpoint_info: dict | None = None,
             context_hint: str = "") -> dict:
    """纯分类(上下文已在外部检测好)。返回 {level1, level2, confidence, industry, checkpoint_relation}。"""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last.strip():
        logger.info("[分类] [1/3] 无有效用户输入 → 返回默认值")
        return _default()

    # 注入上下文
    if context_hint:
        logger.info("[分类] [1/3] 注入上下文 hint=%.60s", context_hint)
        last = f"用户输入: {last}\n上下文: {context_hint}"
    else:
        logger.info("[分类] [1/3] 无上下文,直接分类 input=%.80s", last)

    # 构建 prompt
    sys_prompt = INTENT_SYSTEM
    if checkpoint_info:
        ck = checkpoint_info
        sys_prompt = INTENT_SYSTEM.replace(
            "用户输入: ",
            f"断点: 阶段={ck.get('stage','?')} 进度={ck.get('pct',0)}% 标题=\"{ck.get('title','')}\"\n"
            f"用户输入: ",
        )
        logger.info("[分类] [1/3] 检测到断点 stage=%s pct=%s%%", ck.get('stage','?'), ck.get('pct',0))

    # LLM 分类
    logger.info("[分类] [2/3] 调用LLM分类 model=%s len=%d", model_id, len(last))
    t0 = time.time()
    order = resolve_fallback_order(model_id)
    for mid in order:
        try:
            chat = get_chat_model(mid, streaming=False)
            resp = chat.invoke([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": last[:500]},
            ])
            raw = (resp.content or "").strip()
            elapsed = (time.time() - t0) * 1000
            logger.info("[分类] [2/3] LLM返回 model=%s 耗时=%.0fms raw=%.200s", mid, elapsed, raw)
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
                res = {"level1": l1, "level2": l2, "confidence": confidence, "industry": industry, "checkpoint_relation": ck_rel}
                logger.info("[分类] [3/3] LLM分类成功 一级=%s 二级=%s 置信度=%.0f%% 行业=%s",
                           l1, l2, confidence * 100, industry)
                return res
            old_intent = data.get("intent", "")
            if old_intent in OLD_TO_LEVELS:
                l1, l2 = OLD_TO_LEVELS[old_intent]
                res = {"level1": l1, "level2": l2, "confidence": confidence, "industry": industry, "checkpoint_relation": ck_rel}
                logger.info("[分类] [3/3] 旧格式转换 %s→%s/%s 置信度=%.0f%%", old_intent, l1, l2, confidence * 100)
                return res
            break
        except Exception as e:
            logger.warning("[分类] [2/3] 模型%s调用失败: %s", mid, e)
            continue

    logger.info("[分类] [3/3] LLM失败,降级关键词匹配 input=%.120s", last)
    result = _keyword_fallback(last)
    logger.info("[分类] [3/3] 关键词结果 一级:%s 二级:%s 行业:%s 置信度:70%%",
               result["level1"], result["level2"], result.get("industry"))
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
    取前 600 字做关键词匹配, 不调 LLM(零延迟)。
    """
    t = text[:600].lower()
    keywords = [
        # 天气 & 地理
        ("天气", "天气查询"), ("温度", "天气查询"), ("下雨", "天气查询"),
        ("湿度", "天气查询"), ("风速", "天气查询"), ("紫外线", "天气查询"),
        ("城市", "城市信息"), ("省份", "城市信息"), ("地图", "地图查询"),

        # 建站 & 开发
        ("网站", "网站制作"), ("网页", "网页制作"), ("建站", "网站制作"),
        ("模板", "模板选择"), ("设计风格", "网站设计"), ("html", "网页开发"),
        ("css", "网页开发"), ("javascript", "网页开发"), ("单页", "网页开发"),
        ("响应式", "网页开发"), ("导航栏", "页面布局"), ("页脚", "页面布局"),

        # 前端技术
        ("前端", "前端开发"), ("布局", "页面布局"), ("组件", "组件开发"),
        ("代码", "代码编写"), ("编程", "编程学习"), ("调试", "错误排查"),

        # 设计
        ("颜色", "配色方案"), ("配色", "配色方案"), ("色调", "配色方案"),
        ("字体", "字体选择"), ("排版", "排版设计"), ("动效", "动效设计"),
        ("动画", "动效设计"), ("图标", "图标资源"), ("logo", "品牌设计"),

        # 电商
        ("商城", "电商网站"), ("电商", "电商网站"), ("购物", "电商网站"),
        ("商品", "电商网站"), ("订单", "电商网站"), ("支付", "电商网站"),
        ("促销", "电商网站"), ("店铺", "电商网站"),

        # 餐饮
        ("餐厅", "餐饮网站"), ("饭店", "餐饮网站"), ("美食", "餐饮网站"),
        ("外卖", "餐饮网站"), ("菜单", "餐饮网站"), ("饮品", "餐饮网站"),

        # 旅游
        ("旅游", "旅游网站"), ("酒店", "旅游网站"), ("景点", "旅游网站"),
        ("攻略", "旅游网站"), ("出行", "旅游网站"), ("机票", "旅游网站"),

        # 教育
        ("教育", "教育网站"), ("学校", "教育网站"), ("培训", "教育网站"),
        ("课程", "教育网站"), ("学生", "教育网站"), ("在线学习", "教育网站"),

        # 医疗健康
        ("医疗", "医疗网站"), ("医院", "医疗网站"), ("健康", "医疗网站"),
        ("诊所", "医疗网站"), ("预约", "预约系统"), ("挂号", "预约系统"),

        # 个人
        ("个人", "个人网站"), ("博客", "个人博客"), ("简历", "个人简历"),
        ("作品集", "作品集展示"), ("portfolio", "作品集展示"),

        # 企业
        ("企业", "企业官网"), ("公司", "企业官网"), ("品牌", "品牌网站"),
        ("团队", "团队介绍"), ("服务", "服务介绍"), ("产品", "产品展示"),

        # 内容
        ("文档", "文档编写"), ("教程", "教程指南"), ("说明", "文档编写"),
        ("readme", "文档编写"), ("指南", "教程指南"), ("翻译", "文本翻译"),

        # 游戏 & 娱乐
        ("游戏", "游戏开发"), ("小游戏", "游戏开发"), ("娱乐", "娱乐网站"),
        ("视频", "视频网站"), ("音乐", "音乐网站"), ("直播", "直播网站"),

        # 部署
        ("部署", "部署上线"), ("发布", "部署上线"), ("上线", "部署上线"),
        ("预览", "预览链接"), ("分享", "分享链接"),

        # 通用
        ("修改", "内容修改"), ("改成", "内容修改"), ("换成", "内容修改"),
        ("调整", "内容修改"), ("优化", "性能优化"), ("修复", "错误修复"),
        ("报错", "错误修复"), ("bug", "错误修复"), ("搜索", "信息搜索"),
    ]
    for keyword, label in keywords:
        if keyword in t:
            return label
    return ""
