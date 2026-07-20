"""Requirement Agent: 行业特化需求对话 + 可选多方案(无代码)。

流程:
  1. 根据行业只问 2~3 个关键问题(不问无关项)
  2. 出 multiple 方案 → options 事件
  3. 用户选定 → 输出需求文档 JSON
  4. 通知前端: requirement_doc 就绪 → 等"开始生成"
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Dict

from ..events import ev
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
    "你叫小胡，是智能建站助手的「需求分析师」。职责：简洁高效地收集建站需求。\n\n"
    "原则:\n"
    "- 只问与行业相关的关键问题(2-3个),不问无关项\n"
    "- 品牌名/口号/200字介绍都不是必须的,用户不给就留空\n"
    "- 至少1个页面、1个功能即可输出文档\n"
    "- 用户回答模糊时,不要反复追问,直接按常识合理补充\n"
    "- 尽量一次性收集完毕,不要拖多轮\n\n"
    "当信息足够时输出需求文档 JSON:\n"
    '{{"brand":{{"name":"品牌名或留空","slogan":"口号或留空","intro":"一句话介绍或留空"}},'
    '"target_user":"目标用户(一句话)","pages":[{{"title":"页面名","sections":[{{"name":"区块","content":"占位文案"}}]}}],'
    '"features":["功能1"],"design_style":"风格(1-2词)","color_scheme":{{"primary":"#xxx","bg":"#xxx"}},'
    '"status":"confirmed"}}\n\n'
    "如果用户想法多且不确定方向,可出 2-3 个方案:\n"
    '{{"options":{{"question":"方向选择","choices":['
    '{{"id":"A","title":"方案","desc":"一句话","pros":"优点","cons":"缺点"}}]}}}}\n'
)


async def requirement_agent_handler(
    model_id: str, messages: list, trace_id: str | None = None,
    is_cancelled=None, project_status: str = "draft",
    industry: str = "other", requirement_doc: dict | None = None, **kwargs,
) -> AsyncGenerator[Dict, None]:
    AGENT_LOG.info("[需求] [1/4] 开始分析 trace=%s 行业=%s 状态=%s msgs=%d 已有文档=%s",
                   trace_id, industry, project_status, len(messages), "有" if requirement_doc else "无")

    yield ev("node", stage="analyzing", agent_id="requirement_agent")
    yield ev("think", stage="analyst", content="正在分析您的需求…",
             agent_id="requirement_agent")

    # 行业特化指令(注入 system prompt)
    focus = INDUSTRY_FOCUS.get(industry, INDUSTRY_FOCUS["other"])
    AGENT_LOG.info("[需求] [1/4] 行业特化 行业=%s 提问策略=%.80s", industry, focus)

    full_sys = f"{SYS_REQUIREMENT}\n当前行业: {industry}\n{focus}\n用户输入: "

    req_msgs = [{"role": "user", "content": m.get("content", "")}
                for m in messages if m.get("role") == "user"]
    user_input = req_msgs[-1]["content"][:100] if req_msgs else "(无)"
    AGENT_LOG.info("[需求] [2/4] 调用LLM需求分析 model=%s input=%.100s", model_id, user_input)

    t0 = time.time()
    chat = get_chat_model(model_id, streaming=False)
    resp = chat.invoke([{"role": "system", "content": full_sys}, *req_msgs])
    raw = (resp.content or "").strip()
    AGENT_LOG.info("[需求] [2/4] LLM完成 耗时=%.0fms 输出长度=%d", (time.time() - t0) * 1000, len(raw))

    # 解析输出
    AGENT_LOG.info("[需求] [3/4] 解析LLM输出 raw=%.200s", raw)
    m = __import__("re").search(r"\{[\s\S]*\}", raw)
    if not m:
        AGENT_LOG.info("[需求] [3/4] 未检测到JSON → 纯文本追问")
        yield ev("token", data=raw, agent_id="requirement_agent")
        yield ev("think", stage="analyst", content="请告诉我更多关于您的项目…",
                 agent_id="requirement_agent")
        return
    data = json.loads(m.group(0))

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
        AGENT_LOG.info("[需求] [3/4] 输出=需求文档 品牌=%s 页面数=%d 功能数=%d 风格=%s",
                       data["brand"].get("name", "?"), len(data.get("pages", [])),
                       len(data.get("features", [])), data.get("design_style", "?"))
        summary = [
            f"**品牌**: {data['brand'].get('name','?')}",
            f"**定位**: {data.get('target_user','?')}",
            f"**页面**: " + " → ".join(p.get("title", "?") for p in data.get("pages", [])),
            f"**功能**: " + ", ".join(data.get("features", [])),
            f"**风格**: {data.get('design_style','?')}",
        ]
        yield ev("think", stage="analyst", content="\n".join(summary), agent_id="requirement_agent")
        yield ev("plan", title=data["brand"].get("name", "需求文档"),
                 goal=data.get("target_user", ""),
                 steps=[f"页面: {', '.join(p.get('title','') for p in data.get('pages',[]))}",
                        f"功能: {', '.join(data.get('features',[]))}"],
                 agent_id="requirement_agent")
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
    name="requirement_agent",
    intent_tags=["需求", "建站", "规划"],
    handler=requirement_agent_handler,
    is_graph=False,
    description="需求分析: 行业特化收集需求,出文档或方案选项",
)
