"""LLM 调用失败时的兜底文案与事件。

背景: skill 在 LLM 超时/报错时若只 yield `think`(临时思考面板, done 后清空),
用户最终会看到一条空回复(见 trace t19fabcc6cde8aaa0a1f1b936)。
本模块统一产出「思考提示 + refined 终版文案」, 其中 refined 会被前端 onRefined
写入主气泡并落库, 保证失败也有可见、可解释的道歉文案。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Dict

from ..events import ev

logger = logging.getLogger("ai_service.skills.fallback")


def llm_failure_reason(err: Exception) -> str:
    """把底层异常映射成用户可读的原因(不泄露内部细节)。"""
    msg = str(err).lower()
    if "timed out" in msg or "timeout" in msg:
        return "模型响应超时"
    if "429" in msg or "rate" in msg or "quota" in msg or "limit" in msg:
        return "模型服务繁忙（限流）"
    if "connection" in msg or "connect" in msg or "refused" in msg:
        return "无法连接模型服务"
    if "api" in msg and ("key" in msg or "auth" in msg or "token" in msg):
        return "模型鉴权失败"
    return "模型暂时不可用"


async def emit_llm_failure(
    model_id: str, err: Exception, agent_id: str = "agent",
) -> AsyncGenerator[Dict, None]:
    """产出失败兜底事件序列: think(过程可见) + refined(正式落库回复)。"""
    reason = llm_failure_reason(err)
    logger.warning("[兜底] LLM调用失败 model=%s reason=%s err=%s", model_id, reason, err)

    # 1) 临时思考面板提示(生成过程中可见, 解释当前状态)
    yield ev(
        "think", stage="analyst",
        content=f"⚠️ 抱歉，{reason}，本次未能生成结果。",
        agent_id=agent_id,
    )

    # 2) refined 终版文案 → 成为正式落库回复(用户必见, 刷新后仍在)
    text = (
        f"😔 抱歉，刚才请求模型时{reason}，没能生成内容。\n\n"
        "你可以这样继续：\n"
        "• 稍等几秒后重新发送这条消息再试一次；\n"
        "• 或者直接告诉我更具体的需求（例如「做一个餐厅官网，要有菜单和在线预订」），"
        "我也可以跳过详细文档直接为你生成。\n\n"
        "如果多次都失败，可能是模型服务暂时不稳定，请稍后再来，或换一个模型试试。"
    )
    yield ev("refined", data=text, agent_id=agent_id)
