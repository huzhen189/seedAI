from __future__ import annotations

from app.core.turn_context import TurnContext


class ChatService:
    async def respond(self, context: TurnContext) -> str:
        return f"我已理解你的问题：{context.clean_message}"


chat_service = ChatService()
