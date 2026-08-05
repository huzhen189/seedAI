"""新链路 LLM 客户端实现。

设计约束（对照全链路重构规范）：
- 仅依赖 OpenAI-compatible 协议，不耦合任何 legacy 链路。
- 默认档 = qwen（settings.qwen_*）；当 qwen 不可用时自动降级到 deepseek。
- 所有请求带超时；异常统一包装为 LLMError，便于上层优雅降级。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from openai import AsyncOpenAI

from app.config import settings
from app.security_circuit import CircuitOpenError, get_breaker
from app.analytics import record_ai_llm, record_model_detail

logger = logging.getLogger("app.llm")

# 可被前端选择器枚举的模型元数据（仅展示用；可用性由 has_provider 决定）。
_MODEL_META: dict[str, tuple[str, str]] = {
    "qwen": ("通义千问", "阿里通义千问，综合能力均衡，默认模型"),
    "deepseek": ("DeepSeek", "深度求索，推理能力强，作为默认兜底"),
    "hy3": ("混元 Hunyuan", "腾讯混元大模型，代码生成能力强"),
}


def _fmt_messages(messages: Sequence[dict]) -> str:
    """把拼给 LLM 的 messages 逐角色全量展开，便于复盘「到底喂了哪些对象」。"""
    parts: list[str] = []
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, (list, dict)):
            content = str(content)
        parts.append(f"  [{i}] ({role}) {content}")
    return "\n".join(parts)


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
        purpose: str | None = None,
    ) -> str:
        if not self._providers:
            raise LLMError("未配置任何模型供应商（QWEN_API_KEY / DEEPSEEK_API_KEY 均缺失）")

        last_err: Optional[Exception] = None
        for provider in self._providers:
            breaker = get_breaker(provider.name)
            if not breaker.allow():
                logger.warning("LLM 调用 %s 被熔断跳过", provider.name)
                continue
            t0 = time.time()
            try:
                client = self._client_for(provider)
                logger.info(
                    "[LLM→] provider=%s model=%s temp=%s max_tokens=%s\n[LLM prompt]\n%s",
                    provider.name, provider.model, temperature, max_tokens, _fmt_messages(messages),
                )
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
                logger.info(
                    "[LLM←] provider=%s model=%s 返回 %d 字符\n[LLM response]\n%s",
                    provider.name, provider.model, len(content), content,
                )
                elapsed = (time.time() - t0) * 1000
                # 统计: LLM 调用成功(per-model) → ai:llm + an:model
                tok_in = getattr(resp, "usage", None)
                tok_in = getattr(tok_in, "prompt_tokens", 0) if tok_in else 0
                tok_out = getattr(getattr(resp, "usage", None), "completion_tokens", 0) if getattr(resp, "usage", None) else 0
                await record_ai_llm(model=provider.name, ok=True, duration_ms=elapsed,
                                    tokens_in=int(tok_in or 0), tokens_out=int(tok_out or 0),
                                    purpose=purpose)
                await record_model_detail(provider.name, success=True)
                return content.strip()
            except Exception as exc:  # 故障转移到下一个供应商
                breaker.record_failure()
                last_err = exc
                elapsed = (time.time() - t0) * 1000
                err_type = type(exc).__name__
                # 统计: LLM 调用失败(per-model) → ai:llm + an:model
                await record_ai_llm(model=provider.name, ok=False, duration_ms=elapsed, error_type=err_type, purpose=purpose)
                await record_model_detail(provider.name, success=False)
                logger.warning("LLM 调用 %s 失败: %s", provider.name, exc)
                continue
        # 全部 provider 电路 open → 结构化错误，绝不静默切换平台付费 Key(§14.3)。
        if get_breaker(self._providers[0].name).state.value == "open" and all(
            not get_breaker(p.name).allow() for p in self._providers
        ):
            raise CircuitOpenError(self._providers[0].name)
        raise LLMError(f"所有模型供应商均不可用: {last_err}")

    async def _stream_one(
        self,
        provider: "_Provider",
        messages: Sequence[dict],
        *,
        temperature: float,
        max_tokens: Optional[int],
        timeout: float,
        enable_thinking: bool,
        purpose: str | None,
    ):
        """对单个供应商做一次流式补全，逐块产出 ``{"kind": "think"|"token", "text": str}``。

        失败（含熔断）抛 ``LLMError``，由调用方决定故障转移或回落默认链。
        解析 ``delta.reasoning_content`` → think，``delta.content`` → token；
        流正常结束时按最终 usage 记一次统计（与 chat() 同一落点）。
        """
        breaker = get_breaker(provider.name)
        if not breaker.allow():
            raise LLMError(f"模型供应商 {provider.name} 已熔断，暂时拒绝调用")
        t0 = time.time()
        try:
            client = self._client_for(provider)
            logger.info(
                "[LLM→] provider=%s model=%s temp=%s max_tokens=%s stream=True\n[LLM prompt]\n%s",
                provider.name, provider.model, temperature, max_tokens, _fmt_messages(messages),
            )
            stream = await client.chat.completions.create(
                model=provider.model,
                messages=list(messages),  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                stream=True,
                extra_body={"enable_thinking": enable_thinking},
            )
            usage = None
            full_think = ""
            full_token = ""
            async for chunk in stream:
                # 某些兼容端点把 usage 放在空 choices 的 chunk 上
                u = getattr(chunk, "usage", None)
                if u:
                    usage = u
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                rc = getattr(delta, "reasoning_content", None)
                ct = getattr(delta, "content", None)
                if rc:
                    full_think += rc
                    yield {"kind": "think", "text": rc}
                if ct:
                    full_token += ct
                    yield {"kind": "token", "text": ct}
            elapsed = (time.time() - t0) * 1000
            tok_in = getattr(usage, "prompt_tokens", 0) if usage else 0
            tok_out = getattr(usage, "completion_tokens", 0) if usage else 0
            breaker.record_success()
            await record_ai_llm(
                model=provider.name, ok=True, duration_ms=elapsed,
                tokens_in=int(tok_in or 0), tokens_out=int(tok_out or 0),
                purpose=purpose,
            )
            await record_model_detail(provider.name, success=True)
            logger.info(
                "[LLM←] provider=%s model=%s 流式结束(think=%d字符 token=%d字符)\n[LLM response.think]\n%s\n[LLM response.token]\n%s",
                provider.name, provider.model, len(full_think), len(full_token),
                full_think or "(无思考过程)", full_token or "(无正文)",
            )
            return
        except Exception as exc:  # 故障转移 / 回落由调用方处理
            breaker.record_failure()
            elapsed = (time.time() - t0) * 1000
            err_type = type(exc).__name__
            await record_ai_llm(model=provider.name, ok=False, duration_ms=elapsed, error_type=err_type, purpose=purpose)
            await record_model_detail(provider.name, success=False)
            logger.warning("LLM 流式调用 %s 失败: %s", provider.name, exc)
            raise LLMError(f"模型供应商 {provider.name} 调用失败: {exc}")

    async def chat_stream(
        self,
        messages: Sequence[dict],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = 1024,
        timeout: float = 30.0,
        enable_thinking: bool = True,
        purpose: str | None = None,
    ):
        """流式对话补全，逐块产出 ``{"kind": "think"|"token", "text": str}``。

        沿供应商优先级故障转移：首个可用 provider 起流；流中异常则该 provider 记失败并升级下一个。
        """
        if not self._providers:
            raise LLMError("未配置任何模型供应商（QWEN_API_KEY / DEEPSEEK_API_KEY 均缺失）")

        last_err: Optional[Exception] = None
        for provider in self._providers:
            try:
                async for chunk in self._stream_one(
                    provider, messages,
                    temperature=temperature, max_tokens=max_tokens, timeout=timeout,
                    enable_thinking=enable_thinking, purpose=purpose,
                ):
                    yield chunk
                return
            except LLMError as exc:
                last_err = exc
                continue
        if all(not get_breaker(p.name).allow() for p in self._providers):
            raise CircuitOpenError(self._providers[0].name)
        raise LLMError(f"所有模型供应商均不可用: {last_err}")

    async def chat_stream_with(
        self,
        model_id: str | None,
        messages: Sequence[dict],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = 1024,
        timeout: float = 30.0,
        enable_thinking: bool = True,
        purpose: str | None = None,
    ):
        """按前端选择器指定的模型流式补全；未指定或不可用则回落默认故障转移链。

        用于「全跟 selector 走」：聊天回复与建站代码生成统一由用户选择的模型执行。
        若所选模型已配置但调用失败，会自动回落默认链（fail-soft，绝不中断对话）。
        """
        if model_id:
            cfg = self._config_for(model_id)
            if cfg is not None:
                try:
                    async for chunk in self._stream_one(
                        cfg, messages,
                        temperature=temperature, max_tokens=max_tokens, timeout=timeout,
                        enable_thinking=enable_thinking, purpose=purpose,
                    ):
                        yield chunk
                    return
                except LLMError as exc:
                    logger.warning("定向模型 %s 失败，回落默认链: %s", model_id, exc)
        async for chunk in self.chat_stream(
            messages,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            enable_thinking=enable_thinking, purpose=purpose,
        ):
            yield chunk

    def list_providers(self) -> list[dict[str, str]]:
        """返回已配置可用的模型列表（供前端选择器枚举）。"""
        result: list[dict[str, str]] = []
        for pid, (label, desc) in _MODEL_META.items():
            if self.has_provider(pid):
                result.append({"id": pid, "label": label, "desc": desc})
        return result

    async def health(self) -> bool:
        """轻量自检：尝试一次极简对话。"""
        try:
            await self.chat([{"role": "user", "content": "ping"}], max_tokens=8, timeout=10.0, purpose="health")
            return True
        except Exception:
            return False


    # ---------------------------------------------------------- 定向供应商(建站代码生成用 hy3)
    def _config_for(self, name: str) -> "_Provider | None":
        """解析指定供应商的配置；未配置返回 None。

        与 ``_resolve_providers``（默认 qwen→deepseek 故障转移链）分离，
        用于「明确指定某个 provider」的场景（如建站代码生成必须用 hy3）。
        """
        if name == "qwen" and settings.qwen_api_key:
            return _Provider("qwen", settings.qwen_api_key, str(settings.qwen_base_url), settings.qwen_model)
        if name == "deepseek" and settings.deepseek_api_key:
            return _Provider("deepseek", settings.deepseek_api_key, str(settings.deepseek_base_url), settings.deepseek_model)
        if name == "hy3" and (settings.hy3_api_key or settings.hy3_api_key_demo):
            return _Provider("hy3", settings.hy3_api_key or settings.hy3_api_key_demo,
                             str(settings.hy3_base_url), settings.hy3_model)
        return None

    def has_provider(self, name: str) -> bool:
        """指定供应商是否已配置（供调用方在不发请求的前提下判断可用性）。"""
        return self._config_for(name) is not None

    async def complete_with(
        self,
        provider_name: str,
        messages: Sequence[dict],
        *,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        timeout: float = 30.0,
        purpose: str | None = None,
    ) -> str:
        """定向调用指定供应商（不经过 qwen→deepseek 故障转移链）。

        用于「必须用某个 provider」的场景（例如建站代码生成固定走 hy3）。
        仍受该 provider 的熔断器约束；失败抛 ``LLMError`` 由上层降级。
        """
        prov = self._config_for(provider_name)
        if prov is None:
            raise LLMError(f"未配置模型供应商 {provider_name}")
        breaker = get_breaker(prov.name)
        if not breaker.allow():
            raise LLMError(f"模型供应商 {provider_name} 已熔断，暂时拒绝调用")
        t0 = time.time()
        try:
            client = self._client_for(prov)
            logger.info(
                "[LLM→](定向) provider=%s model=%s temp=%s max_tokens=%s\n[LLM prompt]\n%s",
                prov.name, prov.model, temperature, max_tokens, _fmt_messages(messages),
            )
            resp = await client.chat.completions.create(
                model=prov.model,
                messages=list(messages),
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            content = resp.choices[0].message.content if resp.choices else None
            if not content:
                raise LLMError(f"{prov.name} 返回空内容")
            breaker.record_success()
            elapsed = (time.time() - t0) * 1000
            tok_in = getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or 0
            tok_out = getattr(getattr(resp, "usage", None), "completion_tokens", 0) or 0
            await record_ai_llm(model=prov.name, ok=True, duration_ms=elapsed,
                                tokens_in=int(tok_in or 0), tokens_out=int(tok_out or 0), purpose=purpose)
            await record_model_detail(prov.name, success=True)
            return content.strip()
        except Exception as exc:
            breaker.record_failure()
            elapsed = (time.time() - t0) * 1000
            await record_ai_llm(model=prov.name, ok=False, duration_ms=elapsed, error_type=type(exc).__name__, purpose=purpose)
            await record_model_detail(prov.name, success=False)
            logger.warning("LLM(定向) 调用 %s 失败: %s", prov.name, exc)
            raise LLMError(f"模型供应商 {provider_name} 调用失败: {exc}")

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
    purpose: str | None = None,
) -> str:
    """便捷函数：经默认客户端发起一次对话补全。

    purpose: 调用语义分类(intent/reply/extract/health), 透传给 record_ai_llm 做类型聚合。
    """
    return await get_llm_client().chat(
        messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout, purpose=purpose
    )


async def chat_completion_stream(
    messages: Sequence[dict],
    *,
    temperature: float = 0.7,
    max_tokens: Optional[int] = 1024,
    timeout: float = 30.0,
    enable_thinking: bool = True,
    purpose: str | None = None,
):
    """便捷函数：经默认客户端发起流式对话补全，逐块产出。

    产出元素为 dict:
      - {"kind": "think", "text": str}   思考过程增量(reasoning_content)
      - {"kind": "token", "text": str}   回复正文增量(content)
    仅在流正常结束时统计一次(成功/失败 + 时长 + token 数)，与 chat() 同一套 analytics 落点。
    """
    async for chunk in get_llm_client().chat_stream(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        enable_thinking=enable_thinking,
        purpose=purpose,
    ):
        yield chunk
