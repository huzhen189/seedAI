"""无需额外依赖的 ULID 生成器。"""

from __future__ import annotations

import os
import threading
import time


_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LOCK = threading.Lock()
_LAST_MS = -1
_LAST_RANDOM = 0


def _encode_base32(value: int, length: int) -> str:
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _CROCKFORD32[value & 31]
        value >>= 5
    return "".join(chars)


def new_ulid() -> str:
    """返回 26 字符、按时间字典序递增的 ULID。"""
    global _LAST_MS, _LAST_RANDOM
    now_ms = time.time_ns() // 1_000_000
    with _LOCK:
        if now_ms == _LAST_MS:
            _LAST_RANDOM = (_LAST_RANDOM + 1) & ((1 << 80) - 1)
        else:
            _LAST_MS = now_ms
            _LAST_RANDOM = int.from_bytes(os.urandom(10), "big")
        timestamp = _LAST_MS
        randomness = _LAST_RANDOM
    return _encode_base32(timestamp, 10) + _encode_base32(randomness, 16)
