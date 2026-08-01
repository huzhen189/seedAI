"""BYOK 加密与 allowlist 校验（§14.3 BYOK）。

设计约束：
- provider/model/base_url 必须来自 allowlist，禁止任意 base_url 导致 SSRF 或凭证外送。
- AES-256-GCM：12B 随机 IV、16B tag，AAD 绑定 user+key+provider+kek_version。
- 为免迁移，iv/tag/kek_version 打包进现有 encrypted_key(LongText) 字段：
  token = "v{kek_version}.{b64(iv)}.{b64(tag)}.{b64(ciphertext)}"。
- 支持双 KEK 版本在线重加密：解密先试当前 KEK，失败试上一版本；rotate 用当前 KEK 重加密。
- fingerprint = sha256(明文)[:16] hex，不暴露明文 Key。
- BYOK 失效/限流由调用方返回结构化错误，绝不静默切平台付费 Key（§14.3）。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger("app.security.byok")


# ---- allowlist（标准做法：已知供应商 + HTTPS + 防 SSRF/私有地址）----
ALLOWED_PROVIDERS: frozenset[str] = frozenset(
    {"qwen", "hy3", "deepseek", "openai", "azure", "anthropic", "moonshot", "google"}
)
# 各 provider 默认官方 host 后缀；base_url 命中其一即通过 host 校验。
PROVIDER_HOST_SUFFIXES: dict[str, tuple[str, ...]] = {
    "qwen": (".aliyuncs.com", ".aliyun.com", ".dashscope.aliyuncs.com"),
    "hy3": (".tencentmaas.com", ".tencentcloudapi.com"),
    "deepseek": ("api.deepseek.com", "api.deepseek.com/v1"),
    "openai": ("api.openai.com",),
    "azure": (".openai.azure.com",),
    "anthropic": ("api.anthropic.com",),
    "moonshot": ("api.moonshot.cn",),
    "google": ("generativelanguage.googleapis.com", "ai.googleapis.com"),
}


class ByokValidationError(ValueError):
    """BYOK provider/model/base_url 校验失败。"""


def _is_private_host(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        return False  # 域名，交给后缀校验


def validate_provider_model_base_url(provider: str, model: str, base_url: str) -> None:
    """校验 provider/model/base_url 符合 allowlist 与 SSRF 防护。"""
    if provider not in ALLOWED_PROVIDERS:
        raise ByokValidationError(
            f"provider {provider!r} 不在允许列表 {sorted(ALLOWED_PROVIDERS)}"
        )
    if not model or not model.strip():
        raise ByokValidationError("model 不得为空")
    if len(model) > 128:
        raise ByokValidationError("model 过长")
    if not base_url or not base_url.strip():
        raise ByokValidationError("base_url 不得为空")
    parsed = urlparse(base_url.strip())
    if parsed.scheme != "https":
        raise ByokValidationError("base_url 必须为 https（禁止明文外送凭证）")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ByokValidationError("base_url 缺少 host")
    if _is_private_host(host):
        raise ByokValidationError(f"base_url host {host!r} 为私有/回环地址，禁止（SSRF 防护）")
    suffixes = PROVIDER_HOST_SUFFIXES.get(provider, ())
    if not any(host == s or host.endswith(s) for s in suffixes):
        raise ByokValidationError(
            f"base_url host {host!r} 不匹配 provider {provider} 的允许域名 {suffixes}；"
            "如确需自定义网关，请将域名加入 PROVIDER_HOST_SUFFIXES 白名单"
        )


# ---- AES-256-GCM 加密（双 KEK 版本）----
@dataclass
class ByokCrypto:
    kek: bytes                      # 当前 KEK（32 字节）
    prev_kek: bytes | None = None   # 上一版本 KEK（双版本解密用）
    kek_version: int = 1

    def _aad(self, user_id: int, key_id: int, provider: str, version: int) -> bytes:
        return f"user:{user_id}:key:{key_id}:provider:{provider}:kek:{version}".encode("utf-8")

    def encrypt(self, user_id: int, key_id: int, provider: str, plaintext: str) -> str:
        from Crypto.Cipher import AES
        from Crypto.Random import get_random_bytes

        iv = get_random_bytes(12)
        cipher = AES.new(self.kek, AES.MODE_GCM, nonce=iv)
        cipher.update(self._aad(user_id, key_id, provider, self.kek_version))
        ct, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
        payload = f"v{self.kek_version}.{_b64(iv)}.{_b64(tag)}.{_b64(ct)}"
        return payload

    def decrypt(self, user_id: int, key_id: int, provider: str, token: str) -> str:
        from Crypto.Cipher import AES

        version, iv_b, tag_b, ct_b = token.split(".", 3)
        iv, tag, ct = _unb64(iv_b), _unb64(tag_b), _unb64(ct_b)
        ver = int(version.lstrip("v"))
        # 总是先试当前 KEK,失败再试上一版本 —— 不依赖 token 里的版本号做判断,
        # 否则「换了 KEK 但忘了递增版本号」会让全量旧密文瞬间不可解(生产事故)。
        keys = [self.kek]
        if self.prev_kek:
            keys.append(self.prev_kek)
        last_err: Exception | None = None
        for k in keys:
            try:
                cipher = AES.new(k, AES.MODE_GCM, nonce=iv)
                cipher.update(self._aad(user_id, key_id, provider, ver))
                pt = cipher.decrypt_and_verify(ct, tag)
                return pt.decode("utf-8")
            except Exception as exc:  # 尝试下一版本 KEK
                last_err = exc
        raise ValueError(f"BYOK 解密失败（kek v{ver}）: {last_err}")

    def re_encrypt(
        self, user_id: int, key_id: int, provider: str, token: str
    ) -> str | None:
        """若 token 非当前 KEK 版本，重加密为当前版本；否则返回 None（无需变更）。"""
        version = int(token.split(".", 1)[0].lstrip("v"))
        if version == self.kek_version:
            return None
        plaintext = self.decrypt(user_id, key_id, provider, token)
        return self.encrypt(user_id, key_id, provider, plaintext)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def fingerprint_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()[:16]


def _kek_bytes(hex_key: str) -> bytes:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", hex_key or ""):
        raise ByokValidationError("PROVIDER_ENCRYPTION_KEY 必须是 64 位十六进制 AES-256 密钥")
    return bytes.fromhex(hex_key)


def get_byok_crypto() -> ByokCrypto:
    """从 settings 构造 ByokCrypto；未配置密钥抛清晰错误。"""
    if not settings.provider_encryption_key:
        raise ByokValidationError("PROVIDER_ENCRYPTION_KEY 未配置，BYOK 不可用")
    kek = _kek_bytes(settings.provider_encryption_key)
    prev = _kek_bytes(settings.provider_encryption_key_prev) if getattr(
        settings, "provider_encryption_key_prev", ""
    ) else None
    version = int(getattr(settings, "provider_encryption_key_version", 1) or 1)
    if version < 1:
        raise ByokValidationError("PROVIDER_ENCRYPTION_KEY_VERSION 必须 >= 1")
    return ByokCrypto(kek=kek, prev_kek=prev, kek_version=version)


__all__ = [
    "ALLOWED_PROVIDERS",
    "ByokCrypto",
    "ByokValidationError",
    "fingerprint_key",
    "get_byok_crypto",
    "validate_provider_model_base_url",
]
