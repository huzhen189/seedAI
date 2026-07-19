"""模型抽象层:Provider 注册表 + LangChain ChatOpenAI 工厂 + FallbackRouter(2-C)。

所有模型均走 OpenAI 兼容协议(DeepSeek / Qwen / HY3 都是),
因此统一用 ChatOpenAI 配 base_url 即可,新增模型只加一行注册。

FallbackRouter:按「用户主模型 + 默认降级序」在调用失败时回退到下一个可用模型,
并可在事件流中标注 degraded(由调用方决定)。默认降级序 HY3 → Qwen → DeepSeek(§12 #31)。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import List

from langchain_openai import ChatOpenAI

from .config import settings


# 默认降级序(用户指定可覆盖,见 §13.2 / #31)
FALLBACK_ORDER: List[str] = ["hy3", "qwen", "deepseek"]


class ModelUnavailableError(Exception):
    """模型不可用(限流/鉴权/超时),携带可选降级列表供前端确认切换。"""

    def __init__(self, failed: str, message: str, suggested: list[str]):
        self.failed = failed
        self.suggested = suggested
        super().__init__(message)


# 模型元数据(版本 / 速度 / 特性, 供前端显示)
PROVIDER_META: dict[str, dict] = {
    "hy3": {
        "version": "HY3-Turbo",
        "speed": "快 (~50t/s)",
        "desc": "腾讯混元3，综合能力强，建站和长文档首选",
    },
    "qwen": {
        "version": "Qwen-Plus",
        "speed": "中 (~30t/s)",
        "desc": "通义千问增强版，准确率高，规划和评审出色",
    },
    "deepseek": {
        "version": "DeepSeek-V3",
        "speed": "较快 (~40t/s)",
        "desc": "DeepSeek 旗舰版，中文理解好，编码和翻译强",
    },
}


class ProviderConfig:
    def __init__(self, id: str, label: str, base_url: str, api_key: str, model: str):
        self.id = id
        self.label = label
        self.base_url = base_url
        self.api_key = api_key
        self.model = model


# 注册表:前端 GET /models 拿到列表;生成时按 model_id 取
PROVIDERS: dict[str, ProviderConfig] = {
    "deepseek": ProviderConfig(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key=settings.deepseek_api_key,
        model="deepseek-chat",
    ),
    "qwen": ProviderConfig(
        id="qwen",
        label="Qwen",
        base_url=settings.qwen_base_url,
        api_key=settings.qwen_api_key,
        model=settings.qwen_model,
    ),
    "hy3": ProviderConfig(
        id="hy3",
        label="HY3",
        base_url=settings.hy3_base_url,
        api_key=settings.hy3_api_key or settings.hy3_api_key_demo,
        model=settings.hy3_model,
    ),
}


def list_providers() -> list[dict]:
    return [
        {
            "id": p.id,
            "label": p.label,
            "version": (PROVIDER_META.get(p.id, {}).get("version", "")),
            "speed": (PROVIDER_META.get(p.id, {}).get("speed", "")),
            "desc": (PROVIDER_META.get(p.id, {}).get("desc", "")),
        }
        for p in PROVIDERS.values()
    ]


def available_model_ids() -> List[str]:
    """有真实 API Key 的模型列表(用于降级回退时跳过未配置模型)。"""
    return [pid for pid, p in PROVIDERS.items() if p.api_key]


def resolve_fallback_order(primary: str) -> List[str]:
    """返回实际尝试顺序:[primary(若有 key)] + 其余可用模型(按 FALLBACK_ORDER)。"""
    order: List[str] = []
    if primary in PROVIDERS and PROVIDERS[primary].api_key:
        order.append(primary)
    for m in FALLBACK_ORDER:
        if m not in order and PROVIDERS[m].api_key:
            order.append(m)
    if not order:  # 都没配 key,至少尝试 primary 让错误暴露
        order = [primary] if primary in PROVIDERS else [FALLBACK_ORDER[0]]
    return order


def get_chat_model(model_id: str, streaming: bool = True) -> ChatOpenAI:
    """按 model_id 构造一个可流式/非流式调用的 ChatOpenAI。"""
    p = PROVIDERS[model_id]
    return ChatOpenAI(
        model=p.model,
        api_key=p.api_key,
        base_url=p.base_url,
        streaming=streaming,
        temperature=0.7,
        max_tokens=4096,
    )


async def astream_with_fallback(
    primary: str, messages: list, system: str | None = None
) -> AsyncGenerator:
    """流式生成,仅使用主模型;失败时抛 ModelUnavailableError(含可选替代),不自动切换。

    前端收到 retry 事件后弹框让用户选择替代模型,确认后重新发起请求——以此替代自动降级。
    """
    try:
        chat = get_chat_model(primary, streaming=True)
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        async for chunk in chat.astream(msgs):
            yield chunk, primary
    except Exception as e:
        order = resolve_fallback_order(primary)
        suggested = [m for m in order if m != primary and m in PROVIDERS and PROVIDERS[m].api_key]
        raise ModelUnavailableError(
            failed=primary,
            message=f"模型 {primary} 不可用: {e}",
            suggested=suggested,
        ) from e
