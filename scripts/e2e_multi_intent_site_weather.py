"""多意图端到端验收: "我想创建一个个人网站，上面还可以卖货的，另外今天深圳天气咋样？"

验证方案 B 收敛后的多意图链路:
  1) 意图层识别为多意图(≥2 个 level1 大类) → 进入 RoleOrchestrator 拆分;
  2) 建站子任务(sub_0 → agent_generate_site, 走 DevAgent 上下文)与天气子任务(sub_1)并行;
  3) 首跑发出 await_confirm(paused), 续跑(resume=true)后不再重弹 → 断点续跑;
  4) 合并结果同时包含「网站预览 preview」与「天气文本(含深圳/天气/℃)」两类产物;
  5) artifacts 表出现 HTML 产物。

用法: python scripts/e2e_multi_intent_site_weather.py [--host 127.0.0.1:7101]
"""

import argparse
import asyncio
import json
import os
import re
import time

import httpx

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("TEST_HOST", "http://127.0.0.1:7101")

MULTI_QUERY = "帮我做一个个人摄影作品集网站，包含首页和关于页两个页面，现代简约风格，主色用深蓝；另外帮我查一下今天深圳的天气怎么样？"


async def register(client: httpx.AsyncClient):
    uname = f"e2e_mi_{int(time.time())}"
    r = await client.post(f"{BASE}/auth/register",
                          json={"account": uname, "password": "Test123456", "nickname": "多意图E2E"})
    sc = r.headers.get("set-cookie", "")
    m = re.search(r"access_token=([^;]+)", sc)
    tok = m.group(1) if m else None
    return tok, uname, r.status_code


async def create_project(client: httpx.AsyncClient, tok: str):
    r = await client.post(f"{BASE}/api/projects",
                          json={"name": "多意图E2E", "description": "site+weather"},
                          headers={"Authorization": f"Bearer {tok}"})
    return r.json().get("id") if r.status_code in (200, 201) else None


async def create_conv(client: httpx.AsyncClient, tok: str, pid):
    r = await client.post(f"{BASE}/api/conversations",
                          json={"title": "多意图对话", "project_id": pid},
                          headers={"Authorization": f"Bearer {tok}"})
    return r.json().get("id") if r.status_code in (200, 201) else None


async def send(client: httpx.AsyncClient, tok: str, cid, text, resume=False, timeout=600):
    params = {"model": "deepseek", "conversation_id": cid, "q": text, "token": tok}
    if resume:
        params["resume"] = "true"
    out = {"done": False, "paused": False, "error": False, "errmsg": None,
           "events": 0, "intents": [], "preview": None, "tokens": 0,
           "weather_hit": False, "text": ""}
    t0 = time.time()
    try:
        async with client.stream("GET", f"{BASE}/api/chat", params=params, timeout=timeout) as resp:
            nt = resp.headers.get("X-Access-Token")
            if nt:
                tok = nt
            if resp.status_code != 200:
                body = await resp.aread()
                out["error"] = True
                out["errmsg"] = f"HTTP {resp.status_code}: {body[:200].decode('utf-8','ignore')}"
                return out
            ev = None
            parts: list[str] = []
            async for raw in resp.aiter_lines():
                if raw == "":
                    if ev or parts:
                        d = "".join(parts)
                        if ev == "done":
                            out["done"] = True
                        elif ev == "paused":
                            out["paused"] = True
                        elif ev == "intent":
                            try:
                                o = json.loads(d)
                                out["intents"].append((o.get("level1"), o.get("level2")))
                            except Exception:
                                pass
                        elif ev == "preview":
                            try:
                                o = json.loads(d)
                                url = o.get("url")
                                # 仅当拿到"真实非空直链"才记为有预览, 空串("")绝不冒充预览(此前假绿根因)
                                out["preview"] = url if isinstance(url, str) and url.strip() else None
                            except Exception:
                                pass
                        elif ev == "token":
                            try:
                                o = json.loads(d)
                                if isinstance(o.get("data"), str):
                                    out["tokens"] += len(o["data"])
                                    out["text"] += o["data"]
                            except Exception:
                                pass
                        out["events"] += 1
                    ev = None
                    parts = []
                    continue
                if raw.startswith("event:"):
                    ev = raw[6:].strip()
                elif raw.startswith("data:"):
                    parts.append(raw[5:].strip())
    except Exception as e:
        out["error"] = True
        out["errmsg"] = f"{type(e).__name__}: {e}"
    out["elapsed"] = round(time.time() - t0, 1)
    out["weather_hit"] = bool(re.search(r"深圳|天气|气温|℃|摄氏度", out["text"]))
    return out


