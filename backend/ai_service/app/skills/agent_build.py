"""Skill: generate_site(核心建站能力 · Plan-and-Execute + Reflexion · §5.3/§5.7)。

M0 实现为显式异步生成器(比 LangGraph astream_events 更易产出结构化事件):
  1. Planner  -> think:planner(结构化规格)
  2. Coder    -> 流式 token(单文件 HTML)
  3. Reviewer -> 静态分析 + LLM 自审(3-C),不通过则 Reflexion 回退 Coder(≤3 轮)
  4. 完成后经 cos_upload 投递预览,emit node(stage=preview, url)

产出的是 SSE 事件字典流(§5.5),由 runner.run_skill 统一包裹 router 级 node + done。
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from collections.abc import AsyncGenerator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
import asyncio
from pathlib import Path
from typing import Dict, Optional

from ..events import ev
from ..providers import (
    ModelUnavailableError,
    astream_with_fallback,
    get_chat_model,
    resolve_fallback_order,
)
from ..intent.common import build_skill_sys
from ..knowledge.chroma import build_rag_context, save_memory
from ..registry import register_skill
from ..scoring import parse_scores, SCORING_DIMENSIONS, needs_review
logger = logging.getLogger("ai_service.skills.agent_build")


GEN_LOG = logging.getLogger("ai_service.generate")


SYS_PLANNER = (
    "你是一名资深产品设计师兼前端架构师,负责把建站需求拆解成可直接指导『高级开发』的结构化规格。"
    "请**只输出一个 JSON 对象**(不要代码块围栏、不要多余解释),字段如下:\n"
    "{\n"
    '  "title": "网站标题(简短,≤12字)",\n'
    '  "goal": "本次生成要达成的核心目标(1句话)",\n'
    '  "steps": ["步骤1", "步骤2", ...],   // 3~6 个有序执行步骤,每步一句话\n'
    '  "design_spec": {\n'
    '     "mood": "整体调性(如 高级/克制/科技/温润)",\n'
    '     "visual_strategy": "差异化视觉策略(1-2句,如 玻璃拟态分层 + 渐变光晕 + 磁吸交互)",\n'
    '     "layout": "布局骨架(如 全屏Hero + 玻璃卡片网格 + 粘性导航)",\n'
    '     "motion": "动效纲领(如 滚动渐显 + 磁吸按钮 + 60fps 缓动)",\n'
    '     "typography": "字号层级策略(如 Display 大标题 + 克制正文)"\n'
    "  },\n"
    '  "reasoning": "拆解思路与关键取舍(2~4 句自由文本)"\n'
    "}\n"
    "要求: 板块划分 / 整体布局 / 视觉风格 / 技术选型 / 动效与交互都体现在 steps、design_spec 与 reasoning 中;"
    "用中文;目标产出『有高级感、不简陋』的成品。"
)
SYS_CODER = (
    "你是一名顶级前端创意开发工程师(Expert Frontend / Creative Developer)。"
    "根据用户需求与上方需求规格(含 design_spec),生成一个【单文件 HTML】,"
    "CSS 与 JS 全部内联在 <style> 和 <script> 中,可直接用 iframe 预览。"
    "只输出完整 HTML 代码,不要解释、不要 markdown 代码块围栏(```)。\n\n"
    "【高级视觉与交互硬标准——必须满足】\n"
    "1. 视觉质感: 使用玻璃拟态(glassmorphism)、柔和分层阴影、渐变光晕/微噪点质感、克制留白;"
    "杜绝大色块平涂与廉价渐变。配色须经设计且符合 WCAG AA 对比度。\n"
    "2. 排版: 建立清晰字号层级(Display/标题/正文/辅助),使用系统字体栈或 Google Fonts,"
    "字距与行高经过调校,呈现『编辑级』排版。\n"
    "3. 微交互: 按钮/卡片 hover 有磁吸或抬升、平滑 cubic-bezier 缓动;"
    "重要元素进入视口时用 IntersectionObserver 渐显/位移。\n"
    "4. 动效性能: 仅 animate transform/opacity,目标 60fps;尊重 prefers-reduced-motion。\n"
    "5. 响应式: 移动端单列、桌面多列,断点合理;触控目标 ≥44px。\n"
    "6. 主题变量: 在 :root 用 CSS 变量暴露主色/背景/圆角/阴影,便于切换;若规格要求则实现浅/暗双主题。\n"
    "7. 结构/可访问性: 语义化标签 + 必要 aria;英雄区(Hero)有强视觉焦点与清晰 CTA。\n"
    "8. 内容: 不输出 lorem 占位或灰底色块;每一屏都要有真实信息与精心排布的内容。"
)

SYS_CODER_GAME = (
    "你是一名游戏开发者。生成一个完整的单文件 HTML 互动小游戏。"
    "必须引入 Three.js CDN: "
    "<script src=\"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js\"></script>。"
    "游戏要素: 3D/2D 场景 + 玩家控制(键盘+触屏) + 碰撞/得分 + 开始/重新开始按钮 + 操作提示。"
    "视觉打磨: 发光粒子 / HUD / 流畅帧率 / 赛博感配色;把 CSS/JS 全部内联,"
    "只输出完整 HTML,不要解释、不要 markdown 代码块围栏(```)。"
    "确保兼容移动端触屏操作和 PC 键盘操作。"
)
SYS_REVIEWER = (
    "你是严格的资深前端评审 + 设计总监。检查给定 HTML 是否:① 以 <html 开头且结构基本完整;"
    "② 标签基本闭合;③ 不含明显会白屏的致命错误(eval / 未定义脚本、外部不可达资源);"
    "④ 视觉与交互是否达到『高级感』: 有层次/留白/微交互/缓动,而非平涂色块或简陋排版;"
    "⑤ 颜色/排版/响应式/可访问性有无问题;⑥ 是否含危险内容/外部不可控脚本(safety)。\\n"
    "输出 JSON(不要代码块围栏):\\n"
    '{"passed": true/false, "comment": "..., 最多60字", '
    '"scores": {"correctness": 1-10, "completeness": 1-10, "readability": 1-10, '
    '"compliance": 1-10, "efficiency": 1-10, "craft": 1-10, "safety": 1-10}, '
    '"issues": ["问题1", "问题2"]}'  # passed=false 时列出具体问题; craft=视觉/交互精致度; safety=安全性
)

# 行业→设计约束(注入 Planner)——升级为高级视觉方向
INDUSTRY_DESIGN: dict[str, str] = {
    "restaurant": "暖色玻璃拟态, 大图Banner带渐变遮罩, 菜单卡片悬浮微交互, 预约/订座磁吸按钮, 电话醒目, 食品高清图",
    "ecommerce": "商品玻璃网格布局, 搜索+筛选栏悬浮, 购物车图标动效, 促销标签微光, 评分星级, 分类导航吸顶",
    "gov": "蓝白/红白庄重, 无障碍 aria 完善, 公告栏玻璃置顶, 政务标识清晰, 留白克制",
    "edu": "清新蓝绿渐变, 课程玻璃卡片列表, 报名表单聚焦态, 师资头像圆角, 学生作品瀑布, 联系方式醒目",
    "health": "柔和蓝白/米色分层, 预约挂号磁吸CTA, 医生卡片悬浮, 卫生标识, 保险提示, 信任感强",
    "finance": "深蓝/金高级感, 数据图表动效, 合规声明小字, 安全标识, 客服入口常驻",
    "game": "暗色/赛博朋克, 发光粒子, 全屏沉浸, 开始游戏大按钮脉冲, 操作提示, Three.js 3D",
    "personal": "简约留白+玻璃卡片, 个人头像圆形光环, 作品集悬浮放大, 社交图标微动, 时间线, 关于我",
    "corp": "品牌色主调, 大图+视频Hero渐变遮罩, 案例/客户Logo墙滚动, 联系CTA磁吸, 关于我们",
    "tech": "深色渐变+网格光晕, 产品截图悬浮, 技术特性图标动效, CTA按钮脉冲, 代码风格等宽字体, 功能介绍",
    "media": "视觉冲击大图, 引导关注动效, 瀑布流玻璃卡片, 视频嵌入, 订阅入口脉冲, 社交分享",
    "other": "现代简约玻璃拟态, 卡片悬浮微交互, 响应式, 清新配色, 克制留白",
}


def _chat(model_id: str, system: str, user_msgs: list) -> str:
    """同步调用模型(Planner/Reviewer)。失败时不自动降级,抛 ModelUnavailableError 让前端选替代。"""
    try:
        chat = get_chat_model(model_id, streaming=False)
        resp = chat.invoke([{"role": "system", "content": system}, *user_msgs])
        return resp.content
    except Exception as e:
        order = resolve_fallback_order(model_id)
        suggested = [m for m in order if m != model_id]
        raise ModelUnavailableError(
            failed=model_id, message=f"模型 {model_id} 不可用: {e}", suggested=suggested
        ) from e


async def _cancelled_now(fn) -> bool:
    """统一支持「同步返回 bool」或「异步协程返回 bool」的取消检测(§1-C/C1)。

    worker_loop 传入的是 async 闭包(需 await);本地测试也可能传同步函数。
    """
    if not fn:
        return False
    res = fn()
    if inspect.isawaitable(res):
        return bool(await res)
    return bool(res)


def _extract_html(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # 去掉 ``` 围栏(可能带 html 语言标注)
        text = text.split("```", 2)[1]
        if text.lstrip().lower().startswith(("html", "HTML")):
            text = text.lstrip()[4:]
    return text.strip()


def _parse_plan(raw: str) -> dict:
    """把 Planner 的 JSON 输出安全解析为计划结构。

    失败兜底:用原文首行当 title、按换行拆 steps,保证前端至少有一个特殊节点可渲染。
    """
    import json
    import re

    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}
    title = str(data.get("title") or "").strip() or (raw.strip().splitlines()[0][:24] if raw.strip() else "建站计划")
    goal = str(data.get("goal") or "").strip()
    reasoning = str(data.get("reasoning") or "").strip()
    steps_raw = data.get("steps") or []
    steps: list[str] = []
    for s in steps_raw:
        if isinstance(s, dict):
            s = s.get("text") or s.get("step") or ""
        s = str(s).strip()
        if s:
            steps.append(s)
    if not steps:  # 兜底:把 reasoning/原文按句拆成步骤
        for line in re.split(r"[\n;；]", reasoning or raw):
            line = line.strip().lstrip("0123456789.、)。) ")
            if len(line) > 2:
                steps.append(line)
        steps = steps[:6]
    design_spec = data.get("design_spec") if isinstance(data.get("design_spec"), dict) else {}
    return {"title": title, "goal": goal, "reasoning": reasoning, "steps": steps, "design_spec": design_spec}


async def _review(model_id: str, html: str) -> Dict:
    """3-C: 静态分析 + LLM 自审(7 维, v1.2.0 统一打分)。

    返回含 needs_review 的评审结果, 供生成内 Reflexion 修复循环与后置 QC 按需触发。
    🔧 C7 修复: LLM 自审异常不再静默放过(passed=True 默认高分), 改为:
      - 调用失败 → passed=False + needs_review=True, 触发修复循环 + QC 升级;
      - 输出无法解析 → passed=True + needs_review=True, 放行但由 QC 二次复核。
    """
    low = html.lower()
    if "<html" not in low or len(html) < 50:
        return {"passed": False, "comment": "缺少 <html 根标签或内容过短",
                "scores": {"correctness": 0, "completeness": 0, "readability": 0,
                           "compliance": 5, "efficiency": 5, "craft": 0, "safety": 8},
                "issues": ["缺少<html根标签"], "needs_review": True}
    if low.count("<script") > low.count("</script") or low.count("<style") > low.count("</style>"):
        return {"passed": False, "comment": "标签未闭合(<script>/</style>)",
                "scores": {"correctness": 2, "completeness": 5, "readability": 5,
                           "compliance": 5, "efficiency": 5, "craft": 3, "safety": 8},
                "issues": ["标签未闭合"], "needs_review": True}
    try:
        out = await asyncio.to_thread(_chat, model_id, SYS_REVIEWER, [{"role": "user", "content": html[:6000]}])
        m = re.search(r"\{.*\}", out, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            scores = parse_scores(data)  # 7 维, 缺失/异常填 0
            return {
                "passed": bool(data.get("passed")),
                "comment": data.get("comment", ""),
                "scores": scores,
                "issues": data.get("issues", []) or [],
                "needs_review": (not bool(data.get("passed"))) or needs_review(scores),
            }
        logger.warning("[gen] Reviewer 输出无解析 JSON, 标记待复核")
        return {"passed": True, "comment": "评审输出无法解析, 已标记待复核",
                "scores": {"correctness": 5, "completeness": 5, "readability": 5,
                           "compliance": 6, "efficiency": 6, "craft": 5, "safety": 6},
                "issues": ["评审输出异常"], "needs_review": True}
    except Exception as e:
        logger.warning("[gen] Reviewer LLM 自审失败, 标记未通过待复核: %s", e)
        return {"passed": False, "comment": "评审模型调用失败, 已标记待复核",
                "scores": {"correctness": 5, "completeness": 5, "readability": 5,
                           "compliance": 6, "efficiency": 6, "craft": 5, "safety": 6},
                "issues": ["评审模型调用失败"], "needs_review": True}


def _deliver(html: str, trace_id: str, user_id: int | None = None,
             project_id: int | None = None, version: int | None = None) -> Optional[str]:
    """落盘本地产物并上传 COS,返回预览直链(失败返回 None,不阻断主流程)。

    COS key 版本化: previews/{user_id}/{project_id}/v{version}/index.html,
    使每次生成/调整在云存储留痕、不被覆盖(旧版仍可按 artifact 行点选)。
    """
    try:
        from ..tools.cos_upload import cos_upload

        art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
        site_dir = art_dir / "anon" / (trace_id or "site")
        site_dir.mkdir(parents=True, exist_ok=True)
        idx = site_dir / "index.html"
        idx.write_text(html, encoding="utf-8")
        # 版本段: 优先用业务下发的语义版本号, 否则降级用 trace_id 保证唯一
        ver_seg = f"v{version}" if version else (trace_id or "site")
        uid = user_id if user_id is not None else "anon"
        pid = project_id if project_id is not None else "anon"
        cos_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/{uid}/{pid}/{ver_seg}/index.html"
        res = cos_upload(str(idx), cos_key)
        if res.get("ok"):
            return res.get("url")
    except Exception:
        pass
    return None


# 评分维度中文标签(用于生成结果汇总文案)
_SCORE_LABELS = {
    "correctness": "正确性", "completeness": "完整性", "readability": "可读性",
    "compliance": "合规", "efficiency": "性能", "craft": "精致度",
}


def _build_generation_summary(plan: dict, review: dict | None, url: str | None,
                               version: int | None, project_id: int | None,
                               intent: str | None) -> str:
    """组装本次生成的文字汇总(Markdown), 以 '文字 + 文件' 形式随 refined 事件返回前端气泡。"""
    plan = plan or {}
    review = review or {}
    title = (plan.get("title") or "网站").strip()
    goal = (plan.get("goal") or "").strip()
    steps = plan.get("steps") or []
    design = plan.get("design_spec") or {}
    ds_mood = (design.get("mood") or "").strip()
    ds_strategy = (design.get("visual_strategy") or "").strip()
    scores = review.get("scores") or {}
    passed = bool(review.get("passed"))
    comment = (review.get("comment") or "").strip()
    kind = "互动小游戏" if intent == "game" else "网站"

    lines: list[str] = []
    lines.append(f"✅ 已为你生成 **{title}**（{kind}）")
    if goal:
        lines.append(f"\n**目标**：{goal}")
    lines.append("\n**本次构建**")
    if steps:
        lines.append(f"- 方案规划：{len(steps)} 个关键步骤（需求拆解 → 编码实现 → 多轮评审打磨）")
    if ds_mood or ds_strategy:
        line = "- 设计方向："
        if ds_mood:
            line += ds_mood
        if ds_strategy:
            line += ("" if not ds_mood else "；") + ds_strategy
        lines.append(line)
    lines.append("- 交付产物：单文件 `index.html`（CSS/JS 全内联，可直接预览与部署）")
    if scores:
        try:
            avg = sum(float(v) for v in scores.values()) / len(scores)
        except Exception:
            avg = 0
        lines.append(f"\n**质量评审**：{'通过 ✅' if passed else '已尽力优化 ⚠️'}（综合 {avg:.1f}/10）")
        parts = [f"{_SCORE_LABELS.get(k, k)} {v}" for k, v in scores.items() if isinstance(v, (int, float))]
        if parts:
            lines.append("维度：" + " · ".join(parts))
    if comment:
        lines.append(f"\n> 评审备注：{comment}")
    lines.append("\n**交付与查看**")
    if url:
        lines.append(f"- 在线预览：{url}")
    lines.append("- 右侧预览面板可查看/下载；下载文件为本次生成版本（历史版本可按 `vN` 切换）")
    lines.append("\n如需调整（配色 / 文案 / 某个模块 / 增删页面等），告诉我具体方向，我马上改。")
    return "\n".join(lines)


# ── 需求来源解析(修复 RC1: 建站时必须读取对话真实需求, 而非首条闲聊消息) ──
_BUILD_KW = ("网站", "官网", "站点", "建站", "生成网站", "做个网站", "首页", "主页",
             "落地页", "页面", "网页", "h5", "H5", "landing", "单页")
_CONTENT_KW = ("天气", "美食", "地图", "定位", "展示", "列表", "预约", "价格", "商品",
               "联系", "关于", "模块", "功能", "板块", "轮播", "表单", "导航", "评论",
               "搜索", "登录", "注册", "新闻", "博客", "案例", "团队", "服务", "产品",
               "介绍", "详情", "订单", "购物车", "支付", "会员", "课程", "视频", "图片",
               "下载", "分享", "日历", "日程", "签到", "排行", "统计", "图表", "特色",
               "活动", "资讯", "动态", "留言", "客服", "品牌", "风格", "配色", "主题")


def _req_doc_to_text(doc: dict) -> str:
    """把结构化需求文档(dict)可读序列化(无 report 字段时的兜底)。"""
    try:
        parts: list[str] = []
        brand = doc.get("brand") or {}
        if isinstance(brand, dict):
            if brand.get("name"):
                parts.append(f"品牌: {brand['name']}")
            if brand.get("intro"):
                parts.append(f"定位: {brand['intro']}")
        goal = doc.get("project_goal") or {}
        if isinstance(goal, dict):
            if goal.get("background"):
                parts.append(f"背景: {goal['background']}")
            if goal.get("objectives"):
                parts.append("目标: " + "；".join(goal["objectives"]))
        pages = doc.get("pages") or []
        if pages:
            plist = []
            for p in pages:
                if isinstance(p, dict):
                    secs = [s.get("name") for s in p.get("sections", []) if isinstance(s, dict)]
                    plist.append(f"{p.get('title', '')}({'/'.join(secs)})")
            parts.append("页面: " + "；".join(plist))
        feats = doc.get("features") or []
        if feats:
            fnames = [f.get("name") if isinstance(f, dict) else str(f) for f in feats]
            parts.append("功能: " + "；".join(fnames))
        design = doc.get("design") or {}
        if isinstance(design, dict):
            if design.get("style"):
                parts.append(f"风格: {design['style']}")
            cs = design.get("color_scheme")
            if isinstance(cs, dict):
                parts.append("配色: " + " ".join(f"{k}={v}" for k, v in cs.items() if k != "meaning"))
        if not parts:
            return json.dumps(doc, ensure_ascii=False, indent=2)
        return "\n".join(parts)
    except Exception:
        return json.dumps(doc, ensure_ascii=False, indent=2)


def _select_requirement(requirement_doc, conversation_summary, messages):
    """从 需求文档 → 对话摘要 → 最近含需求语义的用户消息 挑选最优需求文本。

    返回 (text, source)。修复 RC1: 当用户说"按我刚刚的要求生成网站"时,
    必须取对话里真正描述需求的消息(如"首页天气+附近美食+地图定位"),
    而不是首条闲聊(如"今天天气怎么样")。
    """
    # 1) 结构化需求文档(最权威)
    if isinstance(requirement_doc, dict) and requirement_doc:
        report = requirement_doc.get("report")
        if isinstance(report, str) and report.strip():
            return report.replace("\\n", "\n"), "requirement_doc"
        return _req_doc_to_text(requirement_doc), "requirement_doc"
    # 2) 对话摘要(business 层 get_summary 产出)
    if isinstance(conversation_summary, str) and conversation_summary.strip():
        return conversation_summary.strip(), "conversation_summary"
    # 3) 从消息里找"含建站语义且内容最丰富"的用户消息(排除纯指令句如"帮我做个网站")
    candidates = []
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content") or ""
        if not isinstance(c, str):
            continue
        if any(kw in c for kw in _BUILD_KW):
            score = sum(1 for kw in _CONTENT_KW if kw in c) * 2 + len(c) // 40
            candidates.append((c, score))
    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0], "user_message"
    # 4) 兜底: 最后一条用户消息
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content") or ""
            if isinstance(c, str) and c.strip():
                return c, "user_message"
            break
    return "", "none"


async def generate_stream(
    model_id: str,
    messages: list,
    trace_id: Optional[str] = None,
    is_cancelled=None,
    version: int | None = None,
    user_id: int | None = None,
    project_id: int | None = None,
    intent: Optional[str] = None,
    level2: Optional[str] = None,
    industry: Optional[str] = None,
    checkpoint: Optional[dict] = None,
    resume_mode: str = "resume",
    project_system_prompt: Optional[str] = None,
    requirement_doc: Optional[dict] = None,
    conversation_summary: Optional[str] = None,
    **kwargs,
) -> AsyncGenerator[Dict, None]:
    # 根据意图选 Coder 系统提示(游戏 vs 建站), 并注入项目约束(Tier 1)
    base_coder = SYS_CODER_GAME if intent == "game" else SYS_CODER
    coder_prompt = build_skill_sys(base_coder, project_system_prompt)

    # ① 需求来源(修复 RC1): 优先 结构化需求文档 → 对话摘要 → 最近含需求语义的用户消息,
    #    而非取首条闲聊消息。供下方 RAG / Planner / Coder 统一使用。
    req_text, req_source = _select_requirement(requirement_doc, conversation_summary, messages)
    first_user_msg = req_text or (messages[-1].get("content", "") if messages else "")
    GEN_LOG.info("[gen] 需求来源=%s 长度=%d trace=%s", req_source, len(req_text), trace_id)

    # 断点恢复入口(§7): 跳过已完成阶段
    if checkpoint:
        stage = checkpoint.get("stage", "")
        # await_confirm 续接(来自 paused(await_confirm) 或 requirement_agent→generate_site 转换):
        # 此时 checkpoint 仅含 title/goal/steps(无 stage 键), 需规整为 planner_done 形状再进入 Coder,
        # 否则下方 int(stage[-1]) 会因空 stage 触发 IndexError。
        if not stage or stage == "await_confirm":
            stage = "planner_done"
            if "plan" not in checkpoint:
                checkpoint["plan"] = {
                    "title": checkpoint.get("title", ""),
                    "goal": checkpoint.get("goal", ""),
                    "steps": checkpoint.get("steps", []),
                }
        plan = checkpoint.get("plan", {})
        html = checkpoint.get("html", "")
        attempt = checkpoint.get("attempt", 0)

        if resume_mode == "correct":
            if stage.startswith("reviewer_r"):
                stage = "coder_done"; attempt = 0

        GEN_LOG.info("[gen] 断点恢复 trace=%s stage=%s mode=%s", trace_id, stage, resume_mode)

        # 从断点恢复: 重新执行 Coder(planner_done) 或 跳过 Coder 进 Reviewer(coder_done+)
        if stage == "planner_done":
            yield ev("node", stage="enter_planner_done")
            plan_msgs = [{"role": "user", "content": plan.get("goal", "")}]
            user_msgs = [{"role": "user", "content": f"需求规格:\n{json.dumps(plan, ensure_ascii=False)}"}]
            if req_text:
                user_msgs.append({"role": "user", "content": f"【用户原始需求(来源: {req_source})】\n{req_text}"})
            user_msgs = user_msgs + list(messages)
            # 重新执行 Coder
            html_parts = []
            async for chunk, _ in astream_with_fallback(model_id, user_msgs, system=coder_prompt):
                if await _cancelled_now(is_cancelled):
                    yield ev("aborted"); return
                text = getattr(chunk, "content", chunk)
                if text: html_parts.append(text); yield ev("token", data=text)
            html = _extract_html("".join(html_parts))
            # 进 Reviewer r1
            for attempt in range(3):
                yield ev("node", stage="enter_reviewer", attempt=attempt + 1)
                review = await _review(model_id, html)
                GEN_LOG.info("[gen] Reviewer 第%s轮(恢复) trace=%s passed=%s", attempt + 1, trace_id, review["passed"])
                yield ev("think", stage="reviewer", passed=review["passed"], comment=review["comment"])
                if review["passed"]: break
                yield ev("node", stage="enter_coder", retry=True)
                fix_msgs = [{"role": "user", "content": f"上一版未通过:{review['comment']}\n修正 HTML:\n{html[:8000]}"}]
                hp = []
                async for chunk, _ in astream_with_fallback(model_id, fix_msgs, system=coder_prompt):
                    if await _cancelled_now(is_cancelled):
                        yield ev("aborted"); return
                    text = getattr(chunk, "content", chunk)
                    if text: hp.append(text); yield ev("token", data=text)
                html = _extract_html("".join(hp))
        else:
            # coder_done / reviewer_rN: 直接从 Reviewer 恢复
            yield ev("node", stage=f"resume_{stage}")
            if stage == "coder_done":
                attempt = 0
            else:
                attempt = int(stage[-1])
            for a in range(attempt, 3):
                yield ev("node", stage="enter_reviewer", attempt=a + 1)
                review = await _review(model_id, html)
                if review["passed"] or a >= 2:
                    yield ev("think", stage="reviewer", passed=review["passed"], comment=review.get("comment", ""))
                    break
                yield ev("think", stage="reviewer", passed=False, comment=review["comment"])
                yield ev("node", stage="enter_coder", retry=True)
                fix_msgs = [{"role": "user", "content": f"修正:{review['comment']}\nHTML:\n{html[:8000]}"}]
                hp = []
                async for chunk, _ in astream_with_fallback(model_id, fix_msgs, system=coder_prompt):
                    if await _cancelled_now(is_cancelled):
                        yield ev("aborted"); return
                    text = getattr(chunk, "content", chunk)
                    if text: hp.append(text); yield ev("token", data=text)
                html = _extract_html("".join(hp))

        # 收尾
        yield ev("review", data=review)   # 评审结果(7维+needs_review), 供后置 QC 按需复核
        yield ev("node", stage="previewing")
        url = _deliver(html, trace_id, user_id, project_id, version)
        yield ev("preview", url=url, fallback="srcdoc" if not url else None)
        with suppress(Exception):
            await asyncio.to_thread(save_memory, trace_id or "site", plan.get("title", "建站"), html[:1500], plan.get("steps", []))
        # 文字汇总: 让后端在返回文件的同时, 以文字形式给出本次生成结果说明(前端气泡展示)
        yield ev("refined", data=_build_generation_summary(plan, review, url, version, project_id, intent))
        yield ev("node", stage="done")
        return

    # ---------- 正常流程 ----------

    # ②-a RAG 增强: 需求文本已在上文 first_user_msg 准备好(含对话真实需求),
    #    带超时保护, Chroma 不可达时 5s 后跳过, 不阻塞生成
    rag_ctx = ""
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future: Future[str] = pool.submit(build_rag_context, first_user_msg)
            rag_ctx = future.result(timeout=5.0)
    except FutureTimeout:
        GEN_LOG.warning("[gen] RAG 检索超时(>5s), 跳过增强 trace=%s", trace_id)
    except Exception as e:
        GEN_LOG.warning("[gen] RAG 检索失败, 跳过增强 trace=%s: %s", trace_id, e)

    try:
        # 1) Planner
        yield ev("node", stage="enter_planner")
        GEN_LOG.info("[gen] Planner 开始 trace=%s model=%s rag=%schars", trace_id, model_id, len(rag_ctx))
        planner_msgs = [{"role": "user", "content": first_user_msg or (messages[-1].get("content", "") if messages else "")}]
        if rag_ctx:
            planner_msgs.append(
                {"role": "user", "content": f"【参考上下文(组件库 / 历史记忆)】\n{rag_ctx}"}
            )
        # 注入行业设计约束
        if industry and industry != "none":
            design_hint = INDUSTRY_DESIGN.get(industry, INDUSTRY_DESIGN["other"])
            planner_msgs.append(
                {"role": "user", "content": f"【行业设计约束: {industry}】\n{design_hint}"}
            )
        spec = await asyncio.to_thread(_chat, model_id, build_skill_sys(SYS_PLANNER, project_system_prompt), planner_msgs)
        plan = _parse_plan(spec)
        GEN_LOG.info(
            "[gen] Planner 完成 trace=%s title=%s steps=%s",
            trace_id, plan.get("title", "-"), len(plan.get("steps", [])),
        )
        # 思考流:Planner 的拆解思路(分步思考的一部分)
        if plan.get("reasoning"):
            yield ev("think", stage="planner", content=plan["reasoning"])
        yield ev(
            "plan",
            title=plan.get("title", ""),
            goal=plan.get("goal", ""),
            steps=plan.get("steps", []),
        )
        # 方案确认: 暂停等待用户确认后才开始生成代码
        yield ev(
            "paused",
            stage="await_confirm",
            plan_title=plan.get("title", ""),
            plan_goal=plan.get("goal", ""),
            plan_steps=plan.get("steps", []),
            content="已根据您的需求生成建站方案，确认后开始设计与开发。点击「确认并生成」，或直接回复「开始生成 / 帮我做网站」即可。",
            cta_label="确认并生成",
        )
        return  # 暂停, 等待前端发起 resume/confirm 续接
        # 检查取消(断点保存点 1: planner_done)
        if await _cancelled_now(is_cancelled):
            yield ev("checkpoint", stage="planner_done", data={
                "plan": plan, "rag_ctx": rag_ctx,
                "messages": messages[:10],  # 只保留最近 10 条
            })
            yield ev("paused", stage="planner_done", progress=25)
            yield ev("done")
            return

        # 2) Coder(流式,模型不可用时不自动降级,由前端确认后重发)
        yield ev("node", stage="enter_coder")
        GEN_LOG.info("[gen] Coder 开始 trace=%s model=%s", trace_id, model_id)
        user_msgs = [{"role": "user", "content": f"需求规格:\n{spec}"}]
        if req_text:
            user_msgs.append({"role": "user", "content": f"【用户原始需求(来源: {req_source})】\n{req_text}"})
        user_msgs = user_msgs + list(messages)
        html_parts: list = []
        token_count = 0
        async for chunk, _ in astream_with_fallback(model_id, user_msgs, system=coder_prompt):
            if await _cancelled_now(is_cancelled):
                yield ev("aborted")
                return
            text = getattr(chunk, "content", chunk)
            if text:
                html_parts.append(text)
                token_count += 1
                yield ev("token", data=text)
        yield ev("degraded", model=model_id, requested=model_id)
        html = _extract_html("".join(html_parts))
        GEN_LOG.info(
            "[gen] Coder 完成 trace=%s chars=%s chunks=%s model=%s",
            trace_id, len(html), token_count, model_id,
        )
        # 检查取消(断点保存点 2: coder_done)
        if await _cancelled_now(is_cancelled):
            yield ev("checkpoint", stage="coder_done", data={
                "plan": plan, "html": html, "rag_ctx": rag_ctx,
                "messages": messages[:10],
            })
            yield ev("paused", stage="coder_done", progress=65)
            yield ev("done")
            return

        # 3) Reviewer + Reflexion(≤3 轮)
        for attempt in range(3):
            yield ev("node", stage="enter_reviewer", attempt=attempt + 1)
            review = await _review(model_id, html)
            GEN_LOG.info(
                "[gen] Reviewer 第%s轮 trace=%s passed=%s",
                attempt + 1, trace_id, review["passed"],
            )
            yield ev("think", stage="reviewer", passed=review["passed"], comment=review["comment"])
            if review["passed"]:
                break
            # 检查取消(断点保存点 3: reviewer_rN)
            if await _cancelled_now(is_cancelled):
                yield ev("checkpoint", stage=f"reviewer_r{attempt}", data={
                    "plan": plan, "html": html, "attempt": attempt,
                })
                yield ev("paused", stage=f"reviewer_r{attempt}", progress=75 + attempt * 10)
                yield ev("done")
                return
            # Reflexion: 让 Coder 基于评审建议修正
            yield ev("node", stage="enter_coder", retry=True)
            fix_msgs = [
                {
                    "role": "user",
                    "content": f"上一版未通过评审:{review['comment']}\n请修正以下 HTML:\n{html[:8000]}",
                }
            ]
            html_parts = []
            async for chunk, _ in astream_with_fallback(model_id, fix_msgs, system=coder_prompt):
                if await _cancelled_now(is_cancelled):
                    yield ev("aborted")
                    return
                text = getattr(chunk, "content", chunk)
                if text:
                    html_parts.append(text)
                    yield ev("token", data=text)
            yield ev("degraded", model=model_id, requested=model_id)
            html = _extract_html("".join(html_parts))

        # 4) 预览投递(COS 直链,§10)
        yield ev("review", data=review)   # 评审结果(7维+needs_review), 供后置 QC 按需复核
        yield ev("node", stage="previewing")
        url = _deliver(html, trace_id, user_id, project_id, version)
        GEN_LOG.info("[gen] 预览投递 trace=%s url=%s", trace_id, url or "无(srcdoc 兜底)")
        yield ev("preview", url=url, fallback="srcdoc" if not url else None)

        # ②-a 记忆闭环:生成成功后回写 memory 集合(供未来检索增强)
        with suppress(Exception):
            await asyncio.to_thread(
                save_memory,
                trace_id or "site",
                plan.get("title", "建站"),
                html[:1500],
                plan.get("steps", []),
            )

        # 文字汇总: 让后端在返回文件的同时, 以文字形式给出本次生成结果说明(前端气泡展示)
        yield ev("refined", data=_build_generation_summary(plan, review, url, version, project_id, intent))

        yield ev("node", stage="done")
        GEN_LOG.info("[gen] 完成 trace=%s html=%schars", trace_id, len(html))

    except ModelUnavailableError as e:
        GEN_LOG.warning(
            "[gen] 模型不可用 trace=%s failed=%s suggested=%s", trace_id, e.failed, e.suggested
        )
        yield ev(
            "retry",
            failed=e.failed,
            suggested=e.suggested,
            message=str(e),
        )
        yield ev("aborted")


# 注册进 SkillRegistry(§5.8)
register_skill(
    name="agent_build",
    intent_tags=[
        "site",
        "网页",
        "页面",
        "网站",
        "建站",
        "落地页",
        "官网",
        "landing",
        "主页",
        "博客",
        "个人站",
    ],
    handler=generate_stream,
    is_graph=True,
    display_name="网站迭代",
    avatar="🔧",
    role="建站迭代",
    description="修改/迭代已有网站(Planner→Coder→Reviewer 多 agent)",
)
