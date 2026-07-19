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
    "你是小白编码助手的意图分类器。根据用户输入, 只返回 JSON, 不要额外文字。\n"
    '{"level1": "...", "level2": "...", "confidence": 0.0~1.0, "industry": "..."}\n\n'
    "level1(6选1): learn|code|build|doc|translate|unsupported\n\n"
    "level2(只选对应 level1 下的):\n"
    "learn→explain(解释概念)|debug(排查报错,含代码)|compare(技术对比)|casual(闲聊)\n"
    "code→snippet(写函数片段)|component(写UI组件)|fix(修Bug)|refactor(重构)\n"
    "build→page(单页/落地页)|site(完整多页站)|modify(修改已有)|game(互动游戏)\n"
    "doc→readme(README)|tutorial(教程)|plan(方案/设计)\n"
    "translate→text(翻译文本)|code_lang(跨语言代码翻译)\n"
    "unsupported→无子类\n\n"
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


def classify(messages: list[dict], model_id: str = "hy3", checkpoint_info: dict | None = None) -> dict:
    """分类, 返回 {level1, level2, confidence, industry, checkpoint_relation}。"""
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "") or ""
            break
    if not last.strip():
        return _default()

    # 上下文捷径: 已有 HTML → modify
    has_html = any(
        m.get("role") == "assistant" and ("<html" in (m.get("content") or "").lower())
        for m in messages
    )
    if has_html:
        return _default(l1="build", l2="modify", conf=0.99, ind="other")

    # 构建 prompt(含断点上下文)
    sys_prompt = INTENT_SYSTEM
    if checkpoint_info:
        ck = checkpoint_info
        sys_prompt = INTENT_SYSTEM.replace(
            "用户输入: ",
            f"断点: 阶段={ck.get('stage','?')} 进度={ck.get('pct',0)}% 标题=\"{ck.get('title','')}\"\n"
            f"用户输入: ",
        )

    # LLM 分类
    order = resolve_fallback_order(model_id)
    for mid in order:
        try:
            chat = get_chat_model(mid, streaming=False)
            resp = chat.invoke([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": last[:500]},
            ])
            raw = (resp.content or "").strip()
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

    return _keyword_fallback(last)


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
