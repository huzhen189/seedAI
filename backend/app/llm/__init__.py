"""SeedAI 新链路统一模型客户端。

只服务 S0–S9 Pipeline（不在 legacy app/agent 内）。OpenAI-compatible 端点，
当前内置 qwen（标准档，默认）与 deepseek（兜底）。所有调用均为异步，
失败时抛 LLMError，由上层决定降级策略，绝不静默吞错。
"""

from typing import Optional, Sequence

from .client import LLMClient, LLMError, chat_completion, chat_completion_stream, get_llm_client

__all__ = [
    "LLMClient",
    "LLMError",
    "chat_completion",
    "chat_completion_stream",
    "get_llm_client",
    "list_models",
    "chat_completion_stream_with",
]


def list_models() -> list[dict[str, str]]:
    """已配置的可用模型列表（供前端模型选择器枚举）。"""
    return get_llm_client().list_providers()


async def chat_completion_stream_with(
    model_id: str | None,
    messages: Sequence[dict],
    *,
    temperature: float = 0.7,
    max_tokens: Optional[int] = 1024,
    timeout: float = 30.0,
    enable_thinking: bool = True,
    purpose: str | None = None,
):
    """按指定模型发起流式补全，回落默认链（见 LLMClient.chat_stream_with）。"""
    async for chunk in get_llm_client().chat_stream_with(
        model_id,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        enable_thinking=enable_thinking,
        purpose=purpose,
    ):
        yield chunk
