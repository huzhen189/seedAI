"""Agent: agent_review(合并 review_agent + fix_agent · v1.0)。

定位: 代码评审+Bug修复一体。先评审发现所有问题→给出修复建议→提供修复后代码。
不做: 不从零生成代码(那是 agent_build 的事)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from typing import Optional

from ..events import ev
from ..providers import get_chat_model, astream_with_fallback
from ..registry import register_skill

AGT = logging.getLogger("ai_service.agent_review")

SYS_REVIEW_AND_FIX = (
    "你叫小胡，是代码评审+Bug修复专家，只做前端网页代码(HTML/CSS/JS)的审查和修复。\n\n"
    "## 强约束\n"
    "1. 你只做前端代码评审和Bug修复，不生成新项目、不设计架构、不写文档。\n"
    "2. 遇到超出能力范围的任务输出 {\"decision\":\"escalate\",\"reason\":\"...\"}。\n"
    "3. 需要更多信息时输出 {\"decision\":\"clarifying\",\"questions\":[...]}。\n"
    "4. 所有输出必须是 JSON 对象，代码放在 artifact.fixed_code 字段中。\n"
    "5. 修复后代码必须是完整可运行的 HTML/CSS/JS。\n"
    "6. 不得输出违反中国法律或公序良俗的内容。\n\n"
    "工作流程:\n"
    "1. 分析代码→列出所有问题(严重/中等/低)\n"
    "2. 每个问题给出修复建议和修复后代码\n"
    "3. 输出完整修复后代码\n\n"
    "输出 JSON(不要代码块围栏):\n"
    '{"decision":"done","agent_name":"agent_review","content":"评审摘要",'
    '"artifact":{"issues":[{"severity":"high|medium|low","desc":"...","fix":"..."}],'
    '"fixed_code":"修复后完整HTML"}}'
)


async def agent_review_handler(
    model_id: str, messages: list, trace_id: Optional[str] = None,
    is_cancelled=None, **kwargs,
) -> AsyncGenerator[dict, None]:
    yield ev("think", stage="reviewer", content="正在审查代码...")
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "")[:300]
            break
    msgs = [{"role": "user", "content": f"请评审并修复以下代码:\n{user_text}"}]
    full = []
    try:
        async for chunk, _ in astream_with_fallback(model_id, msgs, system=SYS_REVIEW_AND_FIX):
            if is_cancelled and await is_cancelled():
                yield ev("aborted"); return
            text = getattr(chunk, "content", chunk) if hasattr(chunk, "content") else str(chunk)
            if text: full.append(text); yield ev("token", data=text)
    except Exception as e:
        AGT.warning("agent_review 异常: %s", e)
        yield ev("error", data=str(e))
        return
    raw = "".join(full)
    # 尝试解析 JSON 输出
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and data.get("fixed_code"):
                yield ev("node", stage="fix_ready", data=data)
    except Exception:
        pass


register_skill(
    name="agent_review",
    display_name="评审修复",
    avatar="🔍",
    role="代码评审+修复",
    intent_tags=["修复","报错","bug","fix","review","评审","优化","检查","error"],
    handler=agent_review_handler,
    is_graph=False,
    description="代码评审+Bug修复: 发现问题并给出修复后代码",
)
