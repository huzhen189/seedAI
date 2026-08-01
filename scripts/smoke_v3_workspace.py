"""十阶段链路冒烟: 新库 seed_ai + workspace/search 端点。

固定测试账号(可复现, 便于登录前端复查): e2e20_seedai_test / testpass123
用法: python _smoke_v3.py
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:7101"
ACCOUNT = "e2e20_seedai_test"
PASSWORD = "testpass123"

ok = 0
fail = 0


def step(name: str, resp: httpx.Response, expect: tuple[int, ...] = (200,)) -> bool:
    global ok, fail
    good = resp.status_code in expect
    if good:
        ok += 1
        print(f"  [OK ] {name} -> {resp.status_code}")
    else:
        fail += 1
        print(f"  [FAIL] {name} -> {resp.status_code} {resp.text[:300]}")
    return good


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=True) as c:
        print("== 1. 认证 ==")
        r = c.post(
            "/auth/register",
            json={"account": ACCOUNT, "password": PASSWORD, "display_name": "E2E 冒烟", "email": None},
        )
        # 已存在则 409/400, 属正常
        print(f"  [--] register -> {r.status_code}")
        r = c.post("/auth/login", json={"account": ACCOUNT, "password": PASSWORD})
        if not step("login", r):
            return 1
        data = r.json()
        token = data.get("access_token") or data.get("token")
        if token:
            c.headers["Authorization"] = f"Bearer {token}"
        step("auth/me", c.get("/auth/me"))

        print("== 2. auto-start(自动建项目+会话) ==")
        r = c.post("/api/auto-start", json={"text": "做一个极简风格的咖啡店官网"})
        if not step("POST /api/auto-start", r):
            return 1
        payload = r.json()
        project = payload["project"]
        conversation = payload["conversation"]
        print(f"       project_id={project['id']} name={project['name']!r}")
        print(f"       conversation_id={conversation['id']} name={conversation['name']!r}")
        pid, cid = project["id"], conversation["id"]

        print("== 3. 项目 / 会话 CRUD ==")
        step("GET /api/projects", c.get("/api/projects"))
        step("PATCH /api/projects/{id}", c.patch(f"/api/projects/{pid}", json={"name": "咖啡店官网(改名)"}))
        step("GET /api/conversations?project_id=", c.get("/api/conversations", params={"project_id": pid}))
        step("GET /api/conversations/{id}", c.get(f"/api/conversations/{cid}"))
        step("GET /api/conversations/{id}/messages", c.get(f"/api/conversations/{cid}/messages"))
        r = c.post("/api/conversations", json={"project_id": pid, "name": "第二轮讨论"})
        step("POST /api/conversations", r)
        cid2 = r.json()["id"] if r.status_code == 200 else None

        print("== 4. 搜索(本轮新增) ==")
        r = c.get("/api/search", params={"q": "咖啡"})
        if step("GET /api/search", r):
            print(f"       hits={json.dumps(r.json(), ensure_ascii=False)[:220]}")
        r = c.get("/api/search", params={"q": "100%_不该匹配"})
        if step("GET /api/search (LIKE 元字符转义)", r):
            print(f"       escaped-query hits={len(r.json())} (预期 0)")
        r = c.get("/api/search/messages", params={"q": "咖啡"})
        if step("GET /api/search/messages", r):
            print(f"       message hits={len(r.json())}")

        print("== 5. 遥测吸入 ==")
        step("POST /admin/analytics/track", c.post("/admin/analytics/track", json={"event": "smoke", "props": {}}), (200, 204))
        step("POST /admin/analytics/perf", c.post("/admin/analytics/perf", json={"metric": "lcp", "value": 1.2}), (200, 204))

        print("== 6. Turn 快照端点存活性 ==")
        step("GET /api/gate/pending", c.get("/api/gate/pending"))

        print("== 7. 清理(会话硬删 / 项目软删) ==")
        if cid2:
            step("DELETE /api/conversations/{id}", c.delete(f"/api/conversations/{cid2}"), (204,))
        step("DELETE /api/projects/{id} (软删)", c.delete(f"/api/projects/{pid}"), (204,))
        r = c.get("/api/projects")
        if step("GET /api/projects (软删后不可见)", r):
            visible = [p["id"] for p in r.json()]
            print(f"       软删项目 {pid} 仍可见? {'是 ← BUG' if pid in visible else '否 ✓'}")

    print(f"\n==== 冒烟结果: OK={ok}  FAIL={fail} ====")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
