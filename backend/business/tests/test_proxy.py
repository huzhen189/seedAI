"""proxy 路由的轻量测试:覆盖鉴权 SSE 错误帧与消息解析。

刻意不依赖 MySQL/Redis/AI 服务,保证 CI 在最小环境下可跑绿。
"""
from starlette.requests import Request
from starlette.testclient import TestClient

import app.main as m
from app.proxy import _parse_messages


def _make_request(query: str) -> Request:
    scope = {"type": "http", "query_string": query.encode()}
    return Request(scope)


def test_chat_requires_auth_returns_sse_error():
    """未登录访问 /api/chat 应返回一条 SSE error 帧(AUTH_REQUIRED),
    而非 JSON 401(浏览器 EventSource 读不到状态码)。"""
    c = TestClient(m.app)
    r = c.get(
        "/api/chat",
        params={
            "messages": '[{"role":"user","content":"hi"}]',
            "conversation_id": 1,
        },
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    body = b"".join(r.iter_bytes())
    assert b"event: error" in body
    assert b"AUTH_REQUIRED" in body
    assert b"Missing authentication" in body


def test_chat_bad_cookie_also_returns_sse_error():
    """携带无效 Cookie 同样走 SSE auth error 分支。"""
    c = TestClient(m.app)
    r = c.get(
        "/api/chat",
        params={"messages": "[]", "conversation_id": 1},
        headers={"Cookie": "access_token=garbage"},
    )
    body = b"".join(r.iter_bytes())
    assert b"AUTH_REQUIRED" in body


def test_parse_messages_from_json_array():
    req = _make_request('messages=%5B%7B%22role%22%3A%22user%22%2C%22content%22%3A%22hi%22%7D%5D')
    msgs = _parse_messages(req)
    assert msgs == [{"role": "user", "content": "hi"}]


def test_parse_messages_falls_back_to_single_q():
    req = _make_request("q=hello")
    msgs = _parse_messages(req)
    assert msgs == [{"role": "user", "content": "hello"}]


def test_parse_messages_missing_raises_400():
    import pytest
    from fastapi import HTTPException

    req = _make_request("")
    with pytest.raises(HTTPException) as exc:
        _parse_messages(req)
    assert exc.value.status_code == 400
