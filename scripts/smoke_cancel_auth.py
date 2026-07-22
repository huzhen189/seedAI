"""P0-3 隔离测试:/api/cancel 的登录守卫 + 所有者校验。

单一 TestClient(避免重复 startup 触发已关闭事件循环的 MySQL ping 问题),
并用 fake httpx 拦截对 AI 服务的代理调用,纯验证安全分支。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "business"))

import app.main as m  # noqa: E402
from app import proxy as proxy_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class FakeUser:
    def __init__(self, uid, role="user"):
        self.id = uid
        self.role = role


class FakeResult:
    def scalar_one_or_none(self):
        return self._v

    def __init__(self, v):
        self._v = v


class FakeSession:
    def __init__(self, owner):
        self._owner = owner

    async def execute(self, *a, **k):
        return FakeResult(self._owner)


class FakeResp:
    def json(self):
        return {"ok": True, "proxied": True}


class FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return FakeResp()


def main():
    print("== P0-3 /api/cancel 所有者校验隔离测试 ==")
    # 拦截对 AI 服务的真实代理(owner 通过校验后会调它)
    import httpx as _httpx

    _httpx.AsyncClient = FakeAsyncClient

    with TestClient(m.app) as c:
        # 1) 未登录守卫
        r = c.post("/api/cancel", json={"trace_id": "t"})
        print(f"[no-auth] HTTP {r.status_code} -> {'PASS' if r.status_code == 401 else 'FAIL'}")

        # 2) 所有者(owner=7, user.id=7) → 越过校验 → 代理 AI(被 fake 拦截,200)
        async def db_owner7():
            yield FakeSession(7)

        m.app.dependency_overrides[proxy_mod.get_current_user] = lambda: FakeUser(7)
        m.app.dependency_overrides[proxy_mod.get_db] = db_owner7
        r = c.post("/api/cancel", json={"trace_id": "trace-X"})
        print(f"[owner-self] HTTP {r.status_code} -> {'PASS' if r.status_code == 200 else 'FAIL'}")
        m.app.dependency_overrides.clear()

        # 3) 非所有者(owner=9, user.id=7) → 403
        async def db_owner9():
            yield FakeSession(9)

        m.app.dependency_overrides[proxy_mod.get_current_user] = lambda: FakeUser(7)
        m.app.dependency_overrides[proxy_mod.get_db] = db_owner9
        r = c.post("/api/cancel", json={"trace_id": "trace-X"})
        print(f"[non-owner] HTTP {r.status_code} -> {'PASS' if r.status_code == 403 else 'FAIL'}")
        m.app.dependency_overrides.clear()

        # 4) 超管但非 owner → 仍 403(设计:仅 owner 可取消)
        async def db_owner9b():
            yield FakeSession(9)

        m.app.dependency_overrides[proxy_mod.get_current_user] = lambda: FakeUser(7, "super_admin")
        m.app.dependency_overrides[proxy_mod.get_db] = db_owner9b
        r = c.post("/api/cancel", json={"trace_id": "trace-X"})
        print(f"[superadmin-non-owner] HTTP {r.status_code} -> {'PASS' if r.status_code == 403 else 'FAIL'}")
        m.app.dependency_overrides.clear()


if __name__ == "__main__":
    main()
