"""SeedAI 集成冒烟测试（精简 10 条，唯一入口）。

设计原则（固化「测一条改一条 / 边测边改」工作流，见 2026-07-29 复盘）：
  0. 账号固定可复现 + 测试文档必须记录账号密码（用户拿去前端登录复查）。
  1. 每次跑前必须 `FORCE=1 python scripts/reset_all.py` 重置（schema/意图改动后库需重建），
     重置脚本自动建超管 `huzhen / huzhen189`，本脚本直接用该账号登录。
  2. 跑一条看一条：任意 case 失败（error 或 非 done）立即中断并打印已通过明细，
     便于「测一条改一条」——定位问题改代码（重启 7101）后重跑，不盲目跑完。
  3. 用例覆盖从 0→1 建站 + 单意图→多意图；闲聊仅 1 条（多意图场景日后可顺带带闲聊）。
  4. 自动产出测试报告(reports/test-<ts>.md)，顶部固定写「测试账号」段。

用法:
  python scripts/run_tests.py            # 跑全部 10 条（默认）
  python scripts/run_tests.py --no-stop  # 失败不中断（默认失败即停）
  python scripts/run_tests.py --csv      # 额外导出 CSV

前置:
  - 单进程后端 7101 已启动(业务 + AI 核心合并)。
  - 已重置数据（见上）。默认模型 deepseek（与系统默认对齐，qwen 太慢）。
  - 本地已去掉 hosts 代理，直接用 localhost 访问。
"""

import asyncio
import csv
import json
import os
import re
import sys
import time

import httpx

# ── 配置 ──────────────────────────────────────────
# 本地已去掉 hosts 代理，直接用 localhost。
BASE = os.environ.get("TEST_HOST", "http://localhost:7101")
MODEL = os.environ.get("TEST_MODEL", "deepseek")  # #554 默认 deepseek；qwen 太慢不推荐
USER = os.environ.get("TEST_USER", "huzhen")
PASS = os.environ.get("TEST_PASS", "huzhen189")   # 固定可复现（重置脚本自动建）
TOKEN = None
CONV_ID = None
PROJ_ID = None

# ── 精简 10 条回归：1 闲聊 + 9 建站(从0→1, 单意图→多意图) ──
# 覆盖: 需求采集 → 单意图生成 → 带规格单意图 → 缺槽 clarify(验证 [4.6] 闸门)
#        → 明确单意图 → 修改 → 多意图(2) → 多意图(2) → 多意图(3)
#   要加 case：直接往下面这个列表追加即可（标签随意，仅用于报告分组）。
TEST_CASES = [
    # 1) 闲聊（仅 1 条）
    ("闲聊", "你好"),

    # 2) 单意图·需求采集（应路由 PM，走需求阶段，不直接生成）
    ("需求", "我想做一个摄影作品集网站，用来展示我的旅行照片"),

    # 3) 单意图·建站生成（基于需求直接生成，单意图 route）
    ("建站", "就按上面的需求直接帮我生成一个完整的摄影作品集网站吧"),

    # 4) 单意图·带完整规格（页面/风格齐全，应能直接生成）
    ("建站", "做一个包含首页、作品集、关于我三个页面的个人摄影网站，白色背景简洁风格，导航栏固定顶部"),

    # 5) 单意图·缺槽（只说做企业官网，缺风格/页面 → 应触发 [4.6] pending 闸降级 clarify）
    ("建站", "帮我做一个企业官网"),

    # 6) 单意图·明确建站（单文件 HTML，深色+蓝调，能直接产出）
    ("建站", "生成一个单文件 HTML 落地页，深色主题配蓝色强调色，包含 Hero 和三个特性卡片"),

    # 7) 单意图·修改（build_modify，改导航为响应式汉堡菜单）
    ("建站", "把我网站的导航栏改成响应式汉堡菜单，手机端折叠"),

    # 8) 多意图·双意图（建站首页 + 文案，应 split）
    ("多意图", "帮我做一个官网首页，顺便写一段介绍我们公司的文案"),

    # 9) 多意图·双意图（建站产品页 + 联网搜索趋势，应 split）
    ("多意图", "帮我生成产品详情页，并联网查一下今年最新的网页配色趋势用进去"),

    # 10) 多意图·三意图（建站博客 + 写文章 + 配图，应 split）
    ("多意图", "帮我做一个技术博客站，写两篇关于前端性能优化的文章，再为每篇配一张示意图"),
]


async def login(client: httpx.AsyncClient) -> str | None:
    """登录并返回 access_token（固定账号 huzhen/huzhen189）。"""
    r = await client.post(f"{BASE}/auth/login", json={"account": USER, "password": PASS})
    if r.status_code != 200:
        return None
    m = re.search(r"access_token=([^;]+)", r.headers.get("set-cookie", ""))
    return m.group(1) if m else None


