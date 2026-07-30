"""模型抽象层:Provider 注册表 + LangChain ChatOpenAI 工厂 + FallbackRouter(2-C)。

所有模型均走 OpenAI 兼容协议(DeepSeek / Qwen / HY3 都是),
因此统一用 ChatOpenAI 配 base_url 即可,新增模型只加一行注册。

FallbackRouter:按「用户主模型 + 默认降级序」在调用失败时回退到下一个可用模型,
并可在事件流中标注 degraded(由调用方决定)。默认降级序 HY3 → Qwen → DeepSeek(§12 #31)。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import List

import httpx
from langchain_openai import ChatOpenAI

from .config import settings

# 日志命名规范: app.<module>(见项目约定)。旧名 "ai_service.provider" 不在 handler 覆盖范围内,
# 导致 provider 层日志被静默丢弃(2026-07-30 排查 hy3 401 时发现)。
logger = logging.getLogger("app.agent.providers")


# 默认降级序(用户指定可覆盖,见 §13.2 / #31)
FALLBACK_ORDER: List[str] = ["deepseek", "hy3", "qwen"]


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
        "version": "Qwen-3.7-Plus",
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
        model="deepseek-v4-flash",
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


def _key_fp(key: str) -> str:
    """key 指纹(前 6 + 后 4 + 长度),用于排查「服务器实际用了哪把 key」而不泄露密钥。"""
    if not key:
        return "<EMPTY>"
    return f"{key[:6]}…{key[-4:]}/{len(key)}"


def get_chat_model(model_id: str, streaming: bool = True) -> ChatOpenAI:
    """按 model_id 构造一个可流式/非流式调用的 ChatOpenAI。"""
    p = PROVIDERS[model_id]
    # key 指纹日志:线上出现 401 时可立刻判定是 key 缺失/过期还是打错端点(2026-07-30 hy3 401002 排查)
    logger.info("[provider] build model=%s base=%s key=%s", p.model, p.base_url, _key_fp(p.api_key))
    return ChatOpenAI(
        model=p.model,
        api_key=p.api_key,
        base_url=p.base_url,
        streaming=streaming,
        temperature=0.4,
        max_tokens=8192,
        # read 超时: 120s→900s→1800s。长链路流式生成(单页 3~8 万字符甚至更长)时,
        # deepseek 偶有数分钟静默(限流/批处理), 短 read 会误判掉线 → 触发降级到 qwen 并拖慢整轮。
        # 1800s 给足长生成余量(主模型 attempts=2 → 最坏 60min 才放弃, 但正常生成远小于此)。
        request_timeout=httpx.Timeout(connect=15.0, read=1800.0, write=15.0, pool=10.0),
        max_retries=2,
    )


async def astream_with_fallback(
    primary: str, messages: list, system: str | None = None
) -> AsyncGenerator:
    """流式生成,主模型优先;瞬时失败自动重连,主模型持续不可用时按 FALLBACK_ORDER 自动降级到下一可用模型。

    设计权衡(修复 deepseek 长链路频繁瞬时掉线导致整轮生成中断):
      - 主模型先重试 1 次(应对瞬时抖动);
      - 仍失败则遍历 FALLBACK_ORDER 中其余「已配 key」的模型,每个尝试 1 次;
      - 任一模型成功流完即返回,并在首个 chunk 标注实际使用的模型(degraded 信号由调用方决定);
      - 全部失败才抛 ModelUnavailableError(前端再弹切换框)。
    这样「建站/代码生成」这类长任务不再因单点模型抖动而前功尽弃。
    """
    order = resolve_fallback_order(primary)
    # 构造 (模型id, 是否主模型) 尝试序列:主模型优先,失败再按降级序
    plan: List[tuple[str, bool]] = [(primary, True)]
    for m in order:
        if m != primary:
            plan.append((m, False))

    last_err: Exception | None = None
    # 主模型: 允许 1 次瞬时重连
    first = True
    for mid, is_primary in plan:
        attempts = 2 if is_primary else 1
        for attempt in range(attempts):
            try:
                logger.info("LLM 调用 model=%s attempt=%d streaming=True", mid, attempt + 1)
                chat = get_chat_model(mid, streaming=True)
                msgs = ([{"role": "system", "content": system}] if system else []) + messages
                emitted = False
                async for chunk in chat.astream(msgs):
                    if not emitted:
                        if not is_primary:
                            logger.warning("LLM 降级生效: %s 不可用, 改用 %s", primary, mid)
                        emitted = True
                    yield chunk, mid
                return  # 正常流完
            except (GeneratorExit, ConnectionResetError, ConnectionError) as e:
                last_err = e
                if attempt < attempts - 1:
                    logger.warning("LLM 连接中断, 1s 后重试(%s/%s): %s", attempt + 1, attempts, e)
                    await asyncio.sleep(1)
                    continue
                break
            except Exception as e:
                last_err = e
                break
    # 全部模型均失败 → 抛 ModelUnavailableError(前端弹切换框)
    suggested = [m for m in order if m != primary and m in PROVIDERS and PROVIDERS[m].api_key]
    raise ModelUnavailableError(
        failed=primary,
        message=f"模型 {primary} 不可用(已尝试降级序 {order})",
        suggested=suggested,
    ) from last_err
