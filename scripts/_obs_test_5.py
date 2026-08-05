"""种子 5 条语句观测测试（一次性，聚焦三大模块日志覆盖）。

目标：模拟 5 条语句，重点观察后端日志是否包含
  - 记忆模块 读([S1]) 与 写([S7] 记忆写入已派发 / [memory_write])
  - 子任务 ReAct 执行模块([site] 开始建站/第N轮代码执行/建站完成 + [S6])
  - 工具调用模块([tool_runner] ▶ 执行 / ■ 完成)

本脚本只负责发语句 + 记录每条结果 + 记录日志偏移，供后续 grep。
"""
import asyncio
import json
import os
import re
import sys
import time

import httpx

BASE = os.environ.get("TEST_HOST", "http://localhost:7101")
MODEL = os.environ.get("TEST_MODEL", "deepseek")
USER = os.environ.get("TEST_USER", "huzhen")
PASS = os.environ.get("TEST_PASS", "huzhen189")
LOG_PATH = r"E:\work\myTencentYunHome\seedAI\backend\app\logs\app.log"

# (标签, 语句, 期望命中模块)
STATEMENTS = [
    ("1.闲聊·记忆读", "你好", ["mem_read"]),
    ("2.建站·ReAct+工具", "生成一个单文件 HTML 落地页，深色主题配蓝色强调色，包含 Hero 和三个特性卡片",
     ["mem_read", "mem_write", "subtask_react", "tool_call"]),
    ("3.研究·工具调用", "联网查一下今年最新的网页配色趋势，把结论用进去",
     ["mem_read", "mem_write", "tool_call"]),
    ("4.修改·ReAct+工具", "把我网站的导航栏改成响应式汉堡菜单，手机端折叠",
     ["mem_read", "mem_write", "subtask_react", "tool_call"]),
    ("5.项目·工具调用+治理", "帮我回收这个项目",
     ["mem_read", "mem_write", "tool_call"]),
]


async def login(client):
    # 登录既在 JSON body 返回 access_token，也种 cookie `seedai_access`。
    # cookie 名为 seedai_access（非 access_token），故直接从 body 取 token，
    # 后续请求统一用 Authorization: Bearer 头（security.get_current_user 两者都认）。
    r = await client.post(f"{BASE}/auth/login", json={"account": USER, "password": PASS})
    if r.status_code != 200:
        return None
    try:
        return r.json().get("access_token")
    except Exception:
        return None


async def create_conv(client, headers):
    pr = await client.post(f"{BASE}/api/projects", headers=headers,
                           json={"name": "观测测试项目", "description": "5条语句模块覆盖"})
    pid = pr.json().get("id") if pr.status_code in (200, 201) else None
    cr = await client.post(f"{BASE}/api/conversations", headers=headers,
                           json={"title": "观测测试对话", "project_id": pid})
    cid = cr.json().get("id") if cr.status_code in (200, 201) else None
    return pid, cid


async def send_chat(client, headers, conv_id, text, timeout=300):
    t0 = time.time()
    res = {"done": False, "events": 0, "error": False, "tokens": 0,
           "intent": None, "decision": None, "elapsed": 0.0}
    # 新链路 /api/chat 是 POST + JSON 体（见 backend/app/api/turns.py::create_turn）。
    # 字段: client_msg_id / conversation_id / message。trace_id 由后端生成，
    # 此处仅提供幂等 client_msg_id。
    client_msg_id = f"obs-{int(t0*1000)%1000000}-{conv_id}"
    body = {"client_msg_id": client_msg_id, "conversation_id": conv_id, "message": text}
    try:
        async with client.stream("POST", f"{BASE}/api/chat", json=body,
                                 headers={"Authorization": headers.get("Authorization", ""),
                                          "Accept": "text/event-stream"},
                                 timeout=timeout) as resp:
            if resp.status_code != 200:
                res["error"] = True
                res["elapsed"] = round(time.time() - t0, 1)
                return res
            ce, dp = None, []
            async for line in resp.aiter_lines():
                if line == "":
                    # 帧结束：合成事件(含 id:/event:/data:) 在此结算。
                    if ce == "done":
                        res["done"] = True
                        if isinstance(dp_obj, dict):
                            intents = dp_obj.get("intents") or []
                            if intents:
                                i0 = intents[0]
                                res["intent"] = f"{i0.get('level1')}/{i0.get('level2')}"
                            res["decision"] = dp_obj.get("status")
                    elif ce == "error":
                        res["error"] = True
                    elif ce == "token" and isinstance(dp_obj, dict) and isinstance(dp_obj.get("data"), str):
                        res["tokens"] += len(dp_obj["data"])
                    if ce:
                        res["events"] += 1
                    ce, dp, dp_obj = None, [], None
                elif line.startswith("event: "):
                    ce = line[7:].strip()
                elif line.startswith("data: "):
                    raw = line[6:]
                    dp.append(raw)
                    try:
                        dp_obj = json.loads(raw)
                    except json.JSONDecodeError:
                        dp_obj = None
    except Exception:
        res["error"] = True
    res["elapsed"] = round(time.time() - t0, 1)
    return res


async def main():
    # 取日志偏移（发语句前）
    try:
        offset = os.path.getsize(LOG_PATH)
    except OSError:
        offset = 0
    print(f"LOG_OFFSET={offset}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=320, write=10, pool=10)) as client:
        tok = await login(client)
        if not tok:
            print("LOGIN_FAIL")
            return
        hdrs = {"Authorization": f"Bearer {tok}"}
        pid, cid = await create_conv(client, hdrs)
        print(f"PROJECT={pid} CONV={cid}")
        if not cid:
            print("CONV_FAIL")
            return

        rows = []
        for label, text, expect in STATEMENTS:
            r = await send_chat(client, hdrs, cid, text)
            ok = r["done"] and not r["error"]
            print(f"RESULT|{label}|intent={r['intent']}|decision={r['decision']}|"
                  f"done={r['done']}|error={r['error']}|events={r['events']}|"
                  f"tok={r['tokens']}|elapsed={r['elapsed']}|expect={','.join(expect)}|"
                  f"pass={ok}")
            rows.append({"label": label, "text": text, "expect": expect,
                         "intent": r["intent"], "decision": r["decision"],
                         "done": r["done"], "error": r["error"],
                         "events": r["events"], "tokens": r["tokens"],
                         "elapsed": r["elapsed"], "pass": ok})

        ts = time.strftime("%Y%m%d-%H%M%S")
        os.makedirs("reports", exist_ok=True)
        out = {"ts": ts, "base": BASE, "model": MODEL, "user": USER,
               "project_id": pid, "conversation_id": cid,
               "log_offset": offset, "rows": rows}
        with open(f"reports/_obs_test_5_{ts}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"JSON=reports/_obs_test_5_{ts}.json")


if __name__ == "__main__":
    asyncio.run(main())
