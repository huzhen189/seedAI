"""M10e BYOK: allowlist 校验 + AES-256-GCM 往返 + 双 KEK 重加密 + 信封打包。"""

from __future__ import annotations

import json

import pytest

from app.api.byok import _kek_version, _mask, _pack, _unpack
from app.security_byok import (
    ALLOWED_PROVIDERS,
    ByokCrypto,
    ByokValidationError,
    fingerprint_key,
    validate_provider_model_base_url,
)

KEK_A = bytes(range(32))
KEK_B = bytes(range(32, 64))


# ---------------------------------------------------------------- allowlist


def test_allowlist_accepts_official_hosts() -> None:
    validate_provider_model_base_url(
        "deepseek", "deepseek-chat", "https://api.deepseek.com/v1"
    )
    validate_provider_model_base_url(
        "qwen", "qwen-plus", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    validate_provider_model_base_url("openai", "gpt-4o", "https://api.openai.com/v1")


@pytest.mark.parametrize(
    ("provider", "model", "base_url", "hint"),
    [
        ("evilcorp", "m", "https://api.openai.com", "provider"),          # provider 不在白名单
        ("openai", "", "https://api.openai.com", "model"),                 # model 空
        ("openai", "gpt-4o", "http://api.openai.com", "https"),            # 明文 http
        ("openai", "gpt-4o", "https://127.0.0.1:8080/v1", "私有"),         # SSRF 回环
        ("openai", "gpt-4o", "https://192.168.1.10/v1", "私有"),           # SSRF 内网
        ("openai", "gpt-4o", "https://api.openai.com.evil.io/v1", "不匹配"),  # 后缀伪装
        ("deepseek", "x", "https://api.anthropic.com", "不匹配"),          # 跨 provider 域名
    ],
)
def test_allowlist_rejects(provider: str, model: str, base_url: str, hint: str) -> None:
    with pytest.raises(ByokValidationError) as err:
        validate_provider_model_base_url(provider, model, base_url)
    assert hint in str(err.value)


def test_allowlist_is_frozen_set() -> None:
    assert isinstance(ALLOWED_PROVIDERS, frozenset)
    assert "qwen" in ALLOWED_PROVIDERS and "hy3" in ALLOWED_PROVIDERS


# ---------------------------------------------------------------- 加解密


def test_encrypt_decrypt_roundtrip() -> None:
    crypto = ByokCrypto(kek=KEK_A, kek_version=1)
    token = crypto.encrypt(7, 42, "openai", "sk-secret-value")
    assert token.startswith("v1.")
    assert "sk-secret-value" not in token  # 密文不含明文
    assert crypto.decrypt(7, 42, "openai", token) == "sk-secret-value"


def test_decrypt_rejects_wrong_aad_binding() -> None:
    """AAD 绑定 user/key/provider —— 换个用户或 provider 必须解不开(防横向越权)。"""
    crypto = ByokCrypto(kek=KEK_A, kek_version=1)
    token = crypto.encrypt(7, 42, "openai", "sk-secret-value")
    for args in ((8, 42, "openai"), (7, 43, "openai"), (7, 42, "deepseek")):
        with pytest.raises(ValueError):
            crypto.decrypt(*args, token)


def test_dual_kek_decrypt_and_re_encrypt() -> None:
    """KEK 轮换: 旧密文用 prev_kek 解开,re_encrypt 升到当前版本。"""
    old = ByokCrypto(kek=KEK_A, kek_version=1)
    old_token = old.encrypt(7, 42, "openai", "sk-old")

    new = ByokCrypto(kek=KEK_B, prev_kek=KEK_A, kek_version=2)
    assert new.decrypt(7, 42, "openai", old_token) == "sk-old"

    rotated = new.re_encrypt(7, 42, "openai", old_token)
    assert rotated is not None and rotated.startswith("v2.")
    assert new.decrypt(7, 42, "openai", rotated) == "sk-old"
    # 已是当前版本 -> 无需变更
    assert new.re_encrypt(7, 42, "openai", rotated) is None


def test_prev_kek_fallback_ignores_version_number() -> None:
    """回归：换了 KEK 但忘了递增版本号时，旧密文仍必须能解开（否则是全量数据事故）。"""
    old = ByokCrypto(kek=KEK_A, kek_version=1)
    old_token = old.encrypt(7, 42, "openai", "sk-old")
    # 运维只换了 KEK，版本号还留在 1
    sloppy = ByokCrypto(kek=KEK_B, prev_kek=KEK_A, kek_version=1)
    assert sloppy.decrypt(7, 42, "openai", old_token) == "sk-old"


def test_re_encrypt_requires_version_bump() -> None:
    """版本号未递增时 re_encrypt 返回 None —— 这正是必须递增版本号的原因。"""
    old = ByokCrypto(kek=KEK_A, kek_version=1)
    token = old.encrypt(7, 42, "openai", "sk-old")
    sloppy = ByokCrypto(kek=KEK_B, prev_kek=KEK_A, kek_version=1)
    assert sloppy.re_encrypt(7, 42, "openai", token) is None
    correct = ByokCrypto(kek=KEK_B, prev_kek=KEK_A, kek_version=2)
    assert (correct.re_encrypt(7, 42, "openai", token) or "").startswith("v2.")


def test_fingerprint_stable_and_not_reversible() -> None:
    fp = fingerprint_key("sk-secret-value")
    assert fp == fingerprint_key("sk-secret-value")
    assert len(fp) == 16
    assert fp != fingerprint_key("sk-secret-value2")


# ---------------------------------------------------------------- 信封 / 掩码


def test_envelope_pack_unpack() -> None:
    env = _pack("sk-abcdefgh1234", "gpt-4o", "https://api.openai.com/v1")
    assert json.loads(env)["api_key"] == "sk-abcdefgh1234"
    out = _unpack(env)
    assert out == {
        "api_key": "sk-abcdefgh1234",
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
    }


def test_envelope_unpack_tolerates_legacy_plain_key() -> None:
    out = _unpack("sk-legacy-raw")
    assert out["api_key"] == "sk-legacy-raw"
    assert out["model"] == "" and out["base_url"] == ""


def test_mask_never_leaks_middle() -> None:
    assert _mask("sk-abcdefgh1234") == "sk-a********1234"
    assert _mask("short") == "*****"


def test_kek_version_parsing() -> None:
    assert _kek_version("v2.aaa.bbb.ccc") == 2
    assert _kek_version("pending") == 0
