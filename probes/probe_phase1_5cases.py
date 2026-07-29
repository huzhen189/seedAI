"""Phase 1 验证探针: 5 条用例验证「工具调用/思考」事件从 SSE 透出。

目标: 确认 Phase 1 新增的 reasoning / tool_call / tool_result 三类事件,
能经 /api/chat SSE 实时流出并被前端(或本探针)解析到。

原理(已核对源码):
- backend/app/agent/skills/agent_chat.py:explain_skill 对**非空** user_query
  无条件触发 emit_reasoning + emit_tool_call("web_search") + emit_tool_result("web_search"),
  事件经 ToolEventBus(线程局部作用域, sub_task_id 隔离)废料道经 /api/chat 透出。
- 故这 5 条「非空 chat 类」用例每条都应命中 web_search 三件套。
- 后端 SSE 帧格式(sse-starlette + to_sse):
    event: <类型>
    data: <JSON 字符串>

用法:
  python probes/probe_phase1_5cases.py
前置: 单进程后端 7101 已启动且重置过数据。
"""

import asyncio
import json
import os
import re
import sys
import time

import httpx

BASE = os.environ.get("TEST_HOST", "http://127.0.0.1:7101")
MODEL = os.environ.get("TEST_MODEL", "qwen")
USER = os.environ.get("TEST_USER", "huzhen")
PASS = os.environ.get("TEST_PASS", "huzhen189")

# 5 条探针用例: 偏 agent_chat 路由(解释/问答/对比/实时搜索), 这些是 explain_skill
# 埋 web_search 三事件的路径。每条用例独立建对话, 避免跨轮意图污染导致路由错乱。
# 用例5 故意走"设计咨询"对照(agent_design 不一定埋点), 如实记录。
CASES = [
    ("闲聊", "你好, 介绍一下你自己"),
    ("实时搜索", "2026年最新的网页设计趋势是什么, 请联网帮我查一下"),
    ("技术问答", "解释一下什么是 HTML 语义化标签, 为什么它对 SEO 重要"),
    ("对比问答", "对比一下 CSS Grid 和 Flexbox 布局各自的适用场景, 哪个更适合做响应式导航"),
    ("对照-设计", "帮我搜集一下现在流行的深色模式配色方案, 做成一个设计参考"),
]


async def login(client: httpx.AsyncClient) -> str | None:
    r = await client.post(f"{BASE}/auth/login", json={"account": USER, "password": PASS})
    if r.status_code != 200:
        print(f"  ❌ 登录失败 status={r.status_code} body={r.text[:200]}")
        return None
    m = re.search(r"access_token=([^;]+)", r.headers.get("set-cookie", ""))
    return m.group(1) if m else None


async def create_conv(client: httpx.AsyncClient, headers: dict) -> tuple[int | None, int | None]:
    pr = await client.post(f"{BASE}/api/projects", headers=headers,
                           json={"name": "Phase1探针项目", "description": "工具事件透出验证"})
    pid = pr.json().get("id") if pr.status_code in (200, 201) else None
    cr = await client.post(f"{BASE}/api/conversations", headers=headers,
                           json={"title": "Phase1探针对话", "project_id": pid})
    cid = cr.json().get("id") if cr.status_code in (200, 201) else None
    return pid, cid


async def send_chat(client: httpx.AsyncClient, headers: dict,
                    conv_id: int, text: str, timeout: int = 180) -> dict:
    """发一轮对话, 解析 SSE, 专门统计 Phase 1 三事件。"""
    t0 = time.time()
    res = {
        "done": False, "error": False, "elapsed": 0.0,
        "reasoning": [], "tool_calls": [], "tool_results": [],
        "other_events": 0,
    }
    trace_id = f"probe-{int(t0*1000)%1000000}"
    params = {"model": MODEL, "conversation_id": conv_id, "q": text, "trace_id": trace_id}

    try:
        async with client.stream("GET", f"{BASE}/api/chat", params=params,
                                 headers={"Cookie": headers.get("Cookie", ""),
                                          "Accept": "text/event-stream"},
                                 timeout=timeout) as resp:
            if resp.status_code != 200:
                res["error"] = True
                res["elapsed"] = time.time() - t0
                res["err_msg"] = f"status={resp.status_code} body={resp.text[:200]}"
                return res

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
                            res["done"] = True
                        elif current_event == "error":
                            res["error"] = True
                        elif current_event == "reasoning":
                            # 透传后 data 即顶层内容: {"text": "..."}
                            res["reasoning"].append(obj.get("text", ""))
                        elif current_event == "tool_call":
                            # 透传后 data 即顶层: {"tool_call_id","name","args"}
                            res["tool_calls"].append({"name": obj.get("name"),
                                                      "tool_call_id": obj.get("tool_call_id"),
                                                      "args": obj.get("args")})
                        elif current_event == "tool_result":
                            res["tool_results"].append({"name": obj.get("name"),
                                                        "ok": obj.get("ok"),
                                                        "summary": obj.get("summary")})
                        else:
                            res["other_events"] += 1
                    current_event = None
                    data_parts = []
                elif line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: "):
                    data_parts.append(line[6:])
    except Exception as e:
        res["error"] = True
        res["err_msg"] = f"exception: {e}"
    res["elapsed"] = round(time.time() - t0, 1)
    return res


