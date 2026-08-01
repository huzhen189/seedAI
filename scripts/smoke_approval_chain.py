"""审批链路端到端冒烟: 验证「非盲审批」从触发到决策闭环。

验证点:
  1. 发送会触发审批的意图(发布/删除/彻底删除) -> SSE 出现 approval 事件且 decision_nonce 非空
  2. S5 暂停 reason_code = approval_created, 终态 done.status = waiting_approval
  3. GET /api/gate/pending 能取到该待决审批(注意: 不含明文 nonce)
  4. POST /api/gate/{id} 用 decision_nonce 决策 -> 200, approval 状态变更
  5. 决策后 Turn 是否到达终态(本脚本只观测, 不假设必定 completed)

固定测试账号(可复现): e2e20_seedai_test / testpass123
用法:
  python scripts/smoke_approval_chain.py                 # 直连 :7101
  SMOKE_BASE=http://127.0.0.1:7100 python scripts/smoke_approval_chain.py  # 走 vite 代理
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:7101")
# approve(默认) / reject: 验证两种决策都能把 Turn 收口到终态
DECISION = os.environ.get("DECISION", "approve")
ACCOUNT = os.environ.get("E2E_USER", "e2e20_seedai_test")
PASSWORD = os.environ.get("E2E_PW", "testpass123")
STREAM_TIMEOUT = 180.0

ok = 0
fail = 0


def check(name: str, condition: bool, detail: str = "") -> bool:
    global ok, fail
    if condition:
        ok += 1
        print(f"  [OK ] {name}" + (f" -> {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))
    return condition


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
                if line.startswith("id: "):
                    evt["_id"] = line[4:]
                elif line.startswith("event: "):
                    evt["_event"] = line[7:]
                elif line.startswith("data: "):
                    try:
                        evt["_data"] = json.loads(line[6:])
                    except json.JSONDecodeError:
                        evt["_data"] = {"_raw": line[6:]}
            if evt:
                events.append(evt)
        if time.monotonic() > deadline:
            print("       (stream budget exhausted, stop reading)")
            break
    return events


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=True) as c:
        print("== 1. 认证 ==")
        c.post(
            "/auth/register",
            json={"account": ACCOUNT, "password": PASSWORD, "display_name": "E2E 审批", "email": None},
        )
        r = c.post("/auth/login", json={"account": ACCOUNT, "password": PASSWORD})
        if not check("login", r.status_code == 200, str(r.status_code)):
            return 1
        token = r.json().get("access_token") or r.json().get("token")
        c.headers["Authorization"] = f"Bearer {token}"

        print("== 2. auto-start ==")
        r = c.post("/api/auto-start", json={"text": "做一个极简风格的咖啡店官网"})
        if not check("POST /api/auto-start", r.status_code == 200, str(r.status_code)):
            return 1
        conv_id = r.json()["conversation"]["id"]
        proj_id = r.json()["project"]["id"]
        print(f"       conversation_id={conv_id} project_id={proj_id}")

        # 前置: 先生成一版网站产物。发布是对 head artifact 的操作,
        # 没有产物时 ProjectOps 会正确地拒绝发布(no_artifact), 那样测不到审批放行后的真实执行。
        print("== 2.5 先生成一版网站(为发布准备 head artifact) ==")
        with c.stream(
            "POST",
            "/api/chat",
            json={
                "client_msg_id": f"seed-{uuid.uuid4().hex[:12]}",
                "conversation_id": conv_id,
                "message": "帮我做一个咖啡店官网首页",
            },
            timeout=STREAM_TIMEOUT,
        ) as resp:
            seed_events = parse_sse(resp.iter_text(), STREAM_TIMEOUT) if resp.status_code == 200 else []
        seed_done = [e for e in seed_events if e.get("_event") == "done"]
        seed_status = (seed_done[0].get("_data", {}).get("data") or {}).get("status", "") if seed_done else ""
        check("网站生成 Turn completed", seed_status == "completed", f"status={seed_status}")

        # 触发审批: "发布" -> PUBLISH(speech_act) -> S5 needs_approval
        print("== 3. POST /api/chat (触发审批: 发布) ==")
        client_msg_id = f"approve-{uuid.uuid4().hex[:12]}"
        body = {
            "client_msg_id": client_msg_id,
            "conversation_id": conv_id,
            "message": "帮我发布这个项目官网",
        }
        events: list[dict[str, Any]] = []
        try:
            with c.stream("POST", "/api/chat", json=body, timeout=STREAM_TIMEOUT) as resp:
                check("SSE 响应 200", resp.status_code == 200, str(resp.status_code))
                if resp.status_code != 200:
                    print(f"       body={resp.read()[:400]!r}")
                    return 1
                events = parse_sse(resp.iter_text(), STREAM_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            check("SSE 流读取", False, f"{type(exc).__name__}: {exc}")
            return 1

        for e in events:
            d = e.get("_data", {})
            inner = d.get("data") if isinstance(d.get("data"), dict) else {}
            label = inner.get("stage") or e.get("_event")
            print(f"       seq={e.get('_id'):>3}  {e.get('_event','?'):<22} {label or ''}")

        first = events[0].get("_data", {})
        turn_id = first.get("turn_id")
        stream_id = first.get("stream_id")
        print(f"       turn_id={turn_id}  stream_id={stream_id}")

        # 审批事件
        approval_events = [e for e in events if e.get("_event") == "approval"]
        check("SSE 出现 approval 事件", len(approval_events) >= 1, f"count={len(approval_events)}")
        nonce = ""
        approval_id = ""
        if approval_events:
            adata = approval_events[0].get("_data", {}).get("data", {})
            approval_id = adata.get("approval_id", "")
            nonce = adata.get("decision_nonce", "")
            check("approval 事件含 approval_id", bool(approval_id), approval_id)
            check("approval 事件含一次性 decision_nonce", bool(nonce), f"nonce_len={len(nonce)}")
            print(f"       approval_id={approval_id}  action={adata.get('action')}  risk={adata.get('risk_level')}")

        # S5 暂停
        s5 = [e for e in events if (e.get('_data', {}).get('data') or {}).get('stage') == 'S5']
        if s5:
            rc = (s5[0].get('_data', {}).get('data') or {}).get('reason_code')
            check("S5 reason_code = approval_created", rc == "approval_created", f"rc={rc}")

        # 终态 done
        done = [e for e in events if e.get('_event') == 'done']
        terminal = ""
        if done:
            terminal = (done[0].get('_data', {}).get('data') or {}).get('status', '')
        check("done 终态为 waiting_approval", terminal == "waiting_approval", f"status={terminal}")

        print("== 4. Turn 快照 ==")
        r = c.get(f"/api/turns/{turn_id}")
        if check("GET /api/turns/{id}", r.status_code == 200, str(r.status_code)):
            st = r.json().get("status")
            check("Turn 状态 = waiting_approval", st == "waiting_approval", f"status={st}")

        print("== 5. GET /api/gate/pending (明文 nonce 不应出现) ==")
        r = c.get("/api/gate/pending")
        pending = (r.json() or {}).get("approvals", [])
        check("pending 含待决审批", any(a.get("approval_id") == approval_id for a in pending), f"count={len(pending)}")
        leaked = any(a.get("decision_nonce") or a.get("challenge_nonce") for a in pending)
        check("pending 不含明文 nonce(非盲审批)", not leaked, f"leaked={leaked}")

        print("== 6. POST /api/gate/{id} 审批决策 ==")
        if not approval_id or not nonce:
            check("前置: 有 approval_id 与 nonce", False, "缺少审批事件数据, 跳过决策")
            return 1
        expect_status = "approved" if DECISION == "approve" else "rejected"
        r = c.post(f"/api/gate/{approval_id}", json={"decision": DECISION, "decision_nonce": nonce})
        check("决策返回 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            ap = r.json()
            print(f"       决策后 approval.status={ap.get('status')}  action={ap.get('action')}")
            check(f"决策后状态变为 {expect_status}", ap.get("status") == expect_status, f"status={ap.get('status')}")

        print("== 7. 决策后 Turn 终态观测 ==")
        time.sleep(1.0)
        expect_terminal = "completed" if DECISION == "approve" else "cancelled"
        r = c.get(f"/api/turns/{turn_id}")
        if check("GET /api/turns/{id} 复查", r.status_code == 200, str(r.status_code)):
            st2 = r.json().get("status")
            print(f"       决策后 Turn 状态 = {st2}")
            # 关键断言: 不能永远卡在 waiting_approval(闭环必须到达终态)
            check("Turn 已离开 waiting_approval(闭环闭合)", st2 != "waiting_approval", f"status={st2}")
            check(f"Turn 终态为 {expect_terminal}", st2 == expect_terminal, f"status={st2}")

    print(f"\n==== 结果: {ok} passed, {fail} failed ====")
    print(f"测试账号: {ACCOUNT} / {PASSWORD}  后端: {BASE}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
