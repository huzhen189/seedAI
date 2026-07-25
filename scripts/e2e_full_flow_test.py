"""SeedAI 端到端全流程测试(从注册新账号到建站全流程)。

用法:
  python scripts/e2e_full_flow_test.py [--limit N] [--offset N] [--host 127.0.0.1:7101]

特点:
  - 注册全新测试账号(用户名 e2e_<ts>), 从 Set-Cookie 提取 access_token, 经 ?token= 透传
    (规避本地 127.0.0.1 与 .env 中 cookie_domain=huzhen.net.cn 不匹配问题)
  - 创建项目 + 对话, 通过业务端 GET /api/chat(SSE) 逐条发送 29 条测试语句
  - 真实链路: 前端 → 业务 /api/chat → 意图识别(混合级联 v1.2.0) → AI 核心 /generate → SSE 透传
  - 自动处理 await_confirm 暂停: 检测到 paused 后立即发 resume=true 续跑建站
  - 每条结果实时写 logs/e2e_progress.jsonl, 结束生成 reports/e2e-<ts>.md 测试报告

潜在修复点(边测边改): 若发现意图误判 / 生成失败 / SSE 异常 / 落库问题, 记录并在测试后修复。
"""

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("TEST_HOST", "http://127.0.0.1:7101")
TOKEN: str | None = None
PID: int | None = None
CID: int | None = None
UNAME: str | None = None
HD = {}  # 备用 header(实际用 ?token=)

PROGRESS = ROOT / "logs" / "e2e_progress.jsonl"

# ── 30 条端到端测试语句(真实用户旅程: 闲聊→需求→建站→迭代→收尾)──
TEST_CASES = [
    ("闲聊", "你好"),
    ("闲聊", "你是谁"),
    ("闲聊", "你能帮我做什么"),
    ("需求", "我想做一个网站"),
    ("需求", "帮我做一个个人摄影作品集网站"),
    ("需求", "风格要简洁大方，白色背景为主"),
    ("需求", "网站标题叫「光影集」"),
    ("需求", "首页要有一句Slogan和一个大图Banner"),
    ("需求", "导航栏固定在上方，滚动时不动"),
    ("需求", "作品展示用网格布局，3列"),
    ("需求", "关于我页面要有我的简介和联系方式"),
    ("需求", "手机端也要能正常看"),
    ("需求", "可以加上一个暗色模式切换吗"),
    ("需求", "配色用深灰色和蓝色作为点缀"),
    ("需求", "加载速度要快"),
    ("需求", "我要放我的摄影作品，大概20张照片"),
    ("需求", "作品集每个卡片有标题、分类标签和查看按钮"),
    ("需求", "加上回到顶部按钮"),
    ("需求", "整体设计再精致一点"),
    ("建站触发", "开始生成网站吧"),
    ("建站", "生成首页的HTML"),
    ("建站", "导航栏要响应式的，手机端变成汉堡菜单"),
    ("建站", "加上hover效果，鼠标悬停图片时放大"),
    ("建站", "关于我页面要有头像占位、个人简介、社交链接"),
    ("建站", "footer要有版权信息和社交媒体图标"),
    ("建站", "配色方案: 主色#2c3e50, 背景#f5f6fa, 强调#3498db"),
    ("修改", "整体再做一次UI优化，让设计更精致"),
    ("修改", "修复一下，导航栏的汉堡菜单在手机端点了没反应"),
    ("收尾", "很好，生成最终版本"),
]


async def register(client: httpx.AsyncClient):
    global TOKEN, UNAME
    UNAME = f"e2e_{int(time.time())}"
    r = await client.post(
        f"{BASE}/auth/register",
        json={"username": UNAME, "password": "Test123456", "nickname": "E2E测试"},
    )
    sc = r.headers.get("set-cookie", "")
    m = re.search(r"access_token=([^;]+)", sc)
    TOKEN = m.group(1) if m else None
    return r.status_code, bool(TOKEN), UNAME


