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
import time
import re
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
from ..analytics import record_reviewer, record_llm_call
from shared.vendor import VENDOR_REFERENCE, LIBS_REFERENCE
logger = logging.getLogger("ai_service.skills.agent_generate_site")

# 本技能注册名(供统计维度 per-skill 区分; 与 @register_skill 名一致)
SKILL_NAME = "agent_generate_site"


GEN_LOG = logging.getLogger("ai_service.generate")


def _review_reason(review: dict) -> str:
    """把 reviewer 结果归类为一个失败原因标签, 供统计 reason_dist 分布。

    取值: static_html / static_close_tag / parse_fail / llm_fail / llm_unpassed / ok
    """
    blob = " ".join(review.get("issues") or []) + " " + (review.get("comment", "") or "")
    if "评审模型调用失败" in blob:
        return "llm_fail"
    if "评审输出异常" in blob or "无法解析" in blob:
        return "parse_fail"
    if "html" in blob.lower() and ("根标签" in blob or "缺少" in blob):
        return "static_html"
    if "标签未闭合" in blob:
        return "static_close_tag"
    if not review.get("passed"):
        return "llm_unpassed"
    return "ok"


SYS_PLANNER = (
    "你是一名资深产品设计师兼前端架构师,负责把建站需求拆解成可直接指导前端开发的结构化规格。\n\n"
    "⚠️ 重要说明：本平台仅支持纯静态前端页面（HTML + CSS + JavaScript），"
    "无法接入后端服务、数据库、用户系统或服务端 API。"
    "请在设计时确保所有功能均可通过前端技术实现，涉及数据存储/登录/支付等需求应在 reasoning 中友好提示用户。\n\n"
    "请**只输出一个 JSON 对象**(不要代码块围栏、不要多余解释),字段如下:\n"
    "{\n"
    '  "title": "网站标题(简短,≤12字)",\n'
    '  "goal": "本次生成要达成的核心目标(1句话)",\n'
    '  "steps": ["步骤1", "步骤2", ...],   // 3~6 个有序执行步骤,每步一句话\n'
    '  "files": ["index.html"],   // 需生成的文件列表;多页面站点列出全部(首页必须 index.html),如 ["index.html","products.html","about.html"]\n'
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
    "根据用户需求与上方需求规格(含 design_spec),生成一个完整的静态网站。\n\n"
    "⚠️ 平台限制（请务必遵守）：\n"
    "• 仅支持纯前端技术：HTML、CSS、JavaScript（含内联或独立 .css/.js 文件）\n"
    "• 不支持：后端服务、数据库、Node.js 服务端、PHP、Python 后端、第三方 API 代理、用户登录/注册、文件上传、支付\n"
    "• 如需「联系我们」表单，可使用 mailto: 链接或静态表单 + 提示文字「本功能需后端支持，此处为演示界面」\n"
    "• 如需求中包含后端功能，用友好文案告知用户：「这个功能需要后端服务才能完整实现，目前为您展示了静态前端的模拟效果」\n\n"
    "【输出格式——多文件规范】\n"
    "每个文件用 `<!-- FILE: 文件名 -->` 标记开头。示例:\n"
    "<!-- FILE: index.html -->\n<html>...</html>\n"
    "<!-- FILE: style.css -->\n/* CSS 内容 */\n"
    "<!-- FILE: script.js -->\n// JS 内容\n"
    "主入口 HTML 必须命名为 index.html; CSS/JS 文件按实际用途命名(如 style.css / main.js 等)。"
    "如果内容较少可全内联在 HTML 中(此时仅输出 index.html 一个文件)。\n"
    "【多页面站点】若需求含多个页面(如 首页/产品/关于我们/联系我们),请为每个页面生成独立 HTML 文件,"
    "英文 slug 命名(如 index.html、products.html、about.html、contact.html),文件间通过统一顶部导航相互链接,"
    "并保持相同的设计语言与 CSS/JS 引用;首页必须命名为 index.html。文件树会列出全部页面,用户可在右侧面板切换预览。"
    "不要输出 markdown 代码块围栏(```)、不要输出多余解释。\n"
    "⚠️ 若上方『必须生成的文件列表』给出了具体文件名,你必须【严格按该列表逐一生成】(每个文件以 <!-- FILE: 文件名 --> 开头),"
    "缺一不可;页面之间(尤其顶部导航)必须互相链接到这些真实文件名(如 <a href=\"products.html\">产品</a>),"
    "严禁使用 href=\"#\" 占位链接(预览中跳不动、会被评审判不通过)。\n\n"
    "【链接与资源硬约束——必须严格遵守】\n"
    "• 所有站内链接(href)与资源引用(src)必须使用【相对路径】,例如 page.html、styles/style.css、script.js;"
    "页面之间互相链接就用对方的文件名(about.html、contact.html),不要用带域名的绝对 URL。\n"
    "• 严禁写出 `/artifacts/...` 开头的本平台内部绝对路径;严禁为站内资源写 `http(s)://本站域名/...` 绝对地址。\n"
    "• 违反此约束会导致站内导航在预览中失效(404/跨域),请在生成时务必自查所有 `href`/`src` 均为相对引用。\n\n"
    "【样式与依赖硬约束——强制】\n"
    "• 平台已内置 SeedPremium 玻璃拟态设计系统(随页面自动内联,无需你手写基础样式)。"
    "你必须【只使用该系统提供的 class】(见下方白名单),不要自己从零堆样式,也不要重复定义 :root 变量。\n"
    "• 基础样式与交互首选 SeedPremium(玻璃拟态/卡片/按钮/进场动画已全部内置)。\n"
    "• 若确实需要额外框架/库(Vue/React/jQuery/Swiper/Bootstrap/Chart.js/Three.js 等),"
    "【只能从下方『本地组件库白名单』选取】,并一律用 /vendor/libs/<name>/<file> 根绝对路径引入"
    "(如 <script src=\"/vendor/libs/vue/vue.global.prod.js\">)。"
    "该目录已预置在服务器域名根,发布后全站共享、无需重复下载、离线可用。\n"
    "• 严禁再引入任何外部【JS/CSS 框架 CDN】: 包括但不限于 unpkg.com、cdn.jsdelivr.net、cdn.tailwindcss.com、"
    "cdnjs、code.jquery.com,以及 React / Vue / Babel / Tailwind Play CDN 等运行时。"
    "这些在离线/沙箱预览中不可达,会导致页面变灰块或白屏;需要它们请改用本地 /vendor/libs 白名单。\n"
    "• 字体: 仅使用系统字体栈(已内置在 SeedPremium 的 .display/h1/h2/h3/.lead 中),"
    "严禁引入任何外部字体 CDN(含 fonts.googleapis.com / fonts.gstatic.com / 各类 iconfont CDN);"
    "离线/沙箱预览无法加载外部字体,会回退但不可控。需要图标请用内联 SVG 或 SeedPremium 内置标记。\n"
    "• 严禁使用占位图服务(via.placeholder.com / placeholder.com / dummyimage 等),"
    "预览环境无法访问会显示灰块。图片请使用真实图片 URL(https 可达)或直接内联 SVG。\n\n"
    + VENDOR_REFERENCE + "\n\n"
    + (LIBS_REFERENCE + "\n\n" if LIBS_REFERENCE else "")
    + "【高级视觉与交互硬标准——必须满足】\n"
    "1. 视觉质感: 优先用 .glass / .card 玻璃拟态 + 柔和分层阴影 + 渐变光晕,克制留白;"
    "杜绝大色块平涂与廉价渐变。配色须经设计且符合 WCAG AA 对比度。\n"
    "2. 排版: 使用系统字体栈(已内置)或 Google Fonts,层级用 .display/h1/h2/h3/.lead 体现,"
    "字距与行高经过调校,呈现『编辑级』排版。\n"
    "3. 微交互: 按钮用 .btn / .btn-primary / .btn-ghost,加 data-magnetic=\"0.25\" 实现磁吸;"
    "卡片 .card 已有 hover 抬升;重要区块加 class=\"reveal\" 实现滚动渐显。\n"
    "4. 动效性能: 仅 animate transform/opacity,目标 60fps;尊重 prefers-reduced-motion(系统已处理)。\n"
    "5. 响应式: .grid-2/3/4 自动断点;移动端单列;触控目标 ≥44px(.btn 已保证)。\n"
    "6. 主题变量: 改 :root 的 --brand/--brand-2/--bg 等变量即可换肤;深色在 <html class=\"dark\"> 切换。\n"
    "7. 结构/可访问性: 语义化标签 + 必要 aria;英雄区(.hero)有强视觉焦点与清晰 CTA(.btn-primary)。\n"
    "8. 内容: 不输出 lorem 占位或灰底色块;每一屏都要有真实信息与精心排布的内容。"
)

