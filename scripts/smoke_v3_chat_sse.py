"""十阶段主链路 SSE 冒烟: POST /api/chat 真实跑通 S0-S9。

验证点:
  1. SSE 流可建立, 帧格式合法(id/event/data)
  2. S0-S9 十个阶段事件齐全且顺序正确
  3. Turn 终态落库, assistant 消息写入
  4. 幂等: 同 client_msg_id 重复提交不重复执行
  5. 断线续传: GET /api/streams/{stream_id}?after=N 只回放增量
  6. 快照: GET /api/turns/{turn_id}

固定测试账号(可复现, 便于登录前端复查): e2e20_seedai_test / testpass123
用法: python scripts/smoke_v3_chat_sse.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pymysql

BASE = "http://127.0.0.1:7101"
ACCOUNT = "e2e20_seedai_test"
PASSWORD = "testpass123"
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


def db_conn() -> pymysql.connections.Connection:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    env = dict(re.findall(r"^([A-Z_]+)=(.*)$", env_path.read_text(encoding="utf-8"), re.M))
    m = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)", env["MYSQL_URL"])
    if m is None:
        raise SystemExit("MYSQL_URL 解析失败")
    return pymysql.connect(
        host=m.group(3),
        port=int(m.group(4)),
        user=m.group(1),
        password=m.group(2),
        database=m.group(5),
        charset="utf8mb4",
    )


def parse_sse(chunk_iter: Any, budget: float) -> list[dict[str, Any]]:
    """解析 SSE 字节流为事件列表。遇终态事件或超时即停。"""
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
            json={"account": ACCOUNT, "password": PASSWORD, "display_name": "E2E 冒烟", "email": None},
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

        print("== 3. POST /api/chat 读取 SSE 主链路 ==")
        client_msg_id = f"smoke-{uuid.uuid4().hex[:12]}"
        body = {
            "client_msg_id": client_msg_id,
            "conversation_id": conv_id,
            "message": "帮我做一个咖啡店官网首页,要有 hero 区和菜单展示",
        }
        events: list[dict[str, Any]] = []
        t0 = time.monotonic()
        try:
            with c.stream("POST", "/api/chat", json=body, timeout=STREAM_TIMEOUT) as resp:
                ctype = resp.headers.get("content-type", "")
                check("SSE 响应状态", resp.status_code == 200, str(resp.status_code))
                check("content-type 为 text/event-stream", "text/event-stream" in ctype, ctype)
                if resp.status_code != 200:
                    print(f"       body={resp.read()[:400]!r}")
                    return 1
                events = parse_sse(resp.iter_text(), STREAM_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            check("SSE 流读取", False, f"{type(exc).__name__}: {exc}")
            return 1
        elapsed = time.monotonic() - t0
        print(f"       收到 {len(events)} 个事件, 耗时 {elapsed:.1f}s")

        for e in events:
            data = e.get("_data", {})
            stage = data.get("data", {}).get("stage") if isinstance(data.get("data"), dict) else None
            print(f"       seq={e.get('_id'):>3}  {e.get('_event','?'):<22} {('stage=' + str(stage)) if stage else ''}")

        check("收到事件", len(events) > 0, f"{len(events)} events")
        if not events:
            return 1

        # 帧契约
        first = events[0].get("_data", {})
        for field in ("stream_id", "turn_id", "trace_id", "event_id", "seq", "timestamp", "type"):
            check(f"事件字段 {field}", field in first, "" if field in first else f"缺失, keys={list(first)}")

        turn_id = first.get("turn_id")
        stream_id = first.get("stream_id")
        print(f"       turn_id={turn_id}  stream_id={stream_id}")

        # 阶段覆盖
        stages_seen: list[str] = []
        for e in events:
            d = e.get("_data", {})
            inner = d.get("data") if isinstance(d.get("data"), dict) else {}
            s = (inner or {}).get("stage")
            if isinstance(s, str) and s not in stages_seen:
                stages_seen.append(s)
        expected = [f"S{i}" for i in range(10)]
        missing = [s for s in expected if s not in stages_seen]
        check("S0-S9 阶段事件齐全", not missing, f"seen={stages_seen} missing={missing}")
        if stages_seen:
            order_ok = stages_seen == sorted(stages_seen, key=lambda x: expected.index(x) if x in expected else 99)
            check("阶段顺序单调递增", order_ok, "->".join(stages_seen))

        # seq 单调
        seqs = [int(e["_id"]) for e in events if e.get("_id", "").isdigit()]
        check("seq 严格递增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"{seqs[:12]}{'...' if len(seqs) > 12 else ''}")

        types = [e.get("_event") for e in events]
        print(f"       事件类型集合: {sorted(set(t for t in types if t))}")

        print("== 4. Turn 快照 ==")
        r = c.get(f"/api/turns/{turn_id}")
        if check("GET /api/turns/{id}", r.status_code == 200, str(r.status_code)):
            snap = r.json()
            st = snap.get("status") or snap.get("turn", {}).get("status")
            check("Turn 已到终态", st in ("succeeded", "completed", "failed", "cancelled"), f"status={st}")
            print(f"       snapshot keys={list(snap)[:10]}")

        print("== 5. 断线续传 ==")
        after = seqs[len(seqs) // 2] if len(seqs) >= 2 else 0
        try:
            with c.stream("GET", f"/api/streams/{stream_id}", params={"after": after}, timeout=30.0) as resp:
                check("GET /api/streams/{id}", resp.status_code == 200, str(resp.status_code))
                if resp.status_code == 200:
                    replay = parse_sse(resp.iter_text(), 15.0)
                    rseqs = [int(e["_id"]) for e in replay if e.get("_id", "").isdigit()]
                    check(
                        f"续传只回放 seq>{after}",
                        all(s > after for s in rseqs) if rseqs else True,
                        f"replay={len(replay)} events seqs={rseqs[:8]}",
                    )
        except Exception as exc:  # noqa: BLE001
            check("续传流读取", False, f"{type(exc).__name__}: {exc}")

        print("== 6. 幂等(同 client_msg_id 重复提交) ==")
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM turns WHERE client_msg_id=%s", (client_msg_id,))
        before = cur.fetchone()[0]
        try:
            with c.stream("POST", "/api/chat", json=body, timeout=60.0) as resp:
                check("重复提交返回 200", resp.status_code == 200, str(resp.status_code))
                parse_sse(resp.iter_text(), 10.0)
        except Exception as exc:  # noqa: BLE001
            print(f"       (重复提交流读取异常, 不致命: {type(exc).__name__})")
        cur.execute("SELECT COUNT(*) FROM turns WHERE client_msg_id=%s", (client_msg_id,))
        after_n = cur.fetchone()[0]
        check("未产生重复 Turn", before == after_n == 1, f"before={before} after={after_n}")

        print("== 7. 落库校验 ==")
        cur.execute("SELECT status, stream_id FROM turns WHERE turn_id=%s", (turn_id,))
        row = cur.fetchone()
        check("turns 行存在", row is not None, f"status={row[0] if row else None}")
        cur.execute("SELECT role, LEFT(content,40) FROM messages WHERE turn_id=%s ORDER BY id", (turn_id,))
        msgs = cur.fetchall()
        roles = [m[0] for m in msgs]
        check("user 消息落库", "user" in roles, f"roles={roles}")
        check("assistant 消息落库", "assistant" in roles, f"roles={roles}")
        for role, snippet in msgs:
            print(f"       {role}: {snippet!r}")
        cur.execute("SELECT event_type FROM outbox_events WHERE aggregate_id=%s ORDER BY id", (turn_id,))
        obx = [r[0] for r in cur.fetchall()]
        check("outbox 有受理事件", "turn.accepted" in obx, f"{obx}")
        check(
            "outbox 有终态事件",
            any(e.startswith("turn.") and e != "turn.accepted" for e in obx),
            f"{obx}",
        )
        # artifacts 表按 project 归属(无 turn_id 列)，唯一约束为 project_id+version
        cur.execute("SELECT version, status FROM artifacts WHERE project_id=%s ORDER BY version", (proj_id,))
        arts = cur.fetchall()
        check("artifact 已产出", len(arts) > 0, f"versions={[a[0] for a in arts]} status={[a[1] for a in arts]}")
        conn.close()

    print(f"\n==== 结果: {ok} passed, {fail} failed ====")
    print(f"测试账号: {ACCOUNT} / {PASSWORD}  后端: {BASE}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
