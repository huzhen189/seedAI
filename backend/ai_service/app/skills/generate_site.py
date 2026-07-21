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


GEN_LOG = logging.getLogger("ai_service.generate")


SYS_PLANNER = (
    "你负责把用户的建站需求拆解成结构化规格。请**只输出一个 JSON 对象**(不要代码块围栏、"
    "不要多余解释),字段如下:\n"
    "{\n"
    '  "title": "网站标题(简短,≤12字)",\n'
    '  "goal": "本次生成要达成的核心目标(1句话)",\n'
    '  "steps": ["步骤1", "步骤2", ...],   // 3~6 个有序执行步骤,每步一句话\n'
    '  "reasoning": "拆解思路与关键取舍(2~4 句自由文本)"\n'
    "}\n"
    "要求:板块划分 / 整体布局 / 视觉风格 / 技术选型建议都体现在 steps 与 reasoning 中;"
    '用中文;steps 为可执行的有序清单。'
)
SYS_CODER = (
    "你是一名资深前端工程师。根据用户需求(及上方需求规格),生成一个【单文件 HTML】,"
    "把 CSS 和 JS 全部内联在 <style> 和 <script> 中,可直接用 iframe 预览。"
    "只输出完整 HTML 代码,不要解释、不要 markdown 代码块围栏(```)。"
)

SYS_CODER_GAME = (
    "你是一名游戏开发者。生成一个完整的单文件 HTML 互动小游戏。"
    "必须引入 Three.js CDN: "
    "<script src=\"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js\"></script>。"
    "游戏要素: 3D/2D 场景 + 玩家控制(键盘+触屏) + 碰撞/得分 + 开始/重新开始按钮 + 操作提示。"
    "把 CSS/JS 全部内联,只输出完整 HTML,不要解释、不要 markdown 代码块围栏(```)。"
    "确保兼容移动端触屏操作和 PC 键盘操作。"
)
SYS_REVIEWER = (
    "你是严格的代码评审。检查给定 HTML 是否:① 以 <html 开头且结构基本完整;② 标签基本闭合;"
    "③ 不含明显会白屏的致命错误(eval / 未定义脚本、外部不可达资源)。"
    "先给结论 passed=true/false,再给一句中文修改建议(若未通过)。用 JSON 回复:"
    '{"passed": true/false, "comment": "..."}'
)

