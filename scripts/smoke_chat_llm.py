"""M8 闲聊路径实跑：验证 S6 兜底 `chat_service.respond` 接通真实 LLM。

链路：login -> auto-start(建项目+会话) -> POST /api/chat(闲聊) -> SSE 取 assistant 文本
断言：
  - 流式出现 assistant 事件且文本非空
  - 文本不是旧 echo stub（"我已理解你的问题："）
  - 文本是模型真实生成（长度合理、非固定模板）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:7101")
ACC = os.environ.get("SMOKE_ACC", "e2e20_seedai_test")
PWD = os.environ.get("SMOKE_PWD", "testpass123")

STUB_MARKER = "我已理解你的问题："


async def _login(c: httpx.AsyncClient) -> str:
    r = await c.post(f"{BASE}/auth/login", json={"account": ACC, "password": PWD})
    assert r.status_code == 200, f"login {r.status_code} {r.text[:200]}"
    return r.json()["access_token"]


async def _auto_start(c: httpx.AsyncClient, token: str) -> int:
    r = await c.post(
        f"{BASE}/api/auto-start",
        json={"text": "我想先聊聊建站思路"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, f"auto-start {r.status_code} {r.text[:200]}"
    return r.json()["conversation"]["id"]


async def _chat_stream(c: httpx.AsyncClient, token: str, conv_id: int, message: str) -> tuple[str, list[str]]:
    events: list[str] = []
    reply_text = ""
    payload = {
        "client_msg_id": f"smoke-chat-{os.urandom(6).hex()}",
        "conversation_id": conv_id,
        "message": message,
    }
    async with c.stream(
        "POST",
        f"{BASE}/api/chat",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    ) as resp:
        if resp.status_code != 200:
            body = await resp.aread()
            raise AssertionError(f"chat {resp.status_code} {body[:300]}")
        buf = ""
        async for line in resp.aiter_lines():
            if not line:
                etype = None
                data_line = None
                for bl in buf.split("\n"):
                    if bl.startswith("event: "):
                        etype = bl.removeprefix("event: ").strip()
                    elif bl.startswith("data: "):
                        data_line = bl.removeprefix("data: ")
                if etype:
                    events.append(etype)
                    if etype == "done" and data_line:
                        try:
                            data = json.loads(data_line)
                        except json.JSONDecodeError:
                            data = {}
                        reply = data.get("reply") or data.get("data", {}).get("reply") or ""
                        if isinstance(reply, str):
                            reply_text += reply
                        print("[debug] done reply:", reply[:300])
                buf = ""
            else:
                buf += line + "\n"
    return reply_text, events


async def main() -> int:
    async with httpx.AsyncClient() as c:
        token = await _login(c)
        conv_id = await _auto_start(c, token)
        text, events = await _chat_stream(
            c, token, conv_id, "你好，随便聊聊：你觉得好的网站设计，最关键的三个原则是什么？"
        )
        reply_text = text

    print("=== SSE events (types) ===")
    print(events)
    print("=== reply text ===")
    print(repr(reply_text))

    passed = 0
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"[PASS] {name}")
        else:
            failed += 1
            print(f"[FAIL] {name} :: {detail}")

    check("done 事件出现", "done" in events, f"events={events}")
    check("回复文本非空", bool(text.strip()), "空文本")
    check("非旧 echo stub", STUB_MARKER not in text, "仍命中占位 stub")
    check("模型真实生成(长度>=20)", len(text.strip()) >= 20, f"len={len(text.strip())}")

    print(f"\nRESULT: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
