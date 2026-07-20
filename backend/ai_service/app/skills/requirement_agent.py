"""Requirement Agent: 多轮深度需求对话 + 可选多方案(无代码)。

流程:
  1. 收集信息(行业/风格/功能) — 信息不全反复追问
  2. 出 multiple 方案 → options 事件(给用户选)
  3. 用户选定 → 输出需求文档 JSON → 持久化
  4. 通知前端: requirement_doc 就绪 → 等"开始生成"
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Dict

from ..events import ev
from ..providers import get_chat_model
from ..registry import register_skill

AGENT_LOG = logging.getLogger("ai_service.requirement")

SYS_REQUIREMENT = (
    "你叫小胡，是智能建站助手的「需求分析师」。职责：通过对话深度挖掘用户建站需求。\n\n"
    "必须收集到以下信息才输出文档(缺任一项继续追问)：\n"
    "1. 行业/品牌名称\n2. 目标用户\n3. 至少 3 个页面结构\n4. 至少 2 个功能需求\n5. 设计风格偏好\n\n"
    "当信息完整时，输出需求文档 JSON:\n"
    '{"brand":{"name":"品牌名","slogan":"口号","intro":"品牌介绍(200字)"},'
    '"target_user":"目标用户描述","pages":[{"title":"首页","sections":[{"name":"hero","content":"文案"}]}],'
    '"features":["功能1","功能2"],"design_style":"简约/科技/复古","color_scheme":{"primary":"#xxx","bg":"#xxx"},'
    '"reference_sites":[],"status":"confirmed"}\n\n'
    "如果用户给了多个想法但自己不确定，可以出 3 个方案让用户选:\n"
    '输出: {"options":{"question":"方向选择","choices":['
    '{"id":"A","title":"方案名","desc":"简短描述","pros":"优点","cons":"缺点"},'
    '{"id":"B","title":"方案名","desc":"简短描述","pros":"优点","cons":"缺点"}]}}\n\n'
    "用户输入: "
)


@register_skill(
    name="requirement_agent",
    intent_tags=["需求", "建站", "规划"],
    handler=requirement_agent_handler,
    is_graph=False,
    description="需求分析: 深度对话收集需求, 出文档或方案选项",
)
async def requirement_agent_handler(
    model_id: str, messages: list, trace_id: str | None = None,
    is_cancelled=None, project_status: str = "draft", **kwargs,
) -> AsyncGenerator[Dict, None]:
    AGENT_LOG.info("[req] 需求分析 trace=%s status=%s msgs=%d", trace_id, project_status, len(messages))

    yield ev("node", stage="analyzing", agent_id="requirement_agent")
    yield ev("think", stage="analyst", content="正在分析您的需求，收集关键信息…",
             agent_id="requirement_agent")

    # 构造提示: 当前状态 + 历史消息
    status_hint = ""
    if project_status in ("draft", "planning"):
        status_hint = "当前阶段: 需求收集。请先追问用户关键信息。"

    req_msgs = [{"role": "user", "content": f"{status_hint}\n用户消息:\n" + m.get("content", "")}
                for m in messages if m.get("role") == "user"]

    chat = get_chat_model(model_id, streaming=False)
    resp = chat.invoke([{"role": "system", "content": SYS_REQUIREMENT}, *req_msgs])
    raw = (resp.content or "").strip()
    AGENT_LOG.info("[req] LLM完成 chars=%d", len(raw))

    # 解析输出
    m = __import__("re").search(r"\{[\s\S]*\}", raw)
    if not m:
        yield ev("token", data=raw, agent_id="requirement_agent")
        yield ev("think", stage="analyst", content="请告诉我更多关于您的项目…",
                 agent_id="requirement_agent")
        return
    data = json.loads(m.group(0))

    # 多选方案
    if "options" in data:
        opts = data["options"]
        AGENT_LOG.info("[req] 出多选方案 question=%s choices=%d",
                       opts.get("question"), len(opts.get("choices", [])))
        yield ev("think", stage="analyst", content=opts.get("question", "请选择一个方案"),
                 agent_id="requirement_agent")
        yield ev("options", question=opts.get("question"), choices=opts.get("choices", []),
                 agent_id="requirement_agent")
        return

    # 需求文档
    if "brand" in data and data.get("status") == "confirmed":
        AGENT_LOG.info("[req] 需求文档完成 brand=%s pages=%d features=%d",
                       data["brand"].get("name"), len(data.get("pages", [])), len(data.get("features", [])))
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
        return

    # 信息不全 → 追问
    AGENT_LOG.info("[req] 信息不全, 继续追问")
    yield ev("token", data=raw, agent_id="requirement_agent")