async def create_conv(client: httpx.AsyncClient, headers: dict) -> tuple[int | None, int | None]:
    """创建项目 + 对话，返回 (project_id, conversation_id)。"""
    pr = await client.post(f"{BASE}/api/projects", headers=headers,
                           json={"name": "集成冒烟项目", "description": "10条精简回归"})
    pid = pr.json().get("id") if pr.status_code in (200, 201) else None
    cr = await client.post(f"{BASE}/api/conversations", headers=headers,
                           json={"title": "集成冒烟对话", "project_id": pid})
    cid = cr.json().get("id") if cr.status_code in (200, 201) else None
    return pid, cid


async def send_chat(client: httpx.AsyncClient, headers: dict,
                    conv_id: int, text: str, timeout: int = 300) -> dict:
    """发送一轮对话(单进程 /api/chat 网关, GET + SSE),返回结构化结果。

    多意图 split 走 Orchestrator 子任务 DAG，可能耗时较长，timeout 默认 300s。
    """
    t0 = time.time()
    result = {"done": False, "tokens": 0, "events": 0, "qc": False,
              "refined": False, "error": False, "elapsed": 0.0,
              "decision": None, "intent": None}
    trace_id = f"test-{int(t0*1000)%1000000}"
    params = {"model": MODEL, "conversation_id": conv_id, "q": text, "trace_id": trace_id}
    try:
        async with client.stream("GET", f"{BASE}/api/chat", params=params,
                                 headers={"Cookie": headers.get("Cookie", ""),
                                          "Accept": "text/event-stream"},
                                 timeout=timeout) as resp:
            if resp.status_code != 200:
                result["error"] = True
                result["elapsed"] = time.time() - t0
                return result
            current_event = None
            data_parts = []
            async for line in resp.aiter_lines():
                if line == "":
                    if current_event or data_parts:
                        data = "".join(data_parts)
                        obj = {}
                        if data:
                            try:
                                obj = json.loads(data)
                            except json.JSONDecodeError:
                                obj = {}
                        if current_event == "done":
                            result["done"] = True
                        elif current_event == "qc":
                            result["qc"] = True
                        elif current_event == "refined":
                            result["refined"] = True
                        elif current_event == "error":
                            result["error"] = True
                        elif current_event == "intent" and isinstance(obj, dict):
                            result["intent"] = f"{obj.get('level1')}/{obj.get('level2')}"
                            result["decision"] = obj.get("decision")
                        elif current_event == "token" and isinstance(obj.get("data"), str):
                            result["tokens"] += len(obj["data"])
                        result["events"] += 1
                    current_event = None
                    data_parts = []
                elif line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: "):
                    data_parts.append(line[6:])
    except Exception:
        result["error"] = True
    result["elapsed"] = round(time.time() - t0, 1)
    return result


def judge(cat: str, r: dict) -> bool:
    """判定单条是否通过：闲聊/建站/多意图都要求 done 且非 error。

    - clarify 决策也会发 done（见 queue.py clarify 分支），故不会误判失败。
    - 多意图 split 由 Orchestrator 合并后发 done。
    """
    if not r["done"]:
        return False
    if r["error"]:
        return False
    return True


