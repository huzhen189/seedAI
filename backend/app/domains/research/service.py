from __future__ import annotations

from app.core.turn_context import TurnContext


class ResearchService:
    async def research(self, context: TurnContext) -> str:
        return f"已记录研究请求：{context.clean_message}。研究域将在可用数据源返回后提供带引用的结论。"


research_service = ResearchService()
