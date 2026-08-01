from __future__ import annotations

import logging

from app.core.turn_context import TurnContext
from app.llm import LLMError, chat_completion

logger = logging.getLogger("app.domains.chat")

_SYSTEM_PROMPT = (
    "你是 SeedAI 的高级建站助手，专注帮助用户规划、设计与生成优质网站。"
    "当用户在闲聊或澄清需求时，用简洁、专业、友好的中文回应；"
    "如果用户表达了明确的建站意图（如‘做个官网’‘生成作品集’），请引导其补充关键信息"
    "（行业、风格、核心板块、是否需要暗色模式等），不要在此直接生成整站。"
)


class ChatService:
    async def respond(self, context: TurnContext) -> str:
        user_text = context.clean_message or ""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        try:
            return await chat_completion(messages, temperature=0.8, max_tokens=768, timeout=30.0)
        except LLMError as exc:
            logger.warning("LLM 闲聊调用失败，使用降级回复: %s", exc)
            return _graceful_fallback(user_text)

    async def health(self) -> bool:
        from app.llm import get_llm_client

        return get_llm_client().available


def _graceful_fallback(user_text: str) -> str:
    if not user_text:
        return "你好，我是 SeedAI 建站助手。告诉我你想做什么网站，我来帮你规划与生成。"
    return (
        f"我已经收到你的消息：「{user_text}」。"
        "当前模型服务暂时不可用，稍后重试即可；如果你是想建站，直接告诉我行业和想要的风格就行。"
    )


chat_service = ChatService()
