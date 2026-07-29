"""Requirement Agent: 行业特化需求对话 + 可选多方案(无代码)。

流程:
  1. 根据行业只问 2~3 个关键问题(不问无关项)
  2. 出 multiple 方案 → options 事件
  3. 用户选定 → 输出需求文档 JSON
  4. 通知前端: requirement_doc 就绪 → 等"开始生成"
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Dict

from ..events import ev
from ..intent.common import build_skill_sys
from ..providers import get_chat_model
from ..registry import register_skill
from ._llm_fallback import emit_llm_failure
logger = logging.getLogger("ai_service.skills.agent_requirement")

AGENT_LOG = logging.getLogger("ai_service.requirement")

# ── 行业特化收集维度(只问该行业关键项,其余留给 AI 自由发挥) ──
# key 与 intent_classifier 的 industry 输出对齐(英文)
INDUSTRY_FOCUS: dict[str, str] = {
    "ecommerce": "需要了解: ①卖什么品类 ②是否需要购物车/支付/商品展示 ③品牌名和风格偏好。",
    "restaurant": "需要了解: ①餐厅类型/菜系 ②是否需要菜单/预约/地址 ③品牌名和风格偏好。",
    "personal": "需要了解: ①网站主题(博客/作品集/个人品牌) ②需要哪些页面板块 ③风格偏好。",
    "corp": "需要了解: ①公司业务简介 ②需要哪些页面(关于/服务/案例/联系) ③品牌名和风格偏好。",
    "edu": "需要了解: ①课程类型和受众 ②是否需要课程列表/师资/报名 ③品牌名和风格偏好。",
    "health": "需要了解: ①诊所/科室类型 ②是否需要预约/医生介绍/地址 ③品牌名和风格偏好。",
    "game": "需要了解: ①游戏类型和玩法 ②是否需要下载页/社区/排行榜 ③风格偏好。",
    "travel": "需要了解: ①目的地/线路类型 ②是否需要行程展示/预订/攻略 ③品牌名和风格偏好。",
    # 以下归为通用
    "tech": "需要了解: ①产品/服务简介 ②需要哪些页面和功能 ③品牌名和风格偏好。",
    "media": "需要了解: ①内容类型(视频/文章/图片) ②需要哪些板块 ③风格偏好。",
    "gov": "需要了解: ①部门/服务类型 ②需要哪些栏目(公告/办事/机构) ③风格偏好。",
    "finance": "需要了解: ①业务类型 ②需要哪些页面和功能 ③品牌名和风格偏好。",
    "other": "需要了解: ①做什么类型的网站 ②需要哪些页面和功能 ③品牌名和风格偏好。",
}

SYS_REQUIREMENT = (
    "你叫小胡，是智能建站助手的「高级产品经理（PM）」。你的职责不是简单记录，而是像资深 PM 一样，"
    "把用户模糊的想法拆解、补全、专业化，产出一份**详尽、专业、可直接指导设计与开发的产品需求报告（PRD）**。\n\n"
    "工作流程:\n"
    "1. 先按行业只问 2-3 个最关键的决策问题（见下方行业提问策略），不要问无关项。\n"
    "2. 信息足够后，一次性输出完整的产品需求文档 JSON（务必详尽，不要只给骨架）。\n"
    "3. 若用户方向不清、想法发散，可先给 2-3 个方向方案（options）让其选择。\n\n"
    "【详尽输出原则——核心要求】\n"
    "- 不要只列标题，要写出**有信息量的文案**：每个页面/区块/功能都要有实质描述、目的、关键内容。\n"
    "- 目标用户要写成具体画像（角色 / 年龄层 / 使用场景 / 痛点），禁止写成「广大用户」。\n"
    "- 功能必须分级：P0（必须有）、P1（重要）、P2（增强），并说明其业务价值。\n"
    "- 设计要具体到色值含义、字体气质、文案语气，而非「现代简约」这种空话。\n"
    "- 必须包含：项目背景与目标、成功指标（KPI）、用户故事、验收标准、风险与假设。\n"
    "- 用户没说的部分，按 PM 专业判断合理补全，并在文案中标注「（建议）」。\n"
    "- 至少 1 个页面、1 个功能即可输出，但要写细、写专业。\n\n"
    "【JSON 结构】只输出一个 JSON 对象，不要使用代码块标记。字段如下：\n"
    "{\n"
    '  "brand": {"name":"品牌名或留空","slogan":"口号或留空","intro":"一句话定位",'
    '"story":"品牌故事/差异化价值(2-4句,有感染力)"},\n'
    '  "project_goal": {"background":"项目背景(为什么做,2-4句)","objectives":["核心目标1","核心目标2"],'
    '"success_metrics":["成功指标/KPI1","成功指标2"]},\n'
    '  "target_users": [{"role":"用户角色","persona":"画像(年龄/身份/场景)","pain":"痛点","need":"核心诉求"}],\n'
    '  "user_stories": [{"story":"作为<角色>,我要<功能>,以便<价值>","priority":"P0/P1/P2"}],\n'
    '  "pages": [{"title":"页面名","goal":"该页面目标","sections":['
    '{"name":"区块名","purpose":"区块目的","content":"详细文案/内容描述","key_elements":["关键元素1"]}]}],\n'
    '  "features": [{"name":"功能名","description":"功能详细描述","priority":"P0/P1/P2","maps_to":"关联用户故事或页面"}],\n'
    '  "information_architecture": {"navigation":["主导航项1","导航项2"],"notes":"结构/层级说明"},\n'
    '  "design": {"style":"视觉风格(2-4词)","mood":"整体调性","color_scheme":'
    '{"primary":"#hex","secondary":"#hex","bg":"#hex","accent":"#hex","meaning":"各颜色含义"},'
    '"typography":"字体气质建议","tone":"文案语气"},\n'
    '  "non_functional": {"performance":"性能要求","security":"安全要求","compatibility":"兼容/响应式",'
    '"accessibility":"可访问性"},\n'
    '  "acceptance_criteria": ["验收标准1","验收标准2"],\n'
    '  "milestones": [{"phase":"阶段名","scope":"范围说明","priority":"P0/P1/P2"}],\n'
    '  "risks": [{"risk":"风险或假设","mitigation":"应对措施"}],\n'
    '  "report": "一份连贯的产品需求报告 Markdown 长文：用 # 标题组织上述全部内容，语言专业、可读，'
    '像真实 PM 写给团队的 PRD。必须覆盖：项目概述、目标与指标、用户画像、用户故事、功能清单(含优先级)、'
    '信息架构、页面详细规划、设计规范、非功能需求、验收标准、里程碑、风险。report 字段内的换行请用 \\\\n 转义，'
    '不要出现未转义的换行符。",\n'
    '  "status": "confirmed"\n'
    "}\n\n"
    "若用户方向不清，可输出 options 而非上述文档：\n"
    '{"options": {"question":"方向选择","choices":['
    '{"id":"A","title":"方案名","desc":"一句话描述","pros":"优点","cons":"缺点"}]}}\n'
)


def _build_requirement_summary(data: Dict) -> str:
    """把结构化需求文档渲染成 Markdown 文字总结(供 refined 事件进入对话主气泡),
    避免"只有文件卡片、没有文字总结"的问题。"""
    brand = data.get("brand") or {}
    goal = data.get("project_goal") or {}
    design = data.get("design") or {}
    pages = data.get("pages") or []
    feats = data.get("features") or []
    users = data.get("target_users") or []
    ia = data.get("information_architecture") or {}
    nav = ia.get("navigation") or []

    lines: list[str] = []
    name = brand.get("name") or "未命名项目"
    lines.append(f"## 📋 需求文档已生成：{name}")
    slogan = brand.get("slogan")
    if slogan:
        lines.append(f"> {slogan}")
    lines.append("")

    objs = goal.get("objectives") or []
    bg = goal.get("background")
    if bg:
        lines.append(f"**项目背景**：{bg}")
    if objs:
        lines.append("**核心目标**：" + "；".join(objs))
    metrics = goal.get("success_metrics") or []
    if metrics:
        lines.append("**成功指标**：" + "、".join(metrics))
    lines.append("")

    if users:
        lines.append("**目标用户画像**：")
        for u in users[:3]:
            role = u.get("role", "")
            persona = u.get("persona", "")
            need = u.get("need", "")
            line = f"- {role}" + (f"（{persona}）" if persona else "")
            if need:
                line += f"：{need}"
            lines.append(line)
        lines.append("")

    if nav:
        lines.append("**信息架构**：" + " / ".join(nav))
        lines.append("")

    if pages:
        lines.append(f"**页面规划（{len(pages)} 个）**：")
        for p in pages:
            secs = p.get("sections") or []
            suffix = f"（{len(secs)} 个区块）" if secs else ""
            lines.append(f"- **{p.get('title', '')}**：{p.get('goal', '')}{suffix}")
        lines.append("")

    if feats:
        prio: dict[str, list] = {}
        for f in feats:
            if isinstance(f, dict) and f.get("priority"):
                prio.setdefault(f["priority"], []).append(f.get("name", ""))
        lines.append(f"**功能清单（{len(feats)} 项）**：")
        for p in ("P0", "P1", "P2"):
            if prio.get(p):
                items = [x for x in prio[p] if x]
                if items:
                    lines.append(f"- {p}：" + "、".join(items))
        lines.append("")

    style = design.get("style") or data.get("design_style")
    if style:
        lines.append(f"**设计风格**：{style}")
    color = (design.get("color_scheme") or {}).get("primary")
    if color:
        lines.append(f"**主色**：{color}")
    tone = design.get("tone")
    if tone:
        lines.append(f"**文案语气**：{tone}")
    lines.append("")
    lines.append("---")
    lines.append("✅ 需求分析阶段已完成（100%）。点击下方「开始建站」按钮，或直接回复「开始生成 / 帮我做网站」即可进入设计与开发。")
    return "\n".join(lines)


async def requirement_agent_handler(
    model_id: str, messages: list, trace_id: str | None = None,
    is_cancelled=None, project_status: str = "draft",
    industry: str = "other", requirement_doc: dict | None = None,
    project_system_prompt: str | None = None, **kwargs,
) -> AsyncGenerator[Dict, None]:
    AGENT_LOG.info("[需求] [1/4] 开始分析 trace=%s 行业=%s 状态=%s msgs=%d 已有文档=%s",
                   trace_id, industry, project_status, len(messages), "有" if requirement_doc else "无")

    yield ev("node", stage="analyzing", agent_id="requirement_agent")
    yield ev("think", stage="analyst", content="正在分析您的需求…",
             agent_id="requirement_agent")

    # 行业特化指令(注入 system prompt)
    focus = INDUSTRY_FOCUS.get(industry, INDUSTRY_FOCUS["other"])
    AGENT_LOG.info("[需求] [1/4] 行业特化 行业=%s 提问策略=%.80s", industry, focus)

    full_sys = build_skill_sys(
        f"{SYS_REQUIREMENT}\n当前行业: {industry}\n{focus}\n用户输入: ",
        project_system_prompt,
    )

    # ── 跨轮上下文增强(#510 折中方案): 仅并入 queue.py [3.6] 注入的 rel_ctx_msg ──
    # rel_ctx_msg 是 role=system 的「相关历史对话片段」, 原本被本 skill 的 user-only 过滤剔除;
    # 此处单独识别并并入 full_sys, 使 LLM 看到语义相关的跨轮历史, 且不重放 assistant 问答噪音(控成本)。
    _rel_ctx = next(
        (m.get("content", "") for m in messages
         if m.get("role") == "system" and "相关历史对话片段" in (m.get("content") or "")),
        "",
    )
    if _rel_ctx:
        full_sys = full_sys + "\n\n" + _rel_ctx
        AGENT_LOG.info("[需求] [1.5/4] 并入 rel_ctx_msg(跨轮语义历史) 长度=%d", len(_rel_ctx))
    else:
        AGENT_LOG.debug("[需求] [1.5/4] 无 rel_ctx_msg 可并入(本轮可能无相关历史)")

    req_msgs = [{"role": "user", "content": m.get("content", "")}
                for m in messages if m.get("role") == "user"]
    user_input = req_msgs[-1]["content"][:100] if req_msgs else "(无)"
    AGENT_LOG.info("[需求] [2/4] 调用LLM需求分析 model=%s input=%.100s", model_id, user_input)

    # ── 调试透出: 完整打印发送给 LLM 的结构体(排查跨轮上下文是否随请求发出) ──
    # 实际 request = [{"role":"system","content":full_sys}, *req_msgs]
    # 说明: rel_ctx_msg(跨轮语义历史) 已并入 full_sys(system); assistant(PM上一轮提问) 仍按
    #      user-only 过滤剔除。跨轮上下文经 [system(full_sys+rel_ctx) + user历史] 带入 LLM。
    AGENT_LOG.info(
        "[需求] [2/4] 发送LLM结构体 = [system(1,含rel_ctx=%s) + user(%d)] 共 %d 条; "
        "assistant 历史按 user-only 过滤剔除",
        bool(_rel_ctx), len(req_msgs), len(req_msgs) + 1,
    )
    # system 全文(用户要求全打)
    AGENT_LOG.info("[需求] [2/4] [system] ↓↓↓\n%s", full_sys)
    # 逐条 user 全文(跨轮用户消息即真实带入 LLM 的上下文)
    for _i, _m in enumerate(req_msgs):
        AGENT_LOG.info("[需求] [2/4] [user#%d/%d] ↓↓↓\n%s", _i + 1, len(req_msgs), _m["content"])
    # 预思考事件: 长文档生成期间保持 SSE 活跃, 避免前端误判无响应
    yield ev("think", stage="analyst", content="正在为您生成详细的需求文档（产品经理视角）…",
             agent_id="requirement_agent")

    t0 = time.time()
    try:
        chat = get_chat_model(model_id, streaming=False)
        resp = await asyncio.to_thread(chat.invoke, [{"role": "system", "content": full_sys}, *req_msgs])
    except Exception as e:
        AGENT_LOG.warning("[需求] [2/4] LLM调用失败/超时: %s", e)
        # 兜底: 既给临时思考提示, 也 yield refined 使道歉文案成为正式落库回复(用户必见, 否则是空气泡)
        async for _ev in emit_llm_failure(model_id, e, "requirement_agent"):
            yield _ev
        return
    raw = (resp.content or "").strip()
    AGENT_LOG.info("[需求] [2/4] LLM完成 耗时=%.0fms 输出长度=%d", (time.time() - t0) * 1000, len(raw))

    # 解析输出
    AGENT_LOG.info("[需求] [3/4] 解析LLM输出 raw=%.200s", raw)
    import re as _re
    data = None
    # 优先整段解析(模型若只输出纯 JSON, 避免 report 内字符干扰正则)
    try:
        data = json.loads(raw)
    except Exception:
        m = _re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if data is None:
        AGENT_LOG.info("[需求] [3/4] 未检测到JSON → 纯文本追问")
        yield ev("token", data=raw, agent_id="requirement_agent")
        yield ev("think", stage="analyst", content="请告诉我更多关于您的项目…",
                 agent_id="requirement_agent")
        return

    # 多选方案
    if "options" in data:
        opts = data["options"]
        AGENT_LOG.info("[需求] [3/4] 输出=多方案 问题=\"%s\" 选项数=%d",
                       opts.get("question"), len(opts.get("choices", [])))
        yield ev("think", stage="analyst", content=opts.get("question", "请选择一个方案"),
                 agent_id="requirement_agent")
        yield ev("options", question=opts.get("question"), choices=opts.get("choices", []),
                 agent_id="requirement_agent")
        AGENT_LOG.info("[需求] [4/4] 等待用户选择方案")
        return

    # 需求文档
    # 🔧 放宽门控: 只要 LLM 输出了结构化需求文档(含 brand 等核心字段)即视为可建站,
    #   不再强依赖 status=="confirmed"(多轮增量采集时模型很少主动置 confirmed, 会导致
    #   需求文档永不产出 → has_requirement_doc 恒为 False → 建站触发被死亡路由打回)。
    if "brand" in data:
        feats = data.get("features", [])
        feat_names = [f["name"] if isinstance(f, dict) else str(f) for f in feats]
        prio_summary: Dict[str, int] = {}
        for f in feats:
            if isinstance(f, dict) and f.get("priority"):
                prio_summary[f["priority"]] = prio_summary.get(f["priority"], 0) + 1
        prio_txt = " / ".join(f"{k}:{v}" for k, v in sorted(prio_summary.items())) or "—"
        goal_obj = data.get("project_goal") or {}
        design_obj = data.get("design") or {}
        n_pages = len(data.get("pages", []))
        n_feats = len(feat_names)
        style = design_obj.get("style") or data.get("design_style", "?")
        AGENT_LOG.info("[需求] [3/4] 输出=需求文档 品牌=%s 页面数=%d 功能数=%d 风格=%s 含报告=%s",
                       data["brand"].get("name", "?"), n_pages, n_feats, style, "report" in data)
        # 进度: 文档整理阶段
        yield ev("node", stage="doc_ready", agent_id="requirement_agent",
                 content=f"需求文档已生成完成（{n_pages} 个页面 / {n_feats} 项功能 / 风格「{style}」）")
        # 完成度提示(思考时间线)
        yield ev("think", stage="analyst",
                 content=f"✅ 需求分析阶段完成：共 {n_pages} 个页面、{n_feats} 项功能，优先级分布 {prio_txt}。",
                 agent_id="requirement_agent")
        yield ev("plan", title=data["brand"].get("name", "需求文档"),
                 goal=(goal_obj.get("background") or data.get("target_user", "")),
                 steps=[f"页面: {', '.join(p.get('title','') for p in data.get('pages',[]))}",
                        f"功能: {', '.join(feat_names)} (优先级 {prio_txt})"],
                 agent_id="requirement_agent")
        data["raw_llm_output"] = raw  # 持久化模型原始输出, 供下载端点拼入报告文件
        yield ev("requirement_doc", data=data, agent_id="requirement_agent")
        # 文字版总结(进入对话主气泡, 修复"只有文件没文字")
        summary_md = _build_requirement_summary(data)
        yield ev("refined", data=summary_md, agent_id="requirement_agent")
        # 主动咨询是否建站 —— 用纯文字提示(不阻塞/不锁定前端, D/#500)。
        # 任务到此正常结束(done), 等待用户二次回复; 跨轮 DST 已记录 build/requirement 意图,
        # 用户回复「开始生成 / 帮我做网站」会重新进入意图分类并被门控放行到生成器。
        yield ev("think", stage="analyst",
                 content="✅ 需求分析已完成。点击下方「开始建站」按钮，或直接回复「开始生成 / 帮我做网站」即可进入设计与开发。",
                 agent_id="requirement_agent")
        AGENT_LOG.info("[需求] [4/4] 需求文档已推送(纯文字CTA, 不阻塞)")
        return

    # 信息不全 → 追问
    AGENT_LOG.info("[需求] [3/4] 输出=追问(信息不全)")
    yield ev("token", data=raw, agent_id="requirement_agent")
    AGENT_LOG.info("[需求] [4/4] 已推送追问消息")

register_skill(
    name="agent_requirement",
    display_name="需求小胡", avatar="📋", role="需求分析师",
    intent_tags=["需求", "建站", "规划"],
    handler=requirement_agent_handler,
    is_graph=False,
    description="需求分析: 行业特化收集需求,出文档或方案选项",
)
