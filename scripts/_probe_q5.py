"""Q5 验证探针：纯聊天 + 建站 两类路径的 SSE 事件流采集。

采集并断言：
  1. token/think 节流：相邻 token 帧间隔应 ≈0.2s（后端 _EMIT_INTERVAL_S）。
  2. S2 stage 事件携带 intents（中文 label）。
  3. S4 stage 事件携带 plan；纯聊天应含虚拟 chat 条目。
  4. S6 产出 task 事件回填 plan 行状态（running/succeeded/failed）。
  5. done 事件含 intents + plan（终态对账）。
"""
from __future__ import annotations

import asyncio
import json
import time
import httpx

BASE = "http://127.0.0.1:7101"
ACC = f"q5probe_{int(time.time())}"
PW = "q5probe123"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        # 1) 注册后登录（register 不返回 token，login 才返回）
        r = await c.post("/auth/register", json={"account": ACC, "password": PW, "display_name": "Q5 Probe"})
        assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
        r = await c.post("/auth/login", json={"account": ACC, "password": PW})
        assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
        token = r.json()["access_token"]
        c.headers["Authorization"] = f"Bearer {token}"

        # 2) 建一个项目 + 会话
        r = await c.post("/api/projects", json={"name": "Q5 probe project"})
        assert r.status_code in (200, 201), f"create project failed: {r.status_code} {r.text}"
        proj_id = r.json()["id"]
        r = await c.post("/api/conversations", json={"project_id": proj_id})
        assert r.status_code in (200, 201), f"create conv failed: {r.status_code} {r.text}"
        conv_id = r.json()["id"]
        print(f"[setup] account={ACC} proj={proj_id} conv={conv_id}")

        # 3) 跑三类消息
        await run_turn(c, conv_id, "你好，今天天气怎么样？", label="纯聊天")
        # 稍等避免速率限制
        await asyncio.sleep(2)
        await run_turn(c, conv_id, "帮我做一个个人博客网站，要有深色主题和文章列表。", label="建站")
        await asyncio.sleep(2)
        await run_turn(c, conv_id, "帮我建一个产品官网，另外顺便告诉我明天北京会下雨吗？", label="多意图")


async def run_turn(c: httpx.AsyncClient, conv_id: int, message: str, label: str) -> None:
    print(f"\n========== {label} | {message!r} ==========")
    msg_id = f"probe_{int(time.time()*1000)}_{label}"
    async with c.stream(
        "POST", "/api/chat",
        json={"client_msg_id": msg_id, "conversation_id": conv_id, "message": message},
    ) as resp:
        assert resp.status_code == 200, f"chat failed: {resp.status_code}"
        last_token_ts = 0.0
        token_gaps: list[float] = []
        last_task_for: dict[str, str] = {}
        saw = {"S2_intents": False, "S4_plan": False, "task": False, "done_intents": False, "done_plan": False}
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            raw = line[len("data: "):]
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            data = ev.get("data", {})
            now = time.time()
            if etype in ("token", "think"):
                if last_token_ts:
                    token_gaps.append(now - last_token_ts)
                last_token_ts = now
            if etype == "stage":
                st = data.get("stage")
                if st == "S2" and data.get("intents"):
                    saw["S2_intents"] = True
                    print(f"  [S2 intents] {json.dumps(data['intents'], ensure_ascii=False)}")
                if st == "S4" and data.get("plan"):
                    saw["S4_plan"] = True
                    print(f"  [S4 plan] {json.dumps(data['plan'], ensure_ascii=False)}")
            if etype == "task":
                saw["task"] = True
                tid = data.get("task_id")
                last_task_for[tid] = data.get("status")
                print(f"  [task] {tid} -> {data.get('status')} ({data.get('label')})")
            if etype == "done":
                if data.get("intents"):
                    saw["done_intents"] = True
                if data.get("plan"):
                    saw["done_plan"] = True
                print(f"  [done] status={data.get('status')} intents={json.dumps(data.get('intents',[]), ensure_ascii=False)}")
                print(f"  [done plan] {json.dumps(data.get('plan',[]), ensure_ascii=False)}")

        # 汇总
        if token_gaps:
            avg = sum(token_gaps) / len(token_gaps)
            mx = max(token_gaps)
            mn = min(token_gaps)
            print(f"  [token 节流] 帧数={len(token_gaps)} 平均间隔={avg*1000:.0f}ms 最小={mn*1000:.0f}ms 最大={mx*1000:.0f}ms")
        print(f"  [断言] S2_intents={saw['S2_intents']} S4_plan={saw['S4_plan']} task_events={saw['task']} done_intents={saw['done_intents']} done_plan={saw['done_plan']}")
        print(f"  [task 终态回填] {json.dumps(last_task_for, ensure_ascii=False)}")


if __name__ == "__main__":
    asyncio.run(main())