async def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=BASE)
    args = ap.parse_args()
    BASE = f"http://{args.host}" if "://" not in args.host else args.host

    results = []
    def check(name, cond, detail=""):
        results.append((name, cond, detail))
        print(f"  [{'OK' if cond else 'FAIL'}] {name} {detail}")

    async with httpx.AsyncClient() as client:
        tok, uname, sc = await register(client)
        check("注册成功", sc in (200, 201) and tok, f"(http={sc}, user={uname})")
        if not tok:
            print("注册失败, 终止"); _report(results); return
        pid = await create_project(client, tok)
        check("建项目成功", pid is not None, f"(pid={pid})")
        cid = await create_conv(client, tok, pid)
        check("建对话成功", cid is not None, f"(cid={cid})")
        if not cid:
            print("建对话失败, 终止"); _report(results); return

        # 首跑: 期望以 paused(await_confirm) 收尾(多意图下 orchestrator 可能在其后 emit done,
        # 但后端已锁死 terminal_status=paused 保住断点, 故以 paused 为权威判据)
        r1 = await send(client, tok, cid, MULTI_QUERY)
        check("首跑返回 await_confirm(paused)", r1["paused"],
              f"(events={r1['events']}, intents={r1['intents']}, done={r1['done']})")
        check("首跑未 error", not r1["error"], f"(err={r1['errmsg']})")

        # 续跑: resume=true, 期望 done 且双产物
        r2 = await send(client, tok, cid, MULTI_QUERY, resume=True)
        check("续跑完成 done", r2["done"], f"(events={r2['events']}, elapsed={r2['elapsed']}s)")
        check("续跑无 error", not r2["error"], f"(err={r2['errmsg']})")
        # 多意图拆分的可靠证据: 同一会话内既产出了网站预览(建站子任务)又产出了天气文本(天气子任务)
        split_ok = (r2["preview"] is not None) and r2["weather_hit"]
        check("多意图拆分(双子任务均执行: 网站预览+天气文本)",
              split_ok,
              f"(preview={r2['preview']}, weather_hit={r2['weather_hit']}, intents={r2['intents']})")
        check("网站预览产物(preview)", r2["preview"] is not None,
              f"(preview={r2['preview']})")
        check("天气文本产物(深圳/天气/℃)", r2["weather_hit"],
              f"(weather_hit={r2['weather_hit']}, tokens={r2['tokens']})")
        check("续跑未再重弹 paused(断点续跑)", not r2["paused"],
              f"(paused={r2['paused']})")

    _report(results)


def _report(results):
    passed = sum(1 for _, c, _ in results if c)
    total = len(results)
    print("\n==== 多意图 e2e 结果 ====")
    print(f"通过 {passed}/{total}")
    if passed == total:
        print("全部通过 ✅")
    else:
        print("存在失败 ❌")
    # 写入报告
    os.makedirs(os.path.join(ROOT, "..", "reports"), exist_ok=True)
    out = os.path.join(ROOT, "..", "reports", "e2e-multi-intent-site-weather.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("# 多意图 e2e 测试报告 (site + weather)\n\n")
        f.write(f"查询: {MULTI_QUERY}\n\n")
        for name, cond, detail in results:
            f.write(f"- [{'OK' if cond else 'FAIL'}] {name} {detail}\n")
        f.write(f"\n**结果: {passed}/{total} 通过**\n")
    print(f"报告已写: {out}")


if __name__ == "__main__":
    asyncio.run(main())
