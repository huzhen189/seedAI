"""BYOK 解析（规范 §14.3）。

用户仅存储 provider + 加密 key（fingerprint）；model/base_url 来自 provider allowlist
默认映射，不得由用户任意指定（防 SSRF / 凭证外送）。解析结果 ``ResolvedByok`` 只在
单次 Harness/Model 调用作用域内存在，不进入日志、Span、Prompt 持久化或响应。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import ErrorEnvelope
from app.db.repositories.user_model_keys import UserModelKeysRepo
from app.security_byok import (
    ByokValidationError,
    get_byok_crypto,
    validate_provider_model_base_url,
)

# provider → (默认 model, 默认 base_url)。规范 §14.3 要求默认映射写入 models.yaml，
# 不硬编码业务代码；此处为集成占位，生产应改为从 models.yaml / settings 读取。
PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "qwen": ("qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "deepseek": ("deepseek-chat", "https://api.deepseek.com/v1"),
    "openai": ("gpt-4o-mini", "https://api.openai.com/v1"),
    "anthropic": ("claude-3-5-sonnet", "https://api.anthropic.com/v1"),
    "moonshot": ("moonshot-v1-8k", "https://api.moonshot.cn/v1"),
    "google": ("gemini-1.5-flash", "https://generativelanguage.googleapis.com/v1beta"),
}


@dataclass
class ResolvedByok:
    provider: str
    model: str
    base_url: str
    api_key: str  # 明文，仅单次调用作用域
    key_id: int
    fingerprint: str


async def resolve_byok(user_id: int, provider: str, session: AsyncSession) -> ResolvedByok | None:
    """解析用户 BYOK 密钥；无活跃密钥返回 None（调用方回退平台 Key）。

    返回前对 provider/model/base_url 做 allowlist + SSRF 校验，并 AES-256-GCM 解密。
    """
    repo = UserModelKeysRepo()
    rec = await repo.by_user_and_provider(session, user_id, provider)
    if rec is None or getattr(rec, "status", "active") != "active":
        return None
    if provider not in PROVIDER_DEFAULTS:
        # 未知 provider 不静默放行：明确失败，由调用方决定是否回退平台 Key。
        raise ByokValidationError(f"provider {provider!r} 无默认 model/base_url 映射")
    model, base_url = PROVIDER_DEFAULTS[provider]
    try:
        validate_provider_model_base_url(provider, model, base_url)
    except ByokValidationError:
        # allowlist 配置本身出错不应泄露密钥；原样上抛供上层告警。
        raise
    crypto = get_byok_crypto()
    api_key = crypto.decrypt(user_id, rec.id, provider, rec.encrypted_key)
    return ResolvedByok(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        key_id=rec.id,
        fingerprint=rec.fingerprint,
    )


def byok_error_envelope(provider: str, reason: str) -> ErrorEnvelope:
    return ErrorEnvelope(
        code="byok_unavailable",
        category="byok",
        what=f"provider {provider!r} 的 BYOK 解析失败",
        why=reason,
        next="可回退平台 Key 或提示用户检查 BYOK 配置",
        retryable=False,
        retry_scope="none",
    )


__all__ = ["PROVIDER_DEFAULTS", "ResolvedByok", "byok_error_envelope", "resolve_byok"]