# 行业→设计约束(注入 Planner)
INDUSTRY_DESIGN: dict[str, str] = {
    "restaurant": "暖色系(橙/红), 大图Banner, 菜单卡片, 预约/订座按钮, 电话醒目, 食品照突出",
    "ecommerce": "商品网格布局, 搜索+筛选栏, 购物车图标, 促销标签, 评分星级, 分类导航",
    "gov": "蓝白/红白主色调, 庄重权威, 无障碍访问(aria标签), 公告栏置顶, 政务标识",
    "edu": "清新蓝绿, 课程卡片列表, 报名表单, 师资展示, 学生作品, 联系方式",
    "health": "柔和蓝白/米色, 信任感强, 预约挂号按钮, 医生卡片, 卫生标识, 保险提示",
    "finance": "深蓝/金色, 专业严谨, 数据图表, 合规声明, 安全标识, 客服入口",
    "game": "暗色/赛博朋克, 动效丰富, 全屏沉浸, 开始游戏大按钮, 操作提示, Three.js",
    "personal": "简约留白, 个人头像, 作品集卡片, 社交媒体链接, 时间线布局, 关于我",
    "corp": "品牌色主调, 大图+视频Hero, 案例/客户Logo墙, 联系方式醒目, 关于我们",
    "tech": "深色渐变, 产品截图/动图, 技术特性图标, CTA按钮, 代码风格, 功能介绍",
    "media": "视觉冲击, 引导关注, 瀑布流布局, 视频嵌入, 订阅入口, 社交分享",
    "other": "现代简约, 卡片布局, 响应式, 清新配色",
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
    return {"title": title, "goal": goal, "reasoning": reasoning, "steps": steps}


def _review(model_id: str, html: str) -> Dict:
    """3-C: 静态分析 + LLM 自审。"""
    # 静态分析(快速硬规则)
    low = html.lower()
    if "<html" not in low or len(html) < 50:
        return {"passed": False, "comment": "缺少 <html 根标签或内容过短"}
    if low.count("<script") > low.count("</script") or low.count("<style") > low.count("</style>"):
        return {"passed": False, "comment": "标签未闭合(<script>/<style>)"}
    # LLM 自审(给 JSON 结论,失败则按静态结果放过)
    try:
        out = _chat(model_id, SYS_REVIEWER, [{"role": "user", "content": html[:6000]}])
        import json
        import re

        m = re.search(r"\{.*\}", out, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return {"passed": bool(data.get("passed")), "comment": data.get("comment", "")}
    except Exception:
        pass
    return {"passed": True, "comment": "静态检查通过"}


def _deliver(html: str, trace_id: str) -> Optional[str]:
    """落盘本地产物并上传 COS,返回预览直链(失败返回 None,不阻断主流程)。"""
    try:
        from ..tools.cos_upload import cos_upload

        art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
        site_dir = art_dir / "anon" / (trace_id or "site")
        site_dir.mkdir(parents=True, exist_ok=True)
        idx = site_dir / "index.html"
        idx.write_text(html, encoding="utf-8")
        cos_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/anon/{trace_id or 'site'}/1/index.html"
        res = cos_upload(str(idx), cos_key)
        if res.get("ok"):
            return res.get("url")
    except Exception:
        pass
    return None


async def generate_stream(
    model_id: str,
    messages: list,
    trace_id: Optional[str] = None,
    is_cancelled=None,
    intent: Optional[str] = None,
    level2: Optional[str] = None,
    industry: Optional[str] = None,
    checkpoint: Optional[dict] = None,
    resume_mode: str = "resume",
    project_system_prompt: Optional[str] = None,
    **kwargs,
) -> AsyncGenerator[Dict, None]:
    # 根据意图选 Coder 系统提示(游戏 vs 建站), 并注入项目约束(Tier 1)
    base_coder = SYS_CODER_GAME if intent == "game" else SYS_CODER
    coder_prompt = build_skill_sys(base_coder, project_system_prompt)

    # 断点恢复入口(§7): 跳过已完成阶段
    if checkpoint:
        stage = checkpoint.get("stage", "")
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
            user_msgs = [{"role": "user", "content": f"需求规格:\n{json.dumps(plan, ensure_ascii=False)}"}] + messages
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
                review = _review(model_id, html)
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
                review = _review(model_id, html)
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
        yield ev("node", stage="previewing")
        url = _deliver(html, trace_id)
        yield ev("node", stage="preview", url=url, fallback="srcdoc" if not url else None)
        with suppress(Exception):
            save_memory(trace_id or "site", plan.get("title", "建站"), html[:1500], plan.get("steps", []))
        yield ev("node", stage="done")
        return

    # ---------- 正常流程 ----------

    # ②-a RAG 增强:带超时保护,Chroma 不可达时 5s 后跳过,不阻塞生成
    first_user_msg = ""
    for m in messages:
        if m.get("role") == "user":
            first_user_msg = m.get("content", "") or ""
            break
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
        spec = _chat(model_id, build_skill_sys(SYS_PLANNER, project_system_prompt), planner_msgs)
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
        user_msgs = [{"role": "user", "content": f"需求规格:\n{spec}"}] + messages
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
            review = _review(model_id, html)
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
        yield ev("node", stage="previewing")
        url = _deliver(html, trace_id)
        GEN_LOG.info("[gen] 预览投递 trace=%s url=%s", trace_id, url or "无(srcdoc 兜底)")
        yield ev("node", stage="preview", url=url, fallback="srcdoc" if not url else None)

        # ②-a 记忆闭环:生成成功后回写 memory 集合(供未来检索增强)
        with suppress(Exception):
            save_memory(
                trace_id or "site",
                plan.get("title", "建站"),
                html[:1500],
                plan.get("steps", []),
            )

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
    name="generate_site",
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
    description="生成单文件 HTML 网站/页面(Planner→Coder→Reviewer 多 agent,支持 RAG 增强与回退)",
)
