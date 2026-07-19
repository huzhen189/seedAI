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
import os
from collections.abc import AsyncGenerator
from contextlib import suppress
from pathlib import Path
from typing import Dict, Optional

from ..events import ev
from ..providers import astream_with_fallback, get_chat_model
from ..rag import build_rag_context, save_memory
from ..registry import register_skill


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
SYS_REVIEWER = (
    "你是严格的代码评审。检查给定 HTML 是否:① 以 <html 开头且结构基本完整;② 标签基本闭合;"
    "③ 不含明显会白屏的致命错误(eval / 未定义脚本、外部不可达资源)。"
    "先给结论 passed=true/false,再给一句中文修改建议(若未通过)。用 JSON 回复:"
    '{"passed": true/false, "comment": "..."}'
)


def _chat(model_id: str, system: str, user_msgs: list) -> str:
    chat = get_chat_model(model_id, streaming=False)
    resp = chat.invoke([{"role": "system", "content": system}, *user_msgs])
    return resp.content


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
) -> AsyncGenerator[Dict, None]:
    # ②-a RAG 增强:取首条用户需求,检索 components + memory 注入 Planner 上下文
    first_user_msg = ""
    for m in messages:
        if m.get("role") == "user":
            first_user_msg = m.get("content", "") or ""
            break
    rag_ctx = build_rag_context(first_user_msg)

    # 1) Planner
    yield ev("node", stage="enter_planner")
    planner_msgs = [{"role": "user", "content": first_user_msg or (messages[-1].get("content", "") if messages else "")}]
    if rag_ctx:
        planner_msgs.append(
            {"role": "user", "content": f"【参考上下文(组件库 / 历史记忆)】\n{rag_ctx}"}
        )
    spec = _chat(model_id, SYS_PLANNER, planner_msgs)
    plan = _parse_plan(spec)
    # 思考流:Planner 的拆解思路(分步思考的一部分)
    if plan.get("reasoning"):
        yield ev("think", stage="planner", content=plan["reasoning"])
    # 特殊节点:大计划 / 目标(title/goal/steps),前端渲染为「计划 / 流程」卡片
    yield ev(
        "plan",
        title=plan.get("title", ""),
        goal=plan.get("goal", ""),
        steps=plan.get("steps", []),
    )

    # 2) Coder(流式,带模型降级 2-C)
    yield ev("node", stage="enter_coder")
    user_msgs = [{"role": "user", "content": f"需求规格:\n{spec}"}] + messages
    used_model = model_id
    html_parts: list = []
    async for chunk, mid in astream_with_fallback(model_id, user_msgs, system=SYS_CODER):
        if await _cancelled_now(is_cancelled):
            yield ev("aborted")
            return
        used_model = mid
        text = getattr(chunk, "content", chunk)
        if text:
            html_parts.append(text)
            yield ev("token", data=text)
    if used_model != model_id:
        yield ev("degraded", model=used_model, requested=model_id)
    html = _extract_html("".join(html_parts))

    # 3) Reviewer + Reflexion(≤3 轮)
    for attempt in range(3):
        yield ev("node", stage="enter_reviewer", attempt=attempt + 1)
        review = _review(model_id, html)
        yield ev("think", stage="reviewer", passed=review["passed"], comment=review["comment"])
        if review["passed"]:
            break
        # Reflexion: 让 Coder 基于评审建议修正
        yield ev("node", stage="enter_coder", retry=True)
        fix_msgs = [
            {
                "role": "user",
                "content": f"上一版未通过评审:{review['comment']}\n请修正以下 HTML:\n{html[:8000]}",
            }
        ]
        html_parts = []
        fix_used = model_id
        async for chunk, mid in astream_with_fallback(model_id, fix_msgs, system=SYS_CODER):
            if await _cancelled_now(is_cancelled):
                yield ev("aborted")
                return
            fix_used = mid
            text = getattr(chunk, "content", chunk)
            if text:
                html_parts.append(text)
                yield ev("token", data=text)
        if fix_used != model_id:
            yield ev("degraded", model=fix_used, requested=model_id)
        html = _extract_html("".join(html_parts))

    # 4) 预览投递(COS 直链,§10)
    yield ev("node", stage="previewing")
    url = _deliver(html, trace_id)
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