async def create_project(client: httpx.AsyncClient):
    global PID
    r = await client.post(
        f"{BASE}/api/projects",
        json={"name": "E2E全流程测试项目", "description": "自动化端到端"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    PID = r.json().get("id") if r.status_code in (200, 201) else None
    return r.status_code, PID


async def create_conv(client: httpx.AsyncClient):
    global CID
    r = await client.post(
        f"{BASE}/api/conversations",
        json={"title": "E2E对话", "project_id": PID},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    CID = r.json().get("id") if r.status_code in (200, 201) else None
    return r.status_code, CID


def _parse_sse(line_iter):
    """简易 SSE 解析: 逐个事件 yield (event, data_str)。"""
    pass


async def send_chat(client: httpx.AsyncClient, text: str, resume: bool = False,
                    timeout: float = 240) -> dict:
    global TOKEN  # 滑动续期会就地重赋值全局 TOKEN, 须在引用前声明
    params = {"model": "deepseek", "conversation_id": CID,
              "q": text, "token": TOKEN}
    if resume:
        params["resume"] = "true"
    t0 = time.time()
    res = {"done": False, "error": False, "paused": False, "events": 0,
           "tokens": 0, "intents": [], "qc": False, "refined": False,
           "preview": None, "elapsed": 0.0, "errmsg": None, "last": "",
           "resumed": resume}
    try:
        async with client.stream("GET", f"{BASE}/api/chat", params=params,
                                  timeout=timeout) as resp:
            # 滑动续期: 抓取服务端回传的 X-Access-Token, 轮换全局 TOKEN,
            # 保证长会话(全流程 29 条)不会因 token 过期而断线。
            nt = resp.headers.get("X-Access-Token")
            if nt:
                TOKEN = nt
            if resp.status_code != 200:
                body = await resp.aread()
                res["error"] = True
                res["errmsg"] = f"HTTP {resp.status_code}: {body[:200].decode('utf-8','ignore')}"
                res["elapsed"] = time.time() - t0
                return res
            event = None
            data_parts: list[str] = []
            async for raw in resp.aiter_lines():
                if raw == "":
                    if event or data_parts:
                        data = "".join(data_parts)
                        if event == "done":
                            res["done"] = True
                        elif event == "error":
                            res["error"] = True
                            try:
                                o = json.loads(data)
                                res["errmsg"] = o.get("message", data)
                            except Exception:
                                res["errmsg"] = data
                        elif event == "paused":
                            res["paused"] = True
                        elif event == "intent":
                            try:
                                o = json.loads(data)
                                res["intents"].append((o.get("level1"), o.get("level2")))
                            except Exception:
                                pass
                        elif event == "qc":
                            res["qc"] = True
                        elif event == "refined":
                            res["refined"] = True
                        elif event == "preview":
                            try:
                                o = json.loads(data)
                                res["preview"] = o.get("url")
                            except Exception:
                                pass
                        elif event == "token":
                            try:
                                o = json.loads(data)
                                if isinstance(o.get("data"), str):
                                    res["tokens"] += len(o["data"])
                            except Exception:
                                pass
                        res["events"] += 1
                        res["last"] = data[:120]
                    event = None
                    data_parts = []
                    continue
                if raw.startswith("event:"):
                    event = raw[6:].strip()
                elif raw.startswith("data:"):
                    data_parts.append(raw[5:].strip())
    except Exception as e:  # httpx 超时 / 连接异常
        res["error"] = True
        res["errmsg"] = f"{type(e).__name__}: {e}"
    res["elapsed"] = round(time.time() - t0, 1)
    return res


async def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=len(TEST_CASES))
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--host", default=BASE)
    args = ap.parse_args()
    BASE = args.host

    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    if PROGRESS.exists():
        PROGRESS.unlink()  # 新跑清空进度

    cases = TEST_CASES[args.offset: args.offset + args.limit]
    print("=" * 64)
    print(f"SeedAI 端到端全流程测试 (账号维度, {len(cases)} 条)")
    print(f"目标: {BASE}")
    print("=" * 64)

    rows = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=10, pool=20)) as client:
        # 1) 注册新账号
        st, ok, uname = await register(client)
        print(f"[注册] {uname} HTTP={st} token={'OK' if ok else 'FAIL'}")
        if not ok:
            print("❌ 注册/取 token 失败, 退出"); return
        # 2) 项目 + 对话
        pst, pid = await create_project(client)
        cst, cid = await create_conv(client)
        print(f"[项目] HTTP={pst} pid={pid} | [对话] HTTP={cst} cid={cid}")
        if not cid:
            print("❌ 创建对话失败, 退出"); return

        # 3) 逐条测试
        for i, (cat, text) in enumerate(cases, 1):
            r = await send_chat(client, text)
            # 自动续跑 await_confirm 暂停
            if r["paused"]:
                rr = await send_chat(client, "确认并开始生成网站", resume=True)
                # 把 resume 结果合并进本行
                r["done"] = r["done"] or rr["done"]
                r["error"] = r["error"] or rr["error"]
                r["preview"] = r["preview"] or rr["preview"]
                r["qc"] = r["qc"] or rr["qc"]
                r["refined"] = r["refined"] or rr["refined"]
                r["events"] += rr["events"]
                r["tokens"] += rr["tokens"]
                r["elapsed"] += rr["elapsed"]
                r["resumed_ok"] = rr["done"] and not rr["error"]
                if rr["errmsg"]:
                    r["errmsg"] = (r["errmsg"] or "") + f" | resume: {rr['errmsg']}"
                print(f"    ↳ 检测到 paused(await_confirm), 已自动 resume: done={rr['done']} err={rr['error']} preview={'有' if rr['preview'] else '无'}")

            # 判定
            if cat == "边界":
                ok = not r["error"]
            else:
                ok = r["done"] and not r["error"]
            intent_s = ",".join(f"{a}/{b}" for a, b in r["intents"]) or "-"
            status = "✅" if ok else "❌"
            print(f"  [{i:02d}] {status} [{cat}] {text[:28]:28s} | done={r['done']} err={r['error']} "
                  f"ev={r['events']} tok={r['tokens']} qc={r['qc']} ref={r['refined']} "
                  f"prev={'Y' if r['preview'] else 'N'} intent=[{intent_s}] {r['elapsed']}s"
                  + (f" ERR={r['errmsg']}" if r['errmsg'] else ""))
            row = {"idx": i, "cat": cat, "text": text, "ok": ok, "res": r}
            rows.append(row)
            # 实时写进度(便于边测边看)
            with PROGRESS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 4) 汇总
    total = len(rows)
    passed = sum(1 for x in rows if x["ok"])
    done_sum = sum(1 for x in rows if x["res"]["done"])
    err_sum = sum(1 for x in rows if x["res"]["error"])
    qc_sum = sum(1 for x in rows if x["res"]["qc"])
    ref_sum = sum(1 for x in rows if x["res"]["refined"])
    prev_sum = sum(1 for x in rows if x["res"]["preview"])
    total_t = sum(x["res"]["elapsed"] for x in rows)
    print(f"\n{'='*64}")
    print(f"通过率: {passed/total*100:.1f}% ({passed}/{total})")
    print(f"done={done_sum} error={err_sum} qc={qc_sum} refined={ref_sum} preview={prev_sum} 总耗时={total_t:.0f}s")

    # 5) 写报告
    ts = time.strftime("%Y%m%d-%H%M%S")
    rep = ROOT / "reports" / f"e2e-{ts}.md"
    rep.parent.mkdir(parents=True, exist_ok=True)
    L = []
    L.append("# SeedAI 端到端全流程测试报告\n")
    L.append(f"> 测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')} | 目标: {BASE}")
    L.append(f"> 账号: `{UNAME}`(全新注册) | 项目={PID} 对话={CID}\n")
    L.append("## 总览\n")
    L.append("| 指标 | 值 |\n|---|---|")
    L.append(f"| 通过率 | **{passed/total*100:.1f}%** ({passed}/{total}) |")
    L.append(f"| done 事件 | {done_sum} |")
    L.append(f"| error 事件 | {err_sum} |")
    L.append(f"| QC 触发 | {qc_sum} |")
    L.append(f"| L2 精炼 | {ref_sum} |")
    L.append(f"| 预览产出 | {prev_sum} |")
    L.append(f"| 总耗时 | {total_t:.0f}s |")
    L.append(f"| 平均耗时 | {total_t/max(total,1):.1f}s/条 |\n")
    L.append("## 详细结果\n")
    L.append("| # | 类别 | 输入(前28字) | 结果 | done | err | ev | tok | qc | ref | prev | intent | 耗时 | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for x in rows:
        r = x["res"]
        intent_s = ",".join(f"{a}/{b}" for a, b in r["intents"]) or "-"
        note = ""
        if r.get("paused"):
            note = "paused→auto-resume " + ("OK" if r.get("resumed_ok") else "FAIL")
        if r["errmsg"]:
            note = (note + " | " if note else "") + str(r["errmsg"])[:60]
        L.append(f"| {x['idx']} | {x['cat']} | {x['text'][:28]} | {'✅' if x['ok'] else '❌'} | "
                 f"{r['done']} | {r['error']} | {r['events']} | {r['tokens']} | {r['qc']} | {r['refined']} | "
                 f"{'Y' if r['preview'] else 'N'} | {intent_s} | {r['elapsed']}s | {note} |")
    rep.write_text("\n".join(L), encoding="utf-8")
    print(f"\n📄 报告: {rep}")
    print(f"📄 进度: {PROGRESS}")


if __name__ == "__main__":
    asyncio.run(main())
