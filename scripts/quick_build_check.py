"""Quick targeted check: verify build skill resume produces a preview.

流程: 注册 → 项目/对话 → 需求 → 建站触发(期望 paused) → resume(期望 preview+done)
仅 4 次 LLM 调用, 用于快速验证 checkpoint 续跑链路修复。
"""
import asyncio, json, re, time, httpx

BASE = "http://127.0.0.1:7101"
TOKEN = None
PID = CID = None
UNAME = None


async def register(c):
    global TOKEN, UNAME
    UNAME = f"qb_{int(time.time())}"
    r = await c.post(f"{BASE}/auth/register",
                     json={"username": UNAME, "password": "Test123456", "nickname": "QB"})
    m = re.search(r"access_token=([^;]+)", r.headers.get("set-cookie", ""))
    TOKEN = m.group(1) if m else None
    return r.status_code, bool(TOKEN)


async def proj_conv(c):
    global PID, CID
    r = await c.post(f"{BASE}/api/projects",
                     json={"name": "QB", "description": "x"},
                     headers={"Authorization": f"Bearer {TOKEN}"})
    PID = r.json().get("id")
    r = await c.post(f"{BASE}/api/conversations",
                     json={"title": "QB", "project_id": PID},
                     headers={"Authorization": f"Bearer {TOKEN}"})
    CID = r.json().get("id")


async def chat(c, text, resume=False, timeout=240):
    p = {"model": "deepseek", "conversation_id": CID, "q": text, "token": TOKEN}
    if resume:
        p["resume"] = "true"
    res = {"done": False, "paused": False, "preview": None, "refined": False,
           "error": False, "errmsg": None, "events": 0, "tokens": 0}
    t0 = time.time()
    async with c.stream("GET", f"{BASE}/api/chat", params=p, timeout=timeout) as resp:
        if resp.status_code != 200:
            res["error"] = True
            res["errmsg"] = f"HTTP {resp.status_code}"
            return res
        ev = None; parts = []
        async for raw in resp.aiter_lines():
            if raw == "":
                if ev or parts:
                    data = "".join(parts)
                    if ev == "done": res["done"] = True
                    elif ev == "paused": res["paused"] = True
                    elif ev == "refined": res["refined"] = True
                    elif ev == "preview":
                        try: res["preview"] = json.loads(data).get("url")
                        except Exception: pass
                    elif ev == "error":
                        res["error"] = True
                        try: res["errmsg"] = json.loads(data).get("message", data)
                        except Exception: res["errmsg"] = data
                    elif ev == "token":
                        try:
                            o = json.loads(data)
                            if isinstance(o.get("data"), str): res["tokens"] += len(o["data"])
                        except Exception: pass
                    res["events"] += 1
                ev = None; parts = []
                continue
            if raw.startswith("event:"): ev = raw[6:].strip()
            elif raw.startswith("data:"): parts.append(raw[5:].strip())
    res["elapsed"] = round(time.time() - t0, 1)
    return res


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=10, pool=20)) as c:
        st, ok = await register(c)
        print(f"[注册] {UNAME} HTTP={st} token={'OK' if ok else 'FAIL'}")
        if not ok: return
        await proj_conv(c)
        print(f"[项目/对话] pid={PID} cid={CID}")

        # 1) 需求
        r1 = await chat(c, "帮我做一个个人摄影作品集网站，标题叫光影集，白色简约风格")
        print(f"[需求] done={r1['done']} paused={r1['paused']} ev={r1['events']} tok={r1['tokens']} {r1['elapsed']}s")

        # 2) 建站触发
        r2 = await chat(c, "开始生成网站吧")
        print(f"[建站触发] done={r2['done']} paused={r2['paused']} ev={r2['events']} prev={'Y' if r2['preview'] else 'N'} {r2['elapsed']}s")

        # 3) resume
        if r2["paused"]:
            r3 = await chat(c, "确认并开始生成网站", resume=True)
            print(f"[resume] done={r3['done']} paused={r3['paused']} err={r3['error']} "
                  f"prev={'Y' if r3['preview'] else 'N'} ref={r3['refined']} ev={r3['events']} {r3['elapsed']}s"
                  + (f" ERR={r3['errmsg']}" if r3['errmsg'] else ""))
            resume_ok = (r3["done"] and r3["preview"] and not r3["error"])
            # 4) Fix I 验证: 站已生成后, 迭代修改消息应直接走建站产出预览(不再被判 chat)
            r4 = await chat(c, "加上hover效果，鼠标悬停图片时放大")
            print(f"[迭代] done={r4['done']} paused={r4['paused']} err={r4['error']} "
                  f"prev={'Y' if r4['preview'] else 'N'} ref={r4['refined']} ev={r4['events']} {r4['elapsed']}s"
                  + (f" ERR={r4['errmsg']}" if r4['errmsg'] else ""))
            iter_ok = (r4["done"] and r4["preview"] and not r4["error"] and not r4["paused"])
            print("\nRESULT:", "PASS ✅ (resume+迭代均产出预览)" if (resume_ok and iter_ok) else "FAIL ❌")
        else:
            print("\nRESULT: 未触发 paused(可能需求未齐/意图误判), 无法验证 resume。")


if __name__ == "__main__":
    asyncio.run(main())
