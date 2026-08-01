"""SeedAI 新链路统一模型客户端。

只服务 S0–S9 Pipeline（不在 legacy app/agent 内）。OpenAI-compatible 端点，
当前内置 qwen（标准档，默认）与 deepseek（兜底）。所有调用均为异步，
失败时抛 LLMError，由上层决定降级策略，绝不静默吞错。
"""

from .client import LLMClient, LLMError, chat_completion, get_llm_client

__all__ = ["LLMClient", "LLMError", "chat_completion", "get_llm_client"]