async def main():
    global TOKEN, CONV_ID, PROJ_ID
    stop_on_fail = "--no-stop" not in sys.argv
    csv_out = "--csv" in sys.argv
    cases = TEST_CASES

    print("=" * 64)
    print(f"SeedAI 集成冒烟 ({len(cases)} 条) | 目标 {BASE} | 模型 {MODEL} | 用户 {USER}")
    print("=" * 64)
    print("⚠ 前置: 已 `FORCE=1 python scripts/reset_all.py` 重置 + 起重 7101（无 hosts 代理，用 localhost）")
    print(f"⚠ 失败策略: {'失败即停(便于测一条改一条)' if stop_on_fail else '跑完不中断'}")

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)
    ) as client:
        # 1. 登录
        TOKEN = await login(client)
        if not TOKEN:
            print("❌ 登录失败(账号应为重置脚本创建的 huzhen/huzhen189), 退出")
            return
        hdrs = {"Cookie": f"access_token={TOKEN}"}
        print("✅ 登录成功")

        # 2. 创建项目 + 对话
        PROJ_ID, CONV_ID = await create_conv(client, hdrs)
        if not CONV_ID:
            print("❌ 创建对话失败, 退出")
            return
        print(f"✅ 项目={PROJ_ID} 对话={CONV_ID}\n")

        # 3. 逐条执行（跑一条看一条）
        stats = {"total": len(cases), "pass": 0, "done_sum": 0,
                 "qc_sum": 0, "refined_sum": 0, "total_time": 0.0}
        rows = []
        stopped = False
        for i, (cat, text) in enumerate(cases, 1):
            r = await send_chat(client, hdrs, CONV_ID, text)
            ok = judge(cat, r)
            if ok:
                stats["pass"] += 1
            if r["done"]:
                stats["done_sum"] += 1
            if r["qc"]:
                stats["qc_sum"] += 1
            if r["refined"]:
                stats["refined_sum"] += 1
            stats["total_time"] += r["elapsed"]

            status = "✅" if ok else "❌"
            detail = (f"intent={r['intent']} decision={r['decision']} "
                      f"ev={r['events']} tok={r['tokens']} qc={r['qc']} "
                      f"ref={r['refined']} {r['elapsed']}s")
            print(f"  [{i:02d}] {status} [{cat}] {text[:28]:28s} | {detail}")
            rows.append((i, cat, text[:28], ok, r))

            if not ok and stop_on_fail:
                print(f"\n⛔ 第 {i} 条失败，按「测一条改一条」策略中断。"
                      f"先改代码 → 重启 7101 → 重跑本脚本。")
                stopped = True
                break

        if stopped:
            passed_so_far = stats["pass"]
            print(f"\n  已通过 {passed_so_far}/{i}（中断于第 {i} 条）")
        else:
            rate = stats["pass"] / stats["total"] * 100
            print(f"\n{'='*64}")
            print(f"通过率: {rate:.1f}% ({stats['pass']}/{stats['total']})")
            print(f"总耗时: {stats['total_time']:.0f}s | done={stats['done_sum']} "
                  f"qc={stats['qc_sum']} refined={stats['refined_sum']}")
            print(f"{'='*64}")

        # 4. 生成测试报告（顶部固定写测试账号）
        ts = time.strftime("%Y%m%d-%H%M%S")
        report_path = f"reports/test-{ts}.md"
        os.makedirs("reports", exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("# SeedAI 集成冒烟测试报告\n\n")
            f.write(f"> 时间: {time.strftime('%Y-%m-%d %H:%M:%S')} | 目标: {BASE} | 模型: {MODEL}\n")
            f.write(f"> 账号: **{USER} / {PASS}**（重置脚本自动创建，可登录前端复查）\n\n")
            f.write("## 〇、测试账号（登录复查用）\n\n")
            f.write(f"- 后端: `{BASE}`（单进程，业务+AI 核心合并）\n")
            f.write(f"- 账号 / 密码: `{USER}` / `{PASS}`（role=super_admin）\n")
            f.write(f"- 前端复查: `http://localhost:7100` → 用上述账号登录 → 看「项目管理/对话」\n\n")

            f.write("## 一、总览\n\n")
            f.write("| 指标 | 值 |\n|---|---|\n")
            f.write(f"| 通过率 | **{stats['pass']}/{stats['total']}** "
                    f"({'中断' if stopped else '跑完'}) |\n")
            f.write(f"| done 事件 | {stats['done_sum']} 条 |\n")
            f.write(f"| QC 触发 | {stats['qc_sum']} 次 |\n")
            f.write(f"| L2 精炼 | {stats['refined_sum']} 次 |\n")
            f.write(f"| 总耗时 | {stats['total_time']:.0f}s |\n\n")

            f.write("## 二、逐条明细\n\n")
            f.write("| # | 类别 | 输入 | 意图 | 决策 | done | error | 事件 | 耗时 | 结果 |\n")
            f.write("|---|---|---|---|---|---|---|---|---|---|\n")
            for i, cat, text, ok, r in rows:
                f.write(f"| {i} | {cat} | {text} | {r['intent']} | {r['decision']} | "
                        f"{r['done']} | {r['error']} | {r['events']} | {r['elapsed']}s | "
                        f"{'✅' if ok else '❌'} |\n")

            if stopped:
                f.write(f"\n> ⛔ 中断于第 {i} 条（失败即停策略）。重跑前先修代码并重启 7101。\n")

            f.write("\n> 后端日志见 `backend/app/logs/app.log`\n")

        print(f"\n📄 报告: {report_path}")

        # 5. CSV 可选
        if csv_out:
            csv_path = f"reports/test-{ts}.csv"
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["idx", "category", "input", "passed", "done",
                            "qc", "refined", "events", "elapsed"])
                for i, cat, txt, ok, r in rows:
                    w.writerow([i, cat, txt, ok, r["done"], r["qc"],
                                r["refined"], r["events"], r["elapsed"]])
            print(f"📊 CSV: {csv_path}")

        # 失败即停时退出码非 0，便于 CI/脚本感知
        if stopped or stats["pass"] != stats["total"]:
            sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
