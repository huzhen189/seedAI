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

    req_msgs = [{"role": "user", "content": m.get("content", "")}
                for m in messages if m.get("role") == "user"]
    user_input = req_msgs[-1]["content"][:100] if req_msgs else "(无)"
    AGENT_LOG.info("[需求] [2/4] 调用LLM需求分析 model=%s input=%.100s", model_id, user_input)

    t0 = time.time()
    chat = get_chat_model(model_id, streaming=False)
    resp = await asyncio.to_thread(chat.invoke, [{"role": "system", "content": full_sys}, *req_msgs])
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
    if "brand" in data and data.get("status") == "confirmed":
        feats = data.get("features", [])
        feat_names = [f["name"] if isinstance(f, dict) else str(f) for f in feats]
        prio_summary: Dict[str, int] = {}
        for f in feats:
            if isinstance(f, dict) and f.get("priority"):
                prio_summary[f["priority"]] = prio_summary.get(f["priority"], 0) + 1
        prio_txt = " / ".join(f"{k}:{v}" for k, v in sorted(prio_summary.items())) or "—"
        goal_obj = data.get("project_goal") or {}
        design_obj = data.get("design") or {}
        AGENT_LOG.info("[需求] [3/4] 输出=需求文档 品牌=%s 页面数=%d 功能数=%d 风格=%s 含报告=%s",
                       data["brand"].get("name", "?"), len(data.get("pages", [])),
                       len(feat_names), design_obj.get("style") or data.get("design_style", "?"),
                       "report" in data)
        summary = [
            f"**品牌**: {data['brand'].get('name','?')}",
            f"**背景**: {goal_obj.get('background') or data.get('target_user','?')}",
            f"**目标**: " + "；".join(goal_obj.get("objectives", []) or [data.get("target_user", "?")]),
            f"**页面({len(data.get('pages', []))})**: " + " → ".join(p.get("title", "?") for p in data.get("pages", [])),
            f"**功能({len(feat_names)})**: " + ", ".join(feat_names),
            f"**优先级分布**: {prio_txt}",
            f"**风格**: {design_obj.get('style') or data.get('design_style','?')}",
            f"**报告**: {'已生成详细 PRD 报告' if 'report' in data else '（未生成）'}",
        ]
        yield ev("think", stage="analyst", content="\n".join(summary), agent_id="requirement_agent")
        yield ev("plan", title=data["brand"].get("name", "需求文档"),
                 goal=(goal_obj.get("background") or data.get("target_user", "")),
                 steps=[f"页面: {', '.join(p.get('title','') for p in data.get('pages',[]))}",
                        f"功能: {', '.join(feat_names)} (优先级 {prio_txt})"],
                 agent_id="requirement_agent")
        data["raw_llm_output"] = raw  # 持久化模型原始输出, 供下载端点拼入报告文件, 避免返回内容单调
        yield ev("requirement_doc", data=data, agent_id="requirement_agent")
        yield ev("paused", stage="await_confirm", plan_title=data["brand"].get("name", ""),
                 agent_id="requirement_agent")
        AGENT_LOG.info("[需求] [4/4] 需求文档已推送,等待用户确认")
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
