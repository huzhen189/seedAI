"""M7 项目运维端到端冒烟: 生成 -> 发布 -> 回收 -> 恢复 -> 清除。

对齐规范 §8.4(ProjectOps) / §10.4(Artifact/Deployment 分离) / §10.6(purge 分步 job)
与验收矩阵 REQ-SITE-001 / REQ-DEPLOY-001 / REQ-DATA-001。

验证点:
  1. 生成网站 -> Artifact preview_ready, project.head_artifact_id 落位
  2. 发布(高危 -> S5 审批闸门) -> 批准后真实执行 Deployment Saga
     -> deployment.status=succeeded, project.published_artifact_id/active_deployment_id 切换
     -> published/{uid}/{pid}/v{n}/index.html 真实落盘
  3. 同一 approval 重放决策 -> 409(单次消费), 不产生第二个 Deployment
  4. 回收(trash) -> project.status=trashed, 从 /api/projects 列表消失
  5. 恢复(restore) -> 回到 active(低危, 无需审批)
  6. 清除(purge) -> HTTP 内只冻结+建 job, 后台 job 跑完后行被物理删除

固定测试账号(可复现): e2e20_seedai_test / testpass123
用法:
  python scripts/smoke_project_ops.py
  SMOKE_BASE=http://127.0.0.1:7100 python scripts/smoke_project_ops.py   # 走 vite 代理
"""

from __future__ import annotations

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
ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", str(Path(__file__).resolve().parent.parent / "artifacts")))
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
    return bool(condition)


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
            print("       (stream budget exhausted)")
            break
    return events


def send_chat(client: httpx.Client, conv_id: int, message: str) -> list[dict[str, Any]]:
    body = {
        "client_msg_id": f"ops-{uuid.uuid4().hex[:12]}",
        "conversation_id": conv_id,
        "message": message,
    }
    with client.stream("POST", "/api/chat", json=body, timeout=STREAM_TIMEOUT) as resp:
        if resp.status_code != 200:
            print(f"       chat 非 200: {resp.status_code} {resp.read()[:300]!r}")
            return []
        return parse_sse(resp.iter_text(), STREAM_TIMEOUT)


def terminal_of(events: list[dict[str, Any]]) -> tuple[str, str]:
    for e in events:
        if e.get("_event") == "done":
            data = e.get("_data", {}).get("data") or {}
            return str(data.get("status", "")), str(data.get("reply", ""))
    return "", ""


def approval_of(events: list[dict[str, Any]]) -> tuple[str, str]:
    for e in events:
        if e.get("_event") == "approval":
            data = e.get("_data", {}).get("data") or {}
            return str(data.get("approval_id", "")), str(data.get("decision_nonce", ""))
    return "", ""


def turn_id_of(events: list[dict[str, Any]]) -> str:
    return str(events[0].get("_data", {}).get("turn_id", "")) if events else ""


def find_project(client: httpx.Client, project_id: int) -> dict[str, Any] | None:
    resp = client.get("/api/projects")
    if resp.status_code != 200:
        return None
    for item in resp.json():
        if item.get("id") == project_id:
            return item
    return None