async def main() -> None:
    print("=" * 64)
    print(f"Phase 1 工具事件透出验证 (5 条用例) | 目标 {BASE} | 用户 {USER}")
    print("=" * 64)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)
    ) as client:
        token = await login(client)
        if not token:
            print("❌ 登录失败, 退出"); sys.exit(1)
        hdrs = {"Cookie": f"access_token={token}"}
        print("✅ 登录成功")

        # 每条用例独立建项目+对话, 避免跨轮意图污染(casual→design 错乱)
        proj_id, conv_id = await create_conv(client, hdrs)
        if not conv_id:
            print("❌ 创建对话失败, 退出"); sys.exit(1)
        print(f"✅ 基线项目={proj_id} 对话={conv_id} (每条用例独立新建对话)\n")

        total_ok = 0          # 硬性通过(埋点 skill: agent_chat 三件套齐)
        ctrl_rows = []        # 对照用例(agent_design 等未埋点路径, 不计入)
        rows = []
        for i, (cat, text) in enumerate(CASES, 1):
            # 每条独立对话(同一项目下), 干净触发各自意图
            _pid, cid = await create_conv(client, hdrs)
            if not cid:
                print(f"  [{i}] ❌ 创建对话失败, 跳过"); continue
            r = await send_chat(client, hdrs, cid, text)
            # 验证标准: 透出 reasoning≥1 且 tool_call≥1 且 tool_result≥1 且 done 无error
            has_all = (len(r["reasoning"]) >= 1
                       and len(r["tool_calls"]) >= 1
                       and len(r["tool_results"]) >= 1
                       and r["done"] and not r["error"])
            is_ctrl = cat.startswith("对照")
            if has_all and not is_ctrl:
                total_ok += 1
            # 对照用例单独统计
            if is_ctrl:
                ctrl_rows.append((i, cat, text, r, has_all))
            rows.append((i, cat, text, r, has_all, is_ctrl))
            mark = "✅" if has_all else ("对照" if is_ctrl else "❌")
            tc_names = ",".join(t["name"] for t in r["tool_calls"])
            print(f"  [{i}] {mark} [{cat}] {text[:26]:26s}")
            print(f"       reasoning={len(r['reasoning'])} tool_call={len(r['tool_calls'])}"
                  f"({tc_names}) tool_result={len(r['tool_results'])} done={r['done']}"
                  f" err={r['error']} {r['elapsed']}s")
            if not has_all and r.get("err_msg"):
                print(f"       err={r['err_msg'][:160]}")

        # 硬性用例 = 非对照
        hard_total = sum(1 for x in rows if not x[5])
        print("\n" + "=" * 64)
        print(f"Phase 1 透出(埋点路径)通过率: {total_ok}/{hard_total}")
        print("=" * 64)

        # 样本明细
        for i, cat, text, r, _, _ in rows:
            print(f"\n── 用例 [{i}] [{cat}] {text[:40]}")
            for j, t in enumerate(r["reasoning"]):
                print(f"   reasoning[{j}]: {t[:120]}")
            for j, t in enumerate(r["tool_calls"]):
                print(f"   tool_call[{j}]: name={t['name']} id={t['tool_call_id']} args={t['args']}")
            for j, t in enumerate(r["tool_results"]):
                print(f"   tool_result[{j}]: name={t['name']} ok={t['ok']} summary={t['summary'][:100]}")

        # 写报告
        ts = time.strftime("%Y%m%d-%H%M%S")
        os.makedirs("reports", exist_ok=True)
        rep = f"reports/probe-phase1-{ts}.md"
        with open(rep, "w", encoding="utf-8") as f:
            f.write("# Phase 1 工具事件透出验证报告\n\n")
            f.write(f"> 时间: {time.strftime('%Y-%m-%d %H:%M:%S')} | 目标: {BASE} | 用户: {USER}\n")
            f.write(f"> 账号: {USER}/{PASS} | 每条用例独立对话避免跨轮污染\n\n")
            f.write("## 结论\n\n")
            f.write(f"- 埋点路径(agent_chat 等含 web_search 三件套)透出通过率: "
                    f"**{total_ok}/{hard_total}**\n")
            f.write("- 对照用例(agent_design 未埋 web_search)如实记录, 不计入通过率。\n")
            if total_ok == hard_total and hard_total > 0:
                f.write("- **Phase 1 工具调用可见化已在线上确认**: reasoning/tool_call/tool_result "
                        "三类事件均经 SSE 实时透出, WorkBuddy 式 think→call→observe 循环生效。\n\n")
            else:
                f.write("- ⚠ 部分埋点路径未透出三件套, 需排查 skill 是否接入 ToolEventBus。\n\n")

            f.write("## 明细\n\n")
            f.write("| # | 类别 | 输入 | reasoning | tool_call | tool_result | done | error | 耗时 | 类型 |\n")
            f.write("|---|---|---|---|---|---|---|---|---|---|\n")
            for i, cat, text, r, _, is_ctrl in rows:
                typ = "对照" if is_ctrl else "埋点"
                f.write(f"| {i} | {cat} | {text[:22]} | {len(r['reasoning'])} | "
                        f"{len(r['tool_calls'])} | {len(r['tool_results'])} | {r['done']} | "
                        f"{r['error']} | {r['elapsed']}s | {typ} |\n")
            f.write("\n## 事件样本\n\n")
            for i, cat, text, r, _, _ in rows:
                f.write(f"### 用例[{i}] {cat}: {text[:40]}\n")
                for t in r["reasoning"]:
                    f.write(f"- reasoning: {t}\n")
                for t in r["tool_calls"]:
                    f.write(f"- tool_call: `{t['name']}` id={t['tool_call_id']} args={t['args']}\n")
                for t in r["tool_results"]:
                    f.write(f"- tool_result: `{t['name']}` ok={t['ok']} summary={t['summary']}\n")
                f.write("\n")
            f.write("\n> 后端日志见 `backend/app/logs/app.log`\n")

        print(f"\n📄 报告: {rep}")
        # 探针退出码: 埋点路径全过才算通过(对照项不计入)
        if total_ok != hard_total:
            sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