SYS_CODER_GAME = (
    "你是一名游戏开发者。生成一个完整的单文件 HTML 互动小游戏。"
    "必须引入本地 Three.js(已预置,无需外网): "
    "<script src=\"/vendor/libs/three/three.min.js\"></script>。"
    "游戏要素: 3D/2D 场景 + 玩家控制(键盘+触屏) + 碰撞/得分 + 开始/重新开始按钮 + 操作提示。"
    "视觉打磨: 发光粒子 / HUD / 流畅帧率 / 赛博感配色;把 CSS/JS 全部内联,"
    "只输出完整 HTML,不要解释、不要 markdown 代码块围栏(```)。"
    "确保兼容移动端触屏操作和 PC 键盘操作。"
)
SYS_REVIEWER = (
    "你是严格的资深前端评审 + 设计总监。检查给定 HTML 是否:① 以 <html 开头且结构基本完整;"
    "② 标签基本闭合;③ 不含明显会白屏的致命错误(eval / 未定义脚本、外部不可达资源);"
    "④ 视觉与交互是否达到『高级感』: 有层次/留白/微交互/缓动,而非平涂色块或简陋排版;"
    "⑤ 颜色/排版/响应式/可访问性有无问题;⑥ 是否含危险内容/外部不可控脚本(safety);"
    "⑦【交互行为】用户要求的功能(按钮/导航/轮播/表单/弹窗/Tab 等)必须有真实 JS 事件绑定,"
    "且 DOM 选择器能匹配到元素: 逐项核对每个可见交互控件, 确认存在 addEventListener/onclick="
    "().querySelector(.+)/getElementById(.+) 之类绑定且 class/id 与 HTML 中一致;"
    "若页面含『点击 X 跳转/切换/提交』但找不到对应事件或选择器对不上, 视为未实现(不通过)。\n"
    "【评审范围说明】你看到的 HTML 可能是被截断的片段(尤其多文件站点只取了首个文件的前若干字符),"
    "因此『结构完整性(标签是否闭合 / 是否有 <html 根标签)』已由上游静态检查保证,你无需重复判断,"
    "也不要仅因『看不到结尾』就判不通过;请聚焦于可见内容的质量维度: 视觉质感 / 微交互 / 配色对比度 /"
    "排版层级 / 可访问性 是否达到『高级感』,以及是否存在明显会导致白屏的致命 JS 错误(eval / 未定义调用)。"
    "若可见部分无明显问题,应判 passed=true。\n"
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
    """同步调用模型(Planner/Reviewer)。主模型失败按 FALLBACK_ORDER 自动降级到下一可用模型,
    全部失败才抛 ModelUnavailableError。避免 deepseek 瞬时抖动导致短调用(规划/评审)直接中断整轮生成。"""
    from ..providers import resolve_fallback_order, PROVIDERS
    order = resolve_fallback_order(model_id)
    last_err: Exception | None = None
    for mid in order:
        try:
            chat = get_chat_model(mid, streaming=False)
            if mid != model_id:
                logger.warning("[gen] _chat 降级: %s 不可用, 改用 %s", model_id, mid)
            resp = chat.invoke([{"role": "system", "content": system}, *user_msgs])
            return resp.content
        except Exception as e:
            last_err = e
            continue
    suggested = [m for m in order if m != model_id and m in PROVIDERS and PROVIDERS[m].api_key]
    raise ModelUnavailableError(
        failed=model_id, message=f"模型 {model_id} 不可用: {last_err}", suggested=suggested
    ) from last_err


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


def _extract_page_files(req_text: str, plan_files: "list | None") -> list[str]:
    """从用户需求(显式点名的 *.html)与 Planner 文件列表提取本次应生成的页面文件名。

    用于把『多文件强约束』钉进 Coder prompt:保证多页面站点真的分文件输出、导航互相跳转。
    index.html 永远置首(平台约定入口)。
    """
    found: list[str] = []
    for m in re.findall(r"[\w\-]+\.html", req_text or "", re.IGNORECASE):
        f = m.strip().lower()
        if f not in found:
            found.append(f)
    for f in (plan_files or []):
        if isinstance(f, str):
            f = f.strip().lower()
            if f.endswith(".html") and f not in found:
                found.append(f)
    if not found:
        found = ["index.html"]
    if "index.html" in found:
        found.remove("index.html")
    found.insert(0, "index.html")
    return found


def _coder_with_files(base: str, page_files: list[str]) -> str:
    """把『本次必须生成的文件列表』钉进 Coder 系统提示(仅多文件时生效)。"""
    if len(page_files) <= 1:
        return base
    others = [f for f in page_files if f != "index.html"]
    return (
        base
        + "\n\n【本次必须生成的文件(严格按此列表,缺一不可)】\n"
        + "\n".join(f"• {f}" for f in page_files)
        + "\n每个文件必须以 `<!-- FILE: 文件名 -->` 单独标记开头(参考上方『多文件规范』)。"
        + "页面之间(尤其顶部导航)必须互相链接到这些真实文件名,例如 "
        + "、".join(f'<a href="{f}">页面</a>' for f in others)
        + ' 形式的链接;严禁使用 href="#" 占位链接(预览中跳不动、会被评审判不通过)。'
    )


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
    files_raw = data.get("files") or []
    files = [str(f).strip() for f in files_raw if isinstance(f, str) and str(f).strip()]
    return {"title": title, "goal": goal, "reasoning": reasoning, "steps": steps,
            "design_spec": design_spec, "files": files}


async def _review(model_id: str, html: str, intent: Optional[str] = None,
                expected_files: Optional[list] = None) -> Dict:
    """3-C: 静态分析 + LLM 自审(7 维, v1.2.0 统一打分)。

    返回含 needs_review 的评审结果, 供:
      - 生成内 Reflexion 修复循环(passed=False 时回退 Coder 重生成);
      - 后置 QC 按需触发(passed=True 但 needs_review=True 时升级三裁判复核, 见 core/queue.py)。

    🔧 C7 修复: LLM 自审异常不再静默放过(passed=True 默认高分), 改为:
      - 调用失败(模型不可用)→ passed=False + needs_review=True, 触发修复循环 + QC 升级;
      - 输出无法解析(偶发脏输出)→ passed=True + needs_review=True, 放行但由 QC 二次复核。
      两者都不让缺陷被「静默放过」。
    """
    # 静态分析(快速硬规则)
    low = html.lower()
    if "<html" not in low or len(html) < 50:
        return {"passed": False, "comment": "缺少 <html 根标签或内容过短",
                "scores": {"correctness": 0, "completeness": 0, "readability": 0,
                           "compliance": 5, "efficiency": 5, "craft": 0, "safety": 8},
                "issues": ["缺少<html根标签"], "needs_review": True}
    if low.count("<script") > low.count("</script") or low.count("<style") > low.count("</style>"):
        return {"passed": False, "comment": "标签未闭合(<script>/<style>)",
                "scores": {"correctness": 2, "completeness": 5, "readability": 5,
                           "compliance": 5, "efficiency": 5, "craft": 3, "safety": 8},
                "issues": ["标签未闭合"], "needs_review": True}
    # C(#487): 静态交互校验 —— 仅当页面存在「必须 JS 才能工作」的控件(按钮 / 表单提交 /
    # Tab 切换 / 轮播 / 折叠菜单 等), 且 JS 里没有任何事件绑定时, 才判不通过, 触发 Reflexion
    # 让 Coder 补上交互逻辑(修复"按钮无法点击/不工作却过了 QC"的痛点)。
    # 注意: <a href> 链接、营销文案里的"点击/导航/跳转"等并**不**需要 JS, 必须排除,
    # 否则每个含导航链接的整站都会误判不通过 → 反复 Reflexion 拖垮建站耗时(实测 7~8min/站)。
    _has_interactive_controls = bool(
        re.search(
            r"<button|<input[^>]*type=[\"']?(?:button|submit|reset)|"
            r"data-(?:tab|toggle|target|action|accordion|carousel|slide)|"
            r"class=[\"'][^\"']*(?:hamburger|menu-toggle|dropdown|accordion|carousel|slider|tab)",
            html, re.IGNORECASE)
    )
    _has_js_binding = bool(
        re.search(r"addEventListener|onclick\s*=|querySelector|getElementById|\.on\w+\s*=", html, re.IGNORECASE)
        and "<script" in low
    )
    if _has_interactive_controls and not _has_js_binding:
        GEN_LOG.warning("[gen] 静态交互校验未通过: 存在交互控件但无 JS 事件绑定")
        return {"passed": False,
                "comment": "检测到的交互控件(按钮/导航/切换等)缺少 JS 事件绑定, 点击会无反应",
                "scores": {"correctness": 3, "completeness": 4, "readability": 5,
                           "compliance": 5, "efficiency": 5, "craft": 4, "safety": 8},
                "issues": ["交互控件缺少 JS 事件绑定(按钮/导航点击无反应)"], "needs_review": True}
    # #598 静态围栏: 禁止外部 JS/CSS 框架 CDN 与占位图(离线/沙箱预览不可达 → 白屏/灰块)。
    # 仅游戏意图允许 Three.js CDN, 其余一律禁止。匹配 src/href 里的域名关键字。
    _blocked_cdn = re.search(
        r"(unpkg\.com|cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com|cdn\.tailwindcss\.com|"
        r"reactjs\.org|react\.dev|babeljs\.io|code\.jquery\.com|via\.placeholder\.com|"
        r"placeholder\.com|dummyimage\.com|fonts\.googleapis\.com|fonts\.gstatic\.com|"
        r"fonts\.google\.com)",
        html, re.IGNORECASE)
    _is_game_whitelist = (intent == "game" and
                          re.search(r"/vendor/libs/three/three\.min\.js", html, re.IGNORECASE))
    if _blocked_cdn and not _is_game_whitelist:
        GEN_LOG.warning("[gen] 静态校验未通过: 含禁用外部 CDN/占位图 %s", _blocked_cdn.group(0))
        return {"passed": False,
                "comment": f"检测到禁用外部依赖({_blocked_cdn.group(0)}), 离线预览会变灰块/白屏, 须改用内置 SeedPremium",
                "scores": {"correctness": 4, "completeness": 5, "readability": 5,
                           "compliance": 2, "efficiency": 5, "craft": 3, "safety": 8},
                "issues": ["引用外部 CDN/框架/占位图(违反平台依赖白名单)"], "needs_review": True}
    # 多文件强校验: 若本次明确要求生成多个 HTML 文件(>1), 但输出未用 <!-- FILE: --> 分文件,
    # 或导航里出现 href="#" 占位死链 → 判不通过, 触发 Reflexion 让 Coder 补充分文件/真实跳转。
    _exp_html = [f for f in (expected_files or []) if str(f).lower().endswith(".html")]
    if len(_exp_html) > 1:
        if not re.search(r'<!--\s*FILE:\s*.+?\s*-->', html, re.IGNORECASE):
            GEN_LOG.warning("[gen] 静态校验未通过: 要求多文件但无 <!-- FILE: --> 标记")
            return {"passed": False,
                    "comment": "需求要求生成多页面/多文件,但输出未使用 <!-- FILE: 文件名 --> 分文件标记,须按规范分文件输出",
                    "scores": {"correctness": 4, "completeness": 4, "readability": 5,
                               "compliance": 3, "efficiency": 5, "craft": 3, "safety": 8},
                    "issues": ["未按多文件规范输出(缺少 <!-- FILE: --> 标记)"], "needs_review": True}
        # 导航/菜单里的占位死链 href="#" 在多页站点会导致跳转失效
        _nav_dead = re.search(
            r'(<nav|class\s*=\s*["\'][^"\']*nav|class\s*=\s*["\'][^"\']*menu|navbar)'
            r'[\s\S]{0,900}?href\s*=\s*["\']#["\']',
            html, re.IGNORECASE)
        if _nav_dead:
            GEN_LOG.warning("[gen] 静态校验未通过: 导航含 href=\"#\" 死链")
            return {"passed": False,
                    "comment": "导航/菜单中存在 href=\"#\" 占位死链,多页站点必须互相链接到真实文件名(如 products.html)",
                    "scores": {"correctness": 4, "completeness": 5, "readability": 5,
                               "compliance": 3, "efficiency": 5, "craft": 3, "safety": 8},
                    "issues": ["导航含 href=\"#\" 死链(应改为相对文件名跳转)"], "needs_review": True}
    # LLM 自审(给 JSON 结论)
    try:
        t0r = time.monotonic()
        # LLM 自审片段: 多文件时取首个完整文件(index.html), 且给足长度(整文件, 上限 24k),
        # 避免截断导致误判"未闭合/不完整"。结构完整性已由上方静态检查保证。
        _rev_files = _parse_multi_files(html)
        _rev_doc = _rev_files.get("index.html", html) if len(_rev_files) > 1 else html
        _llm_slice = _rev_doc[:24000]
        out = await asyncio.to_thread(_chat, model_id, SYS_REVIEWER, [{"role": "user", "content": _llm_slice}])
        await record_llm_call(model_id, True, (time.monotonic() - t0r) * 1000)
        m = re.search(r"\{.*\}", out, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            scores = parse_scores(data)  # 7 维, 缺失/异常填 0
            reason = _review_reason({"passed": bool(data.get("passed")), "issues": data.get("issues", []) or [], "comment": data.get("comment", "")})
            logger.info("[gen] Reviewer 自审 passed=%s reason=%s overall=%.2f",
                        bool(data.get("passed")), reason,
                        sum(scores.values()) / max(len(scores), 1))
            return {
                "passed": bool(data.get("passed")),
                "comment": data.get("comment", ""),
                "scores": scores,
                "issues": data.get("issues", []) or [],
                "needs_review": (not bool(data.get("passed"))) or needs_review(scores),
            }
        # 输出无 JSON: 不放行, 但也不强制重生成(偶发脏输出), 标记待 QC 复核
        logger.warning("[gen] Reviewer 输出无解析 JSON, 标记待复核 reason=parse_fail")
        return {"passed": True, "comment": "评审输出无法解析, 已标记待复核",
                "scores": {"correctness": 5, "completeness": 5, "readability": 5,
                           "compliance": 6, "efficiency": 6, "craft": 5, "safety": 6},
                "issues": ["评审输出异常"], "needs_review": True}
    except Exception as e:
        # 🔧 C7 修复: 调用失败 → 标记未通过, 触发修复循环 + QC 升级(不再静默 passed=True)
        await record_llm_call(model_id, False, (time.monotonic() - t0r) * 1000,
                              error_type=type(e).__name__)
        logger.warning("[gen] Reviewer LLM 自审失败, 标记未通过待复核 reason=llm_fail: %s", e)
        return {"passed": False, "comment": "评审模型调用失败, 已标记待复核",
                "scores": {"correctness": 5, "completeness": 5, "readability": 5,
                           "compliance": 6, "efficiency": 6, "craft": 5, "safety": 6},
                "issues": ["评审模型调用失败"], "needs_review": True, "llm_fail": True}


def _parse_multi_files(raw: str) -> dict[str, str]:
    """解析多文件输出: 按 `<!-- FILE: 文件名 -->` 标记拆分。
    若未检测到标记, 则整个作为 index.html 返回(兼容旧单文件格式)。
    """
    import re
    pattern = re.compile(r'<!--\s*FILE:\s*(.+?)\s*-->\n?', re.IGNORECASE)
    parts = pattern.split(raw)
    files: dict[str, str] = {}
    if not parts or not pattern.search(raw):
        # 旧格式: 单文件 index.html
        files['index.html'] = raw.strip()
        return files
    # parts[0] 是第一个标记前的内容(应忽略), parts[1]=文件名, parts[2]=内容, ...
    for i in range(1, len(parts), 2):
        fname = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ''
        if fname and content:
            files[fname] = content
    if not files:
        files['index.html'] = raw.strip()
    return files


def _assemble_marked(files: "dict[str, str]") -> str:
    """把 {文件名: 内容} 组装成带 <!-- FILE: 文件名 --> 标记的单段 HTML,
    供 _deliver / _parse_multi_files / 评审消费。单文件时直接返回内容(与原行为一致)。"""
    if len(files) <= 1:
        return next(iter(files.values()), "")
    return "\n\n".join(f"<!-- FILE: {f} -->\n{c}" for f, c in files.items())


def _page_instruction(fname: str, page_files: "list[str]") -> str:
    """多文件场景: 让 Coder 只生成『当前这一个文件』,用相对文件名互相链接。
    关键: 不依赖模型自己切分(实测 deepseek 经常漏写 <!-- FILE: --> 标记),
    由我们在 _assemble_marked 里统一组装,保证多页站真的分文件落盘。"""
    others = [f for f in page_files if f != fname]
    nav_list = "、".join(page_files)
    return (
        f"【本次只生成文件 `{fname}`】这是多页面网站的一部分,整站包含以下页面: {nav_list}。\n"
        f"要求:\n"
        f"1. 只输出 `{fname}` 这一个文件的【完整 HTML】(从 <!DOCTYPE html> 到 </html>),"
        f"不要写 `<!-- FILE: -->` 标记,也不要用 markdown 代码围栏包裹。\n"
        f"2. 页面顶部必须有导航栏,用【相对文件名】链接到本站其他页面"
        f"(如 <a href=\"products.html\">产品</a>),禁止 href=\"#\" 占位链接。\n"
        f"3. 本站其他页面为: {('、'.join(others) if others else '无')}。"
        f"各页面导航栏与视觉风格必须一致(同一品牌/配色/字体)。\n"
        f"4. 首页(index.html)导航应覆盖全部页面;子页导航同样要能跳回首页及其他页。"
    )


async def _gen_pages(out: "dict[str, str]", page_files: "list[str]", base_user_msgs: list,
                   system: str, model_id: str, is_cancelled, trace_id: str) -> "AsyncGenerator":
    """生成整站。多文件=每页一次 Coder 调用(可靠切分);单文件=一次调用(行为不变)。
    逐页 yield gen_file / token / degraded 事件,并把每页 HTML 写入 out[fname]。"""
    for fname in page_files:
        yield ev("gen_file", name=fname)
        yield ev("node", stage="enter_coder")
        GEN_LOG.info("[gen] Coder(页) 开始 trace=%s file=%s", trace_id, fname)
        user_msgs = list(base_user_msgs)
        if len(page_files) > 1:
            user_msgs = user_msgs + [{"role": "user", "content": _page_instruction(fname, page_files)}]
        parts: list = []
        async for chunk, mid in astream_with_fallback(model_id, user_msgs, system=system):
            if await _cancelled_now(is_cancelled):
                yield ev("aborted")
                return
            text = getattr(chunk, "content", chunk)
            if text:
                parts.append(text)
                yield ev("token", data=text)
        if mid != model_id:
            yield ev("degraded", model=mid, requested=model_id)
        out[fname] = _extract_html("".join(parts))
        GEN_LOG.info("[gen] Coder(页) 完成 trace=%s file=%s chars=%s", trace_id, fname, len(out[fname]))


def _normalize_relative_links(html: str, filenames: "set[str]") -> str:
    """q-2: 把页面内绝对路径链接/资源重写为相对路径, 把多页跳转钉死。

    问题: Coder 偶尔会写出 `/artifacts/{uid}/{pid}/...` 绝对路径或 `http(s)://` CDN 域的
    站内资源链接, 经 nginx 同源直出时会导致站内导航/资源 404 或跨域破坏预览。
    处理:
      - 剥离本平台 artifacts 绝对前缀 `/artifacts/...` → 相对文件名(站点内互相引用);
      - 站内 href/src 指向已知生成文件名(index.html/about.html/style.css...)统一相对化;
      - 真正的外部链接(http/https/mailto/tel/# 锚点)一律保留不动。
    兜底: 即便模型仍写绝对路径, 落盘后此处强制修正, 保证预览同域 iframe 内跳转正确。
    """
    if not html:
        return html
    filename_set = {f for f in filenames if f.lower().endswith(('.html', '.css', '.js', '.png', '.jpg', '.jpeg', '.svg', '.webp', '.gif', '.json', '.md', '.txt'))}
    # 匹配引号内的 src/href 属性值(捕获引号类型)
    attr_re = re.compile(r'(?P<attr>src|href)\s*=\s*(?P<q>["\'])(?P<val>[^"\']*?)(?P=q)', re.IGNORECASE)

    def _rewrite(m: "re.Match") -> str:
        attr, q, val = m.group("attr"), m.group("q"), m.group("val")
        val_stripped = val.strip()
        # 保留: 锚点 / 协议链接 / 特殊协议 / 数据 URI / 空
        if (not val_stripped
                or val_stripped.startswith("#")
                or val_stripped.startswith("data:")
                or val_stripped.startswith("mailto:")
                or val_stripped.startswith("tel:")
                or val_stripped.startswith("javascript:")
                or re.match(r"^[a-z][a-z0-9+.-]*://", val_stripped, re.IGNORECASE)):
            return m.group(0)
        # 剥离本平台 artifacts 绝对前缀(/artifacts/{uid}/{pid}/... 或 /artifacts/...)
        norm = val_stripped
        if norm.startswith("/artifacts/"):
            norm = norm[len("/artifacts/"):]
            # 继续去掉可能存在的 uid/pid/ver 段, 只留文件名(站内引用)
            parts = [p for p in norm.split("/") if p not in ("", ".", "..")]
            # 找到最后一个与已知文件名匹配处截断, 否则只取末段文件名作相对引用
            kept = None
            for i, p in enumerate(parts):
                if p in filename_set:
                    kept = "/".join(parts[i:])
                    break
            norm = kept if kept is not None else (parts[-1] if parts else norm)
        # 去掉前导 ./ 与多余绝对根
        norm = norm.lstrip("/")
        # 已是站点相对路径(不含 / 到根) -> 直接保留; 否则按文件名相对化
        if "/" in norm and not norm.split("/", 1)[0].endswith((".html",)):
            base_name = norm.split("/")[-1]
            norm = base_name
        if norm != val_stripped:
            GEN_LOG.warning("[rel-links] 链接已相对化: %s -> %s", val_stripped, norm)
            return f"{attr}={q}{norm}{q}"
        return m.group(0)

    return attr_re.sub(_rewrite, html)


async def _deliver(html: str, trace_id: str, user_id: int | None = None,
              project_id: int | None = None, version: int | None = None) -> AsyncGenerator[Dict, None]:
    """P1 改造: 本地优先落盘(不生成时传 COS),逐文件 yield 落盘进度,末帧 yield 相对路径字典。

    事件协议(供 proxy / 前端 exec-head 消费):
      ev("disk_save", filename=..., index=..., total=..., path=...)        # 单文件落盘完成
      ev("progress", pct=N, stage="disk_save", file=...)                   # 落盘中(预留进度)
      ev("deliver_done", data={"files": {fname: rel_path}})                # 全部完成

    本地布局: {ARTIFACT_DIR}/{uid}/{pid}/v{ver}/{fname} —— 与 nginx /artifacts/ 静态直出对齐。
    COS 上传推迟到「部署发布」(独立 P4 的 POST /api/deploy), 此阶段只存本地路径。
    相对路径(相对 ARTIFACT_DIR)同时供:
      - proxy 落库 Artifact.files[fname].path;
      - 前端拼 `${location.origin}/artifacts/{path}` 同源预览(零超长内容下发)。
    """
    from shared.artifacts import site_dir as _site_dir, to_rel_path, rel_path_for
    from shared.vendor import ensure_vendor

    files = _parse_multi_files(html)
    # q-2: 落盘后统一校验+修正页面内链接为相对路径, 把多页跳转钉死(见 _normalize_relative_links)。
    files = {f: _normalize_relative_links(c, files.keys()) for f, c in files.items()}
    # #599: 落盘前内联 SeedPremium 设计系统(CSS/JS), 保证预览离线/iframe 可用、
    # 且模型即使没引也不缺样式; 模型若已内联同样标记会幂等跳过。
    files = {
        f: (ensure_vendor(c) if f.lower().endswith((".html", ".htm")) else c)
        for f, c in files.items()
    }

    base = _site_dir(user_id, project_id, version)
    base.mkdir(parents=True, exist_ok=True)

    total = len(files)
    result_paths: dict[str, str] = {}
    idx = 0
    for fname, content in files.items():
        idx += 1
        safe = fname.replace('/', '_')
        # 本地落盘(始终进行): {uid}/{pid}/v{ver}/{fname}
        dst = base / safe
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        rel = rel_path_for(user_id, project_id, version, safe)
        result_paths[fname] = rel
        # 进度帧: 落盘完成(仍兼容前端原有的 disk_save 渲染)
        yield ev("progress", pct=int(idx / max(total, 1) * 100), stage="disk_save", file=fname)
        yield ev("disk_save", filename=fname, index=idx, total=total, path=rel,
                 url=f"/artifacts/{to_rel_path(dst)}")

    GEN_LOG.info("[deliver] 本地落盘完成 trace=%s ver=%s files=%d dir=%s",
                 trace_id, version, len(result_paths), str(base))
    yield ev("deliver_done", data={"files": result_paths})


# 评分维度中文标签(用于生成结果汇总文案)
_SCORE_LABELS = {
    "correctness": "正确性", "completeness": "完整性", "readability": "可读性",
    "compliance": "合规", "efficiency": "性能", "craft": "精致度",
}


def _build_generation_summary(plan: dict, review: dict | None, path: str | None,
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
    lines.append("- 交付产物：主入口 `index.html`（CSS/JS 可按需独立为 style.css / script.js 等文件）")
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
    lines.append("- 右侧预览面板可实时查看/下载；本地预览已就绪（点「部署发布」后可获公开分享直链）")
    lines.append("- 下载文件为本次生成版本（历史版本可按 `vN` 切换，每次生成自动留存）")
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
    """从 需求文档 → 含建站/内容语义的用户消息 → 对话摘要 挑选最优需求文本。

    返回 (text, source)。修复 RC1: 把"含建站/内容语义的用户消息"提到
    conversation_summary 之前 —— 对话摘要是 LLM 有损压缩, 可能丢消息/变空,
    不能作为需求真相源; 仅当没有候选, 或候选极短(如纯指令"帮我做个网站")时,
    才用 conversation_summary 补充上下文。
    """
    # 1) 结构化需求文档(最权威)
    if isinstance(requirement_doc, dict) and requirement_doc:
        report = requirement_doc.get("report")
        if isinstance(report, str) and report.strip():
            return report.replace("\\n", "\n"), "requirement_doc"
        return _req_doc_to_text(requirement_doc), "requirement_doc"
    # 2) 从消息里找"含建站或内容语义"的用户消息(放宽: 命中任一类关键词即入选,
    #    但纯内容词需 >=2 个, 避免 "今天天气怎么样" 这类单发闲聊被误判为需求)
    candidates = []
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content") or ""
        if not isinstance(c, str) or not c.strip():
            continue
        has_build = any(kw in c for kw in _BUILD_KW)
        content_hits = sum(1 for kw in _CONTENT_KW if kw in c)
        # 入选条件: 含建站词, 或含 >=2 个内容词(避免单发闲聊被误判)
        if has_build or content_hits >= 2:
            score = (10 if has_build else 0) + content_hits * 3 + len(c) // 20
            candidates.append((c, score))
    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        best = candidates[0][0]
        # 纯指令句(如"帮我做个网站", 长度<12)无具体内容, 用摘要补充上下文。
        # 但**含建站词(has_build)的候选本身已表达明确建站意图**(如"我想做个水果电商网站"),
        # 不应因字数短而回退到有损的 conversation_summary —— 摘要可能严重跑偏
        # (如历史含"查询天气,助手拒绝"时, 摘要会完全偏离建站主题, 导致方案文不对题, 见 07-29 复现)。
        best_has_build = any(kw in best for kw in _BUILD_KW)
        if (not best_has_build) and len(best) < 12 and isinstance(conversation_summary, str) and conversation_summary.strip():
            return conversation_summary.strip(), "conversation_summary"
        return best, "user_message"
    # 3) 兜底: 对话摘要(可能有损, 但比无中生有强)
    if isinstance(conversation_summary, str) and conversation_summary.strip():
        return conversation_summary.strip(), "conversation_summary"
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

    # 多文件强约束: 从用户需求(显式点名的 *.html)提取页面文件列表,钉进 Coder prompt,
    # 保证多页面站点真的分文件输出、导航互相跳转。Planner 完成后用其 files 列表精修。
    page_files = _extract_page_files(req_text, [])
    coder_prompt_eff = _coder_with_files(coder_prompt, page_files)

    # ② 需求闸门(修复 RC2): 选中需求为空, 或无建站语义且无摘要时, 不发"垃圾站",
    #    直接 clarify 早退, 引导用户补充明确需求。
    _has_site = any(kw in req_text for kw in _BUILD_KW)
    _summary_ok = isinstance(conversation_summary, str) and conversation_summary.strip()
    if not req_text.strip() or (not _has_site and not _summary_ok):
        GEN_LOG.warning(
            "[gen] 需求闸门拦截 trace=%s source=%s has_site=%s: 需求为空或无建站语义, 转 clarify",
            trace_id, req_source, _has_site,
        )
        yield ev(
            "clarify",
            questions=["为了给您生成合适的网站，请补充一下需求～"],
            freeTextHint="可以告诉我：网站类型 / 主要页面 / 核心功能 / 行业风格。例如：做一个XX品牌官网，需要首页、产品列表、关于我们。",
        )
        return

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
            yield ev("gen_file", name="index.html")
            plan_msgs = [{"role": "user", "content": plan.get("goal", "")}]
            user_msgs = [{"role": "user", "content": f"需求规格:\n{json.dumps(plan, ensure_ascii=False)}"}]
            if req_text:
                user_msgs.append({"role": "user", "content": f"【用户原始需求(来源: {req_source})】\n{req_text}"})
            user_msgs = user_msgs + list(messages)
            # 重新执行 Coder
            html_parts = []
            async for chunk, _ in astream_with_fallback(model_id, user_msgs, system=coder_prompt_eff):
                if await _cancelled_now(is_cancelled):
                    yield ev("aborted"); return
                text = getattr(chunk, "content", chunk)
                if text: html_parts.append(text); yield ev("token", data=text)
            html = _extract_html("".join(html_parts))
            # 进 Reviewer r1
            for attempt in range(3):
                yield ev("node", stage="enter_reviewer", attempt=attempt + 1)
                review = await _review(model_id, html, expected_files=page_files)
                await record_reviewer(SKILL_NAME, review, reason=_review_reason(review))
                GEN_LOG.info("[gen] Reviewer 第%s轮(恢复) trace=%s passed=%s llm_fail=%s", attempt + 1, trace_id, review["passed"], review.get("llm_fail", False))
                if review["passed"]:
                    yield ev("think", stage="reviewer", passed=True, comment=review["comment"])
                    break
                if review.get("llm_fail"):
                    GEN_LOG.warning("[gen] Reviewer 第%s轮(恢复) llm_fail, 跳过 Reflexion 交 QC trace=%s", attempt + 1, trace_id)
                    yield ev("think", stage="reviewer", passed=False, comment="评审模型调用失败, 已跳过自动修复并交后置 QC 复核", llm_fail=True)
                    break
                yield ev("node", stage="enter_coder", retry=True)
                fix_msgs = [{"role": "user", "content": f"上一版未通过:{review['comment']}\n修正 HTML:\n{html[:8000]}"}]
                hp = []
                async for chunk, _ in astream_with_fallback(model_id, fix_msgs, system=coder_prompt_eff):
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
                review = await _review(model_id, html, expected_files=page_files)
                await record_reviewer(SKILL_NAME, review, reason=_review_reason(review))
                if review["passed"] or review.get("llm_fail") or a >= 2:
                    yield ev("think", stage="reviewer", passed=review["passed"],
                             comment=review.get("comment", ""), llm_fail=review.get("llm_fail", False))
                    break
                yield ev("think", stage="reviewer", passed=False, comment=review["comment"])
                yield ev("node", stage="enter_coder", retry=True)
                fix_msgs = [{"role": "user", "content": f"修正:{review['comment']}\nHTML:\n{html[:8000]}"}]
                hp = []
                async for chunk, _ in astream_with_fallback(model_id, fix_msgs, system=coder_prompt_eff):
                    if await _cancelled_now(is_cancelled):
                        yield ev("aborted"); return
                    text = getattr(chunk, "content", chunk)
                    if text: hp.append(text); yield ev("token", data=text)
                html = _extract_html("".join(hp))

        # 收尾
        yield ev("review", data=review)   # 评审结果(7维+needs_review), 供后置 QC 按需复核
        yield ev("node", stage="previewing")
        paths: dict[str, str] = {}
        async for _d in _deliver(html, trace_id, user_id, project_id, version):
            if _d.get("event") == "deliver_done":
                paths = (_d.get("data") or {}).get("files", {}) or {}
            else:
                yield _d  # 透传 disk_save / progress 给前端进度条
        main_path = paths.get('index.html', '')
        # P1: 本地预览走 path(前端拼 ${origin}/artifacts/{path}), 不再下发 srcdoc 兜底内容。
        yield ev("preview", path=main_path, files=paths)
        with suppress(Exception):
            await asyncio.to_thread(save_memory, trace_id or "site", plan.get("title", "建站"), html[:1500], plan.get("steps", []))
        # 文字汇总: 让后端在返回文件的同时, 以文字形式给出本次生成结果说明(前端气泡展示)
        yield ev("refined", data=_build_generation_summary(plan, review, main_path, version, project_id, intent))
        yield ev("node", stage="done")
        return

    # ---------- 正常流程 ----------

    # ②-a RAG 增强: 需求文本已在上文 first_user_msg 准备好(含对话真实需求),
    #    带超时保护, Chroma 不可达时 5s 后跳过, 不阻塞生成
    rag_ctx = ""
    rag_hits: dict = {}
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future: Future[dict] = pool.submit(build_rag_context, first_user_msg, project_id, user_id)
            _rag = future.result(timeout=5.0)
            rag_ctx = _rag.get("text", "")
            rag_hits = _rag.get("hits", {})
    except FutureTimeout:
        GEN_LOG.warning("[gen] RAG 检索超时(>5s), 跳过增强 trace=%s", trace_id)
    except Exception as e:
        GEN_LOG.warning("[gen] RAG 检索失败, 跳过增强 trace=%s: %s", trace_id, e)
    # 观测性:SSE 反馈本次向量召回情况(服务 P3 向量真实作用 + P4 友好反馈)
    if rag_hits:
        _rag_msg = ("📚 向量库召回 → 组件库 {components} / 历史记忆 {memory} / "
                    "项目记忆 {project_memory} / 用户偏好 {user_preferences} / 错误模式 {error_patterns}").format(**rag_hits)
        yield ev("think", stage="rag", msg=_rag_msg, hits=rag_hits)

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
        t0p = time.monotonic()
        try:
            spec = await asyncio.to_thread(_chat, model_id, build_skill_sys(SYS_PLANNER, project_system_prompt), planner_msgs)
            await record_llm_call(model_id, True, (time.monotonic() - t0p) * 1000)
        except Exception as e:
            await record_llm_call(model_id, False, (time.monotonic() - t0p) * 1000,
                                  error_type=type(e).__name__)
            raise
        plan = _parse_plan(spec)
        # 用 Planner 给出的 files 列表精修多文件约束(需求里点名 + Planner 推断取并集)
        page_files = _extract_page_files(req_text, plan.get("files", []))
        coder_prompt_eff = _coder_with_files(coder_prompt, page_files)
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
        # D/#502: 不再暂停等待确认 —— 直接 PLANNER 完成后进 CODER 生成。
        # 去掉 await_confirm 断点: 之前此处 yield paused + return 会把下方 Coder/Reviewer/投递
        # 变成死代码, 且会让前端卡在「running/await_confirm」无法继续。
        # 现在方案以 plan 事件展示后即开始生成, 无需用户手动确认(用户要停可随时点停止)。

        # 2) Coder(流式): 多文件=逐页生成(可靠切分); 单文件=原单次调用
        GEN_LOG.info("[gen] Coder 开始 trace=%s model=%s pages=%d", trace_id, model_id, len(page_files))
        user_msgs = [{"role": "user", "content": f"需求规格:\n{spec}"}]
        if req_text:
            user_msgs.append({"role": "user", "content": f"【用户原始需求(来源: {req_source})】\n{req_text}"})
        user_msgs = user_msgs + list(messages)
        _files: dict[str, str] = {}
        token_count = 0
        async for _ev in _gen_pages(_files, page_files, user_msgs, coder_prompt_eff, model_id, is_cancelled, trace_id):
            if _ev.get("event") == "token":
                token_count += 1
            yield _ev
        html = _assemble_marked(_files)
        GEN_LOG.info(
            "[gen] Coder 完成 trace=%s chars=%s pages=%d chunks=%s model=%s",
            trace_id, len(html), len(_files), token_count, model_id,
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
            review = await _review(model_id, html, expected_files=page_files)
            await record_reviewer(SKILL_NAME, review, reason=_review_reason(review))
            GEN_LOG.info(
                "[gen] Reviewer 第%s轮 trace=%s passed=%s llm_fail=%s",
                attempt + 1, trace_id, review["passed"], review.get("llm_fail", False),
            )
            if review["passed"]:
                yield ev("think", stage="reviewer", passed=True, comment=review["comment"])
                break
            # 🔧 P4 修复: 评审因模型基础设施故障(llm_fail)而非 HTML 质量问题未通过时,
            #    不再触发 Coder Reflexion 整轮重生成, 直接放行交由后置 QC 二次复核, 避免烧多轮 LLM。
            if review.get("llm_fail"):
                GEN_LOG.warning(
                    "[gen] Reviewer 第%s轮 llm_fail, 跳过 Reflexion 交 QC trace=%s",
                    attempt + 1, trace_id,
                )
                yield ev("think", stage="reviewer", passed=False,
                         comment="评审模型调用失败, 已跳过自动修复并交后置 QC 复核", llm_fail=True)
                break
            # 检查取消(断点保存点 3: reviewer_rN)
            if await _cancelled_now(is_cancelled):
                yield ev("checkpoint", stage=f"reviewer_r{attempt}", data={
                    "plan": plan, "html": html, "attempt": attempt,
                })
                yield ev("paused", stage=f"reviewer_r{attempt}", progress=75 + attempt * 10)
                yield ev("done")
                return
            # Reflexion: 基于评审意见重新生成(多文件逐页 / 单文件单次, 行为一致)
            yield ev("node", stage="enter_coder", retry=True)
            _fix_user = list(user_msgs)
            _fix_user.append({
                "role": "user",
                "content": f"上一版未通过评审:{review['comment']}\n请按上述意见修正并重新生成整站(多页面需分别输出各文件)。",
            })
            _files2: dict[str, str] = {}
            async for _ev in _gen_pages(_files2, page_files, _fix_user, coder_prompt_eff, model_id, is_cancelled, trace_id):
                yield _ev
            html = _assemble_marked(_files2)

        # 4) 预览投递(P1 本地路径,§10)
        yield ev("node", stage="previewing")
        paths = {}
        async for _d in _deliver(html, trace_id, user_id, project_id, version):
            if _d.get("event") == "deliver_done":
                paths = (_d.get("data") or {}).get("files", {}) or {}
            else:
                yield _d  # 透传 disk_save / progress 给前端进度条
        main_path = paths.get('index.html', '')
        # P1: 本地预览走 path(前端拼 ${origin}/artifacts/{path}), 不再下发 srcdoc 兜底内容。
        GEN_LOG.info("[gen] 预览投递 trace=%s files=%d path=%s", trace_id, len(paths), main_path or "无")
        yield ev("preview", path=main_path, files=paths)

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
        yield ev("refined", data=_build_generation_summary(plan, review, main_path, version, project_id, intent))

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
    name="agent_generate_site",
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
    display_name="网站生成",
    avatar="🏗️",
    role="建站专家",
    description="生成单文件 HTML 网站/页面(Planner→Coder→Reviewer 多 agent,支持 RAG 增强与回退)",
)