def main() -> int:  # noqa: PLR0915 - 端到端脚本按阶段线性铺开更易读
    with httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=True) as c:
        print("== 1. 认证 ==")
        c.post(
            "/auth/register",
            json={"account": ACCOUNT, "password": PASSWORD, "display_name": "E2E 运维", "email": None},
        )
        r = c.post("/auth/login", json={"account": ACCOUNT, "password": PASSWORD})
        if not check("login", r.status_code == 200, str(r.status_code)):
            return 1
        c.headers["Authorization"] = f"Bearer {r.json().get('access_token') or r.json().get('token')}"

        print("== 2. auto-start(单一项目贯穿全流程) ==")
        r = c.post("/api/auto-start", json={"text": "做一个极简风格的咖啡店官网"})
        if not check("POST /api/auto-start", r.status_code == 200, str(r.status_code)):
            return 1
        conv_id = int(r.json()["conversation"]["id"])
        proj_id = int(r.json()["project"]["id"])
        print(f"       conversation_id={conv_id} project_id={proj_id}")

        print("== 3. 生成网站(REQ-SITE-001) ==")
        events = send_chat(c, conv_id, "帮我做一个咖啡店官网首页，要有 hero 区和菜单展示")
        status, reply = terminal_of(events)
        check("生成 Turn 到达 completed", status == "completed", f"status={status} reply={reply[:40]}")
        view = find_project(c, proj_id) or {}
        head_id = view.get("head_artifact_id")
        check("project.head_artifact_id 已落位", bool(head_id), f"head={head_id}")
        check("尚未发布 -> has_unpublished_changes", view.get("has_unpublished_changes") is True, str(view))

        print("== 4. 发布(REQ-DEPLOY-001, 经 S5 审批闸门) ==")
        events = send_chat(c, conv_id, "帮我发布这个项目官网")
        status, _ = terminal_of(events)
        check("发布 Turn 暂停于 waiting_approval", status == "waiting_approval", f"status={status}")
        approval_id, nonce = approval_of(events)
        pub_turn = turn_id_of(events)
        if not check("拿到 approval_id 与一次性 nonce", bool(approval_id and nonce), approval_id):
            return 1

        r = c.post(f"/api/gate/{approval_id}", json={"decision": "approve", "decision_nonce": nonce})
        check("审批决策 200", r.status_code == 200, str(r.status_code))
        time.sleep(0.8)
        r = c.get(f"/api/turns/{pub_turn}")
        turn_status = r.json().get("status") if r.status_code == 200 else "?"
        check("发布 Turn 收口为 completed", turn_status == "completed", f"status={turn_status}")

        view = find_project(c, proj_id) or {}
        check(
            "published_artifact_id 已切到 head",
            view.get("published_artifact_id") == head_id,
            f"published={view.get('published_artifact_id')} head={head_id}",
        )
        check("active_deployment_id 已写入", bool(view.get("active_deployment_id")), str(view.get("active_deployment_id")))
        check("发布后无未发布改动", view.get("has_unpublished_changes") is False, str(view.get("has_unpublished_changes")))

        published = ARTIFACT_DIR / "published"
        hit = list(published.glob(f"*/{proj_id}/v*/index.html")) if published.exists() else []
        check("published 目录已落盘 index.html", bool(hit), str(hit[0]) if hit else f"missing under {published}")

        print("== 5. 审批重放(单次消费) ==")
        r = c.post(f"/api/gate/{approval_id}", json={"decision": "approve", "decision_nonce": nonce})
        check("重放决策被拒绝(409)", r.status_code == 409, f"{r.status_code} {r.text[:120]}")

        print("== 6. 回收(trash) ==")
        events = send_chat(c, conv_id, "把这个项目放进回收站")
        status, _ = terminal_of(events)
        approval_id, nonce = approval_of(events)
        trash_turn = turn_id_of(events)
        if check("回收触发审批", bool(approval_id and nonce), f"status={status}"):
            r = c.post(f"/api/gate/{approval_id}", json={"decision": "approve", "decision_nonce": nonce})
            check("回收决策 200", r.status_code == 200, str(r.status_code))
            time.sleep(0.8)
            r = c.get(f"/api/turns/{trash_turn}")
            st = r.json().get("status") if r.status_code == 200 else "?"
            check("回收 Turn 收口为 completed", st == "completed", f"status={st}")
        check("trashed 项目从 /api/projects 列表消失", find_project(c, proj_id) is None, "list filtered")

        print("== 7. 恢复(restore, 低危不走审批) ==")
        events = send_chat(c, conv_id, "把这个项目从回收站恢复出来")
        status, reply = terminal_of(events)
        approval_id, nonce = approval_of(events)
        check("恢复不触发审批闸门", not approval_id, f"approval={approval_id or '-'}")
        check("恢复 Turn 到达 completed", status == "completed", f"status={status} reply={reply[:40]}")
        view = find_project(c, proj_id)
        check("项目回到可见列表", view is not None, f"status={(view or {}).get('status')}")
        check("发布指针未被回收/恢复破坏", (view or {}).get("published_artifact_id") == head_id, str((view or {}).get("published_artifact_id")))

        print("== 8. 清除(purge, REQ-DATA-001 分步 job) ==")
        events = send_chat(c, conv_id, "把这个项目放进回收站")
        approval_id, nonce = approval_of(events)
        if approval_id:
            c.post(f"/api/gate/{approval_id}", json={"decision": "approve", "decision_nonce": nonce})
        events = send_chat(c, conv_id, "彻底删除这个项目，不要保留任何数据")
        status, _ = terminal_of(events)
        approval_id, nonce = approval_of(events)
        purge_turn = turn_id_of(events)
        if check("清除触发审批", bool(approval_id and nonce), f"status={status}"):
            r = c.post(f"/api/gate/{approval_id}", json={"decision": "approve", "decision_nonce": nonce})
            check("清除决策 200(HTTP 内不同步完成)", r.status_code == 200, str(r.status_code))
            r = c.get(f"/api/turns/{purge_turn}")
            st = r.json().get("status") if r.status_code == 200 else "?"
            check("清除 Turn 已收口", st in {"completed", "failed"}, f"status={st}")
            # 后台 job 分步推进, 给它时间跑完 7 步
            pub_dir = ARTIFACT_DIR / "published" / "1" / str(proj_id)
            prev_dir = ARTIFACT_DIR / "previews" / "1" / str(proj_id)
            gone = False
            for _ in range(25):
                time.sleep(1.0)
                if not pub_dir.exists() and not prev_dir.exists():
                    gone = True
                    break
            check("published+previews 目录已被 purge job 物理清空", gone, f"pub={pub_dir.exists()} prev={prev_dir.exists()}")
            check("purge 后项目不在列表", find_project(c, proj_id) is None, "list filtered")
            # delete_rows 的 HTTP 侧证据: 会话 FK ON DELETE CASCADE, 项目行真删了会话才会消失。
            r = c.get("/api/conversations")
            convs = r.json() if r.status_code == 200 else []
            still = [x for x in convs if x.get("id") == conv_id]
            check("delete_rows 已执行(会话随项目级联消失)", not still, f"remaining={len(still)}")

    print(f"\n==== 结果: {ok} passed, {fail} failed ====")
    print(f"测试账号: {ACCOUNT} / {PASSWORD}  后端: {BASE}  项目: {proj_id}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
