"""M9a 签名预览令牌单测(SEC-PREVIEW-001 的纯逻辑部分)。

覆盖: 正常签发/校验、签名篡改、载荷篡改、过期、格式非法、路径穿越防护、安全响应头。
不依赖数据库与 HTTP 栈, 保证在门禁里秒级跑完。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.preview import (
    _preview_headers,
    _resolve_within,
    sign_preview_token,
    verify_preview_token,
)


def _payload(**over: object) -> dict:
    base = {"p": 1, "a": 2, "v": 3, "u": 4, "g": 0, "e": int(time.time()) + 600}
    base.update(over)
    return base


def test_sign_then_verify_roundtrip() -> None:
    token = sign_preview_token(_payload())
    got = verify_preview_token(token)
    assert got["p"] == 1
    assert got["a"] == 2
    assert got["g"] == 0


def test_tampered_signature_rejected() -> None:
    token = sign_preview_token(_payload())
    body, sig = token.split(".")
    forged = f"{body}.{'A' * len(sig)}"
    with pytest.raises(HTTPException) as exc:
        verify_preview_token(forged)
    assert exc.value.status_code == 403


def test_tampered_payload_rejected() -> None:
    """改 payload 换项目 id, 签名必然不匹配 —— 防越权读别人项目。"""
    token = sign_preview_token(_payload(p=1))
    other = sign_preview_token(_payload(p=999))
    forged = f"{other.split('.')[0]}.{token.split('.')[1]}"
    with pytest.raises(HTTPException) as exc:
        verify_preview_token(forged)
    assert exc.value.status_code == 403


def test_expired_token_returns_410() -> None:
    """过期语义必须区别于伪造: 410 告诉前端「去重新签发」而非「你无权」。"""
    token = sign_preview_token(_payload(e=int(time.time()) - 1))
    with pytest.raises(HTTPException) as exc:
        verify_preview_token(token)
    assert exc.value.status_code == 410
    assert exc.value.detail["code"] == "PREVIEW_TOKEN_EXPIRED"


@pytest.mark.parametrize("bad", ["", ".", "abc", "a.b.c", "....", "onlybody"])
def test_malformed_token_rejected(bad: str) -> None:
    with pytest.raises(HTTPException) as exc:
        verify_preview_token(bad)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "evil",
    [
        "../../../etc/passwd",
        "..",
        "a/../../b",
        "sub/../../../outside.html",
        "\\..\\..\\win.ini",
    ],
)
def test_path_traversal_blocked(tmp_path: Path, evil: str) -> None:
    base = (tmp_path / "site").resolve()
    base.mkdir()
    with pytest.raises(HTTPException) as exc:
        _resolve_within(base, evil)
    assert exc.value.status_code == 403


def test_resolve_within_allows_nested(tmp_path: Path) -> None:
    base = (tmp_path / "site").resolve()
    (base / "assets").mkdir(parents=True)
    got = _resolve_within(base, "assets/app.css")
    assert got == base / "assets" / "app.css"
    # 空路径默认落到入口文件
    assert _resolve_within(base, "") == base / "index.html"


def test_security_headers_contract() -> None:
    headers = _preview_headers()
    csp = headers["Content-Security-Policy"]
    assert "base-uri 'none'" in csp
    assert "object-src 'none'" in csp
    assert "form-action 'none'" in csp
    assert "connect-src 'none'" in csp
    assert "frame-ancestors" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "no-store" in headers["Cache-Control"]
