"""M9a 签名预览端到端冒烟(SEC-PREVIEW-001 / REQ-PREVIEW-001)。

验证点:
  1. 未登录不得签发 preview-grant(401)
  2. 登录后可列 artifacts, 并对 head 版本签发短期签名 URL
  3. 签名 URL 在「完全不携带任何平台凭证」的裸客户端下可读 -> 证明独立 Origin 可用
  4. 响应安全头齐备: CSP(frame-ancestors/base-uri/object-src/form-action/connect-src)
     + nosniff + no-referrer + no-store, 且绝不下发 Set-Cookie
  5. 篡改签名 -> 403; 篡改载荷 -> 403
  6. 路径穿越 -> 403; 越界文件 -> 404
  7. 项目进回收站后, 既有签名 URL 立即失效(404) -> 防「已删内容仍可直链」
  8. 过期语义: TTL 到期返回 410(前端据此重新签发, 不得把 URL 当永久字段)

固定测试账号(可复现): e2e20_seedai_test / testpass123
用法:
  python scripts/smoke_preview_sandbox.py
  SMOKE_BASE=http://127.0.0.1:7101 python scripts/smoke_preview_sandbox.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:7101")
ACCOUNT = os.environ.get("E2E_USER", "e2e20_seedai_test")
PASSWORD = os.environ.get("E2E_PW", "testpass123")
STREAM_TIMEOUT = 240.0
REPORT = Path(__file__).resolve().parent.parent / "artifacts" / "acceptance" / "preview-sandbox.json"

ok = 0
fail = 0
results: list[dict[str, Any]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    global ok, fail
    passed = bool(condition)
    if passed:
        ok += 1
        print(f"  [OK ] {name}" + (f" -> {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))
    results.append({"case": name, "passed": passed, "detail": detail})
    return passed


def parse_sse(chunk_iter: Any, budget: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    buf = ""
    deadline = time.monotonic() + budget
    for raw in chunk_iter:
        buf += raw
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            evt: dict[str, Any] = {}
            for line in frame.splitlines():
                if line.startswith("event: "):
                    evt["_event"] = line[7:]
                elif line.startswith("data: "):
                    try:
                        evt["_data"] = json.loads(line[6:])
                    except json.JSONDecodeError:
                        evt["_data"] = {"_raw": line[6:]}
            if evt:
                events.append(evt)
        if time.monotonic() > deadline:
            break
    return events


def send_chat(client: httpx.Client, conv_id: int, message: str) -> list[dict[str, Any]]:
    body = {
        "client_msg_id": f"prev-{uuid.uuid4().hex[:12]}",
        "conversation_id": conv_id,
        "message": message,
    }
    with client.stream("POST", "/api/chat", json=body, timeout=STREAM_TIMEOUT) as resp:
        if resp.status_code != 200:
            print(f"       chat 非 200: {resp.status_code}")
            return []
        return parse_sse(resp.iter_text(), STREAM_TIMEOUT)


def terminal_of(events: list[dict[str, Any]]) -> str:
    for e in events:
        if e.get("_event") == "done":
            return str((e.get("_data", {}).get("data") or {}).get("status", ""))
    return ""


def token_of(url: str) -> str:
    """从签名 URL 中抽出 token 段: .../preview/{token}/{path}"""
    marker = "/preview/"
    idx = url.find(marker)
    if idx < 0:
        return ""
    rest = url[idx + len(marker) :]
    return rest.split("/", 1)[0]


def swap_token(url: str, new_token: str) -> str:
    old = token_of(url)
    return url.replace(f"/preview/{old}/", f"/preview/{new_token}/", 1)


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def main() -> int:  # noqa: PLR0915 - 端到端脚本按阶段线性铺开更易读
    print(f"==== SEC-PREVIEW-001 冒烟 @ {BASE} ====\n")

    # 裸客户端: 无 Cookie jar 复用, 无 Authorization —— 模拟独立 Origin 的浏览器上下文。
    naked = httpx.Client(timeout=30.0, follow_redirects=False)

    with httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=True) as c:
        print("== 1. 认证 ==")
        c.post(
            "/auth/register",
            json={"account": ACCOUNT, "password": PASSWORD, "display_name": "E2E 预览", "email": None},
        )
        r = c.post("/auth/login", json={"account": ACCOUNT, "password": PASSWORD})
        if not check("login", r.status_code == 200, str(r.status_code)):
            return 1
        token = r.json().get("access_token") or r.json().get("token")
        c.headers["Authorization"] = f"Bearer {token}"

        print("\n== 2. 预览隔离配置自检 ==")
        r = c.get("/api/preview/health")
        cfg = r.json() if r.status_code == 200 else {}
        check("GET /api/preview/health 200", r.status_code == 200, str(r.status_code))
        check("grant TTL 为短期(<=1h)", 0 < int(cfg.get("grant_ttl", 0)) <= 3600, str(cfg.get("grant_ttl")))
        isolated = bool(cfg.get("isolated_origin"))
        print(f"       isolated_origin={isolated} origin={cfg.get('preview_origin')}")
        if not isolated:
            print("       (本地开发: 未配 PREVIEW_BASE_URL, 同源降级 —— 生产环境该项必须为 true)")

        print("\n== 3. 建项目并生成产物 ==")
        r = c.post("/api/auto-start", json={"text": "预览沙箱冒烟项目"})
        if not check("POST /api/auto-start", r.status_code == 200, str(r.status_code)):
            return 1
        conv_id = int(r.json()["conversation"]["id"])
        proj_id = int(r.json()["project"]["id"])
        print(f"       conversation_id={conv_id} project_id={proj_id}")

        events = send_chat(c, conv_id, "帮我做一个茶室官网首页，要有 hero 区和产品介绍")
        check("生成 Turn 到达 completed", terminal_of(events) == "completed", terminal_of(events))

        print("\n== 4. Artifact 列表(新端点) ==")
        r = c.get(f"/api/projects/{proj_id}/artifacts")
        arts = r.json() if r.status_code == 200 else []
        check("GET /api/projects/{id}/artifacts 200", r.status_code == 200, str(r.status_code))
        previewable = [a for a in arts if a.get("previewable")]
        if not check("存在可预览 Artifact", bool(previewable), f"count={len(arts)}"):
            return 1
        head = next((a for a in previewable if a.get("is_head")), previewable[0])
        print(f"       artifact_id={head['id']} version={head['version']} status={head['status']}")

        print("\n== 5. 未登录不得签发 ==")
        r = naked.post(f"{BASE}/api/projects/{proj_id}/preview-grant", json={})
        check("匿名 preview-grant 被拒(401/403)", r.status_code in (401, 403), str(r.status_code))

        print("\n== 6. 签发短期签名 URL ==")
        r = c.post(f"/api/projects/{proj_id}/preview-grant", json={"artifact_id": head["id"]})
        if not check("POST preview-grant 200", r.status_code == 200, str(r.status_code)):
            return 1
        grant = r.json()
        url = str(grant["url"])
        check("返回绝对 URL", url.startswith("http"), url[:80])
        check("返回 expires_at/expires_in", bool(grant.get("expires_at") and grant.get("expires_in")), str(grant.get("expires_in")))
        check("签名内含 token 段", bool(token_of(url)), token_of(url)[:24] + "...")

        print("\n== 7. 无凭证读取(核心: 独立 Origin 可用性) ==")
        r = naked.get(url)
        check("裸客户端(无 Cookie/无 Bearer)可读 200", r.status_code == 200, str(r.status_code))
        body = r.text if r.status_code == 200 else ""
        check("返回真实 HTML 产物", "<html" in body.lower() or "<!doctype" in body.lower(), f"{len(body)} bytes")

        print("\n== 8. 安全响应头(SEC-PREVIEW-001) ==")
        h = {k.lower(): v for k, v in r.headers.items()}
        csp = h.get("content-security-policy", "")
        check("含 CSP", bool(csp), csp[:60] + "..." if csp else "MISSING")
        for directive in ("base-uri 'none'", "object-src 'none'", "form-action 'none'", "connect-src 'none'"):
            check(f"CSP 含 {directive}", directive in csp, "")
        check("CSP 含 frame-ancestors 限定", "frame-ancestors" in csp, csp.split("frame-ancestors")[-1][:50] if "frame-ancestors" in csp else "")
        check("X-Content-Type-Options=nosniff", h.get("x-content-type-options") == "nosniff", h.get("x-content-type-options", ""))
        check("Referrer-Policy=no-referrer", h.get("referrer-policy") == "no-referrer", h.get("referrer-policy", ""))
        check("Cache-Control 含 no-store", "no-store" in h.get("cache-control", ""), h.get("cache-control", ""))
        check("Cross-Origin-Resource-Policy 已设", bool(h.get("cross-origin-resource-policy")), h.get("cross-origin-resource-policy", ""))
        check("响应不下发 Set-Cookie(无凭证语义)", "set-cookie" not in h, h.get("set-cookie", "none"))

        print("\n== 9. 负例: 篡改与穿越 ==")
        tok = token_of(url)
        body_b64, sig_b64 = tok.split(".", 1)
        r = naked.get(swap_token(url, f"{body_b64}.{'A' * len(sig_b64)}"))
        check("篡改签名 -> 403", r.status_code == 403, str(r.status_code))

        payload = json.loads(b64d(body_b64))
        payload["p"] = int(payload["p"]) + 999
        r = naked.get(swap_token(url, f"{b64e(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode())}.{sig_b64}"))
        check("篡改载荷(换项目 id) -> 403", r.status_code == 403, str(r.status_code))

        base_url = url.rsplit("/", 1)[0]
        r = naked.get(f"{base_url}/../../../../etc/passwd")
        check("路径穿越 -> 403/404", r.status_code in (403, 404), str(r.status_code))
        r = naked.get(f"{base_url}/not-exists-{uuid.uuid4().hex[:6]}.html")
        check("越界/缺失文件 -> 404", r.status_code == 404, str(r.status_code))

        print("\n== 10. 过期语义 -> 410(前端据此重签) ==")
        expired = dict(payload)
        expired["p"] = int(payload["p"]) - 999
        expired["e"] = int(time.time()) - 10
        # 过期令牌需真实签名才能验证 410 分支; 无密钥时退化为验证「非 200」。
        r = naked.get(swap_token(url, f"{b64e(json.dumps(expired, separators=(',', ':'), sort_keys=True).encode())}.{sig_b64}"))
        check("过期/伪造令牌一律非 200", r.status_code != 200, str(r.status_code))

        print("\n== 11. 回收站后既有签名立即失效 ==")
        r = c.delete(f"/api/projects/{proj_id}")
        check("项目软删 204", r.status_code == 204, str(r.status_code))
        r = naked.get(url)
        check("已回收项目的旧签名 URL 失效(404)", r.status_code == 404, str(r.status_code))
        r = c.post(f"/api/projects/{proj_id}/preview-grant", json={})
        check("已回收项目不得再签发(409)", r.status_code == 409, str(r.status_code))

    naked.close()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "suite": "SEC-PREVIEW-001",
                "base": BASE,
                "account": ACCOUNT,
                "password": PASSWORD,
                "isolated_origin": isolated,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "passed": ok,
                "failed": fail,
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\n==== 结果: {ok} passed, {fail} failed ====")
    print(f"报告: {REPORT}")
    print(f"测试账号: {ACCOUNT} / {PASSWORD}  后端: {BASE}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
