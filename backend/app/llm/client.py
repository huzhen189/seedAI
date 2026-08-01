"""新链路 LLM 客户端实现。

设计约束（对照全链路重构规范）：
- 仅依赖 OpenAI-compatible 协议，不耦合任何 legacy 链路。
- 默认档 = qwen（settings.qwen_*）；当 qwen 不可用时自动降级到 deepseek。
- 所有请求带超时；异常统一包装为 LLMError，便于上层优雅降级。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from openai import AsyncOpenAI

from app.config import settings
from app.security_circuit import CircuitOpenError, get_breaker

logger = logging.getLogger("app.llm")


class LLMError(RuntimeError):
    """模型调用失败的统一异常，便于上层降级。"""


@dataclass(frozen=True)
class _Provider:
    name: str
    api_key: str
    base_url: str
    model: str


def _resolve_providers() -> List[_Provider]:
    """按优先级解析可用模型供应商。"""
    providers: List[_Provider] = []
    if settings.qwen_api_key:
        providers.append(
            _Provider(
                name="qwen",
                api_key=settings.qwen_api_key,
                base_url=str(settings.qwen_base_url),
                model=settings.qwen_model,
            )
        )
    if settings.deepseek_api_key:
        providers.append(
            _Provider(
                name="deepseek",
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_model,
            )
        )
    return providers


class LLMClient:
    """OpenAI-compatible 异步客户端，按供应商优先级故障转移。"""

    def __init__(self) -> None:
        self._providers = _resolve_providers()
        self._clients: dict[str, AsyncOpenAI] = {}
        if not self._providers:
            logger.warning("LLMClient: 未检测到任何可用模型 API Key，chat 将不可用")

    @property
    def available(self) -> bool:
        return bool(self._providers)

    def _client_for(self, provider: _Provider) -> AsyncOpenAI:
        cached = self._clients.get(provider.name)
        if cached is None:
            cached = AsyncOpenAI(api_key=provider.api_key, base_url=provider.base_url)
            self._clients[provider.name] = cached
        return cached

    async def chat(
        self,
        messages: Sequence[dict],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = 1024,
        timeout: float = 30.0,
    ) -> str:
        if not self._providers:
            raise LLMError("未配置任何模型供应商（QWEN_API_KEY / DEEPSEEK_API_KEY 均缺失）")

        last_err: Optional[Exception] = None
        for provider in self._providers:
            breaker = get_breaker(provider.name)
            if not breaker.allow():
                logger.warning("LLM 调用 %s 被熔断跳过", provider.name)
                continue
            try:
                client = self._client_for(provider)
                resp = await client.chat.completions.create(
                    model=provider.model,
                    messages=list(messages),  # type: ignore[arg-type]  # 调用方传 ChatCompletionMessageParam 兼容的 dict
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                content = resp.choices[0].message.content if resp.choices else None
                if not content:
                    raise LLMError(f"{provider.name} 返回空内容")
                breaker.record_success()
                return content.strip()
            except Exception as exc:  # 故障转移到下一个供应商
                breaker.record_failure()
                last_err = exc
                logger.warning("LLM 调用 %s 失败: %s", provider.name, exc)
                continue
        # 全部 provider 电路 open → 结构化错误，绝不静默切换平台付费 Key(§14.3)。
        if get_breaker(self._providers[0].name).state.value == "open" and all(
            not get_breaker(p.name).allow() for p in self._providers
        ):
            raise CircuitOpenError(self._providers[0].name)
        raise LLMError(f"所有模型供应商均不可用: {last_err}")

    async def health(self) -> bool:
        """轻量自检：尝试一次极简对话。"""
        try:
            await self.chat([{"role": "user", "content": "ping"}], max_tokens=8, timeout=10.0)
            return True
        except Exception:
            return False


_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


async def chat_completion(
    messages: Sequence[dict],
    *,
    temperature: float = 0.7,
    max_tokens: Optional[int] = 1024,
    timeout: float = 30.0,
) -> str:
    """便捷函数：经默认客户端发起一次对话补全。"""
    return await get_llm_client().chat(
        messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout
    )
