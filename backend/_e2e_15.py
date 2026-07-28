"""端到端回归 harness（15 条语句 + 取消/续跑交互场景，不进版本控制）。

覆盖维度: 意图分析准确性 / 路由准确性 / 执行计划拆分合理性 / 运行状态完善度
         / 取消级联 + cancel_summary / 断点续跑(G5) / 未处理异常 / 流程阻塞。

实现: 纯标准库(http.client 流式 SSE + urllib POST)，不依赖第三方库，
      可在任意有完整 stdlib 的 python 上运行(默认 managed python 3.13 即可)。

用法:
  python _e2e_15.py                 # 跑 15 条 + 交互场景, 自动跳过已完成
  python _e2e_15.py --reset         # 清空增量结果/进度重跑
  python _e2e_15.py --only 5,13,16  # 仅跑指定 id
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import uuid
from http.cookiejar import CookieJar
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor

# 全局 cookie jar + 带 cookie 处理的 opener(urlopen 不会自动回写 Set-Cookie)
CJ = CookieJar()
OPENER = build_opener(HTTPCookieProcessor(CJ))

BASE = "http://127.0.0.1:7101"
HERE = os.path.dirname(os.path.abspath(__file__))
RESULT_FILE = os.path.join(HERE, "_e2e_15_results.jsonl")
PROGRESS_FILE = os.path.join(HERE, "_e2e_15_progress.json")

MODEL = "deepseek"
TMP_PW = "testpass123"

# ---- 15 条语句: 由简到难, 覆盖 chat/build/design/requirement/review 单意图 + 多意图编排 + 删除拦截 ----
# role_hint: 我对每条预期命中的 4 角色 SOP 角色(product/design/dev/qa/null)
STATEMENTS = [
    {"id": 1, "text": "你好", "expect": "闲聊→agent_chat，短回复，done", "role_hint": "null"},
    {"id": 2, "text": "用通俗的话解释一下什么是闭包", "expect": "学习/解释→agent_chat，done", "role_hint": "null"},
    {"id": 3, "text": "把『Hello World』翻译成中文", "expect": "翻译→agent_chat/doc，输出中文，done", "role_hint": "null"},
    {"id": 4, "text": "对比一下 React 和 Vue 的优缺点", "expect": "技术对比→agent_chat，done", "role_hint": "null"},
    {"id": 5, "text": "帮我做一个个人博客网站", "expect": "单意图建站→agent_build/agent_generate_site(dev)，plan_preview+SOP，done", "role_hint": "dev"},
    {"id": 6, "text": "帮我设计一个简洁的登录页面", "expect": "单意图设计→chat_design/agent_design(design 角色)，done", "role_hint": "design"},
    {"id": 7, "text": "帮我把刚才的博客网站改成深色主题", "expect": "迭代修改→build_modify(dev)，可能 clarify/复用历史，done", "role_hint": "dev"},
    {"id": 8, "text": "帮我写一份产品需求文档，关于一个待办事项应用", "expect": "强信号→build_requirement(product 角色，PRD)，done", "role_hint": "product"},
    {"id": 9, "text": "我想做一个电商网站，要有商品列表页和购物车功能", "expect": "建站带需求→agent_generate_site(dev)，plan+建站，done", "role_hint": "dev"},
    {"id": 10, "text": "帮我做一个猜数字的小游戏", "expect": "单意图游戏→build_game(dev)，done", "role_hint": "dev"},
    {"id": 11, "text": "检查这段代码有没有问题：def add(a,b): return a+b", "expect": "代码评审→build_review(qa 角色)，输出评审，done", "role_hint": "qa"},
    {"id": 12, "text": "这段 Python 报错 TypeError: list index out of range，帮我修：x=[1,2]; print(x[5])", "expect": "修 bug→build_fix(qa 角色)，done", "role_hint": "qa"},
    {"id": 13, "text": "帮我生成一个公司官网，并写一篇关于我们公司的介绍文章", "expect": "双意图(建站+文档)→多意图门控命中『并』→orchestration≥2 子任务，merge，done", "role_hint": "dev"},
    {"id": 14, "text": "给我做一个待办网站，再帮我写使用说明文档，顺便搜索一下同类产品", "expect": "三意图(建站+文档+搜索)→orchestration 3 子任务，done", "role_hint": "dev"},
    {"id": 15, "text": "删除我的项目", "expect": "危险操作→block/拒绝删项目(安全拦截)，不执行", "role_hint": "null"},
]

# 交互场景(非 15 条矩阵内, 单独计数): 取消级联 / 断点续跑(G5)
INTERACTIVE = [
    {"id": 16, "kind": "cancel", "text": "帮我做一个完整的在线教育平台，要有课程列表、详情页、购物车和支付", "expect": "运行中发送 /cancel → 收 cancel_summary，子任务 cancelled/skipped，无静默断开"},
    {"id": 17, "kind": "resume", "text": "帮我做一个个人作品集网站", "expect": "命中 await_confirm 暂停 → resume=true 重发应真正重跑(非仅回放)"},
]


def log(*a):
    print("[harness]", *a, flush=True)


# ---------------- 进度持久化 ----------------
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            return json.load(open(PROGRESS_FILE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_progress(p):
    json.dump(p, open(PROGRESS_FILE, "w", encoding="utf-8"))


# ---------------- HTTP 基础 ----------------
def http_post(cj, path, obj):
    req = Request(BASE + path, data=json.dumps(obj).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    return _open(cj, req)


def http_get_json(cj, path):
    req = Request(BASE + path, headers={}, method="GET")
    return _open(cj, req)


def _open(cj, req):
    cj.add_cookie_header(req)
    resp = OPENER.open(req, timeout=300)
    body = resp.read().decode("utf-8", "replace")
    try:
        return resp.status, json.loads(body)
    except Exception:
        return resp.status, body


# ---------------- SSE 流式读取(http.client) ----------------
def stream_chat(cj, path, trace_id, max_seconds=240):
    """用 http.client 流式读取 /api/chat 的 SSE。返回 (events, terminal, err_code, timed_out)。"""
    p = http.client.HTTPConnection("127.0.0.1", 7101, timeout=max_seconds + 10)
    headers = {"Accept": "text/event-stream"}
    # 手动注入 cookie
    cookie = "; ".join(f"{c.name}={c.value}" for c in cj)
    if cookie:
        headers["Cookie"] = cookie
    p.request("GET", path, headers=headers)
    resp = p.getresponse()
    if resp.status >= 400:
        raw = resp.read().decode("utf-8", "replace")
        return [], f"HTTP_{resp.status}", None, False, raw
    events = []
    terminal = None
    err_code = None
    timed_out = False
    buf = ""
    start = time.time()
    while True:
        if time.time() - start > max_seconds:
            timed_out = True
            terminal = "timeout"
            break
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
        if line.startswith("event:"):
            buf = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
            ev_type = buf or "message"
            try:
                payload = json.loads(data)
            except Exception:
                payload = data
            events.append({"event": ev_type, "data": payload})
            buf = ""
            if ev_type in ("done", "error", "aborted", "unsupported", "retry",
                           "clarify", "confirm", "block", "cancel_summary"):
                terminal = ev_type
                if ev_type == "error" and isinstance(payload, dict):
                    err_code = payload.get("code")
                if ev_type in ("done", "error", "aborted", "unsupported", "retry",
                               "clarify", "confirm", "block"):
                    break
        elif line == "":
            # 空行: SSE 事件结束(本解析逐行, 无需特别处理)
            pass
    p.close()
    return events, terminal, err_code, timed_out, None


# ---------------- 信号提取 ----------------
def detect_signals(events):
    skills = set()
    subtask_statuses = {}
    has_orchestration = False
    interaction = None
    routed_skill = None
    intent_level = None
    has_cancel_summary = False
    has_plan_preview = False
    stages = []
    for e in events:
        t, d = e["event"], e["data"]
        if t == "orchestration" and isinstance(d, dict):
            has_orchestration = True
            for tk in d.get("tasks", []) or []:
                if isinstance(tk, dict) and tk.get("skill"):
                    skills.add(tk["skill"])
        if t == "subtask_start" and isinstance(d, dict):
            if d.get("skill"):
                skills.add(d["skill"])
            if d.get("id"):
                subtask_statuses[d["id"]] = "running"
        if t in ("subtask_done", "subtask_fail", "subtask_skip") and isinstance(d, dict):
            if d.get("id"):
                subtask_statuses[d["id"]] = t.split("_")[1]
        if t == "intent" and isinstance(d, dict):
            sk = d.get("selected_skill") or d.get("skill")
            if sk:
                routed_skill = sk
            l1 = d.get("level1") or d.get("intent")
            l2 = d.get("level2")
            if l1:
                intent_level = f"{l1}/{l2}" if l2 else l1
        if t in ("clarify", "confirm", "block") and interaction is None:
            interaction = t
        if t == "cancel_summary":
            has_cancel_summary = True
        if t == "plan_preview":
            has_plan_preview = True
        if t == "node" and isinstance(d, dict) and d.get("stage"):
            stages.append(d["stage"])
    if routed_skill:
        skills.add(routed_skill)
    return {
        "skills": sorted(skills),
        "routed_skill": routed_skill,
        "intent_level": intent_level,
        "has_orchestration": has_orchestration,
        "subtask_count": len(subtask_statuses),
        "subtask_statuses": subtask_statuses,
        "interaction": interaction,
        "has_cancel_summary": has_cancel_summary,
        "has_plan_preview": has_plan_preview,
        "stages_sample": stages[:10],
    }


def record(st, sig, terminal, err_code, note):
    row = {
        "id": st["id"], "kind": st.get("kind", "stmt"), "text": st["text"],
        "expect": st["expect"], "role_hint": st.get("role_hint"),
        "terminal": terminal, "err_code": err_code,
        "signals": sig, "note": note, "ts": time.strftime("%H:%M:%S"),
    }
    with open(RESULT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


# ---------------- 单条语句执行 ----------------
def run_statement(cj, project_id, st, trace_base):
    sid = st["id"]
    # 每条新建会话隔离意图
    _, cjr = http_post(cj, "/api/conversations",
                       {"project_id": project_id, "name": f"stmt{sid}"})
    if not isinstance(cjr, dict) or "id" not in cjr:
        return record(st, None, "HTTP_CONV_FAIL", None, str(cjr)[:200])
    cid = cjr["id"]
    trace_id = f"{trace_base}-{sid}"
    url = (f"/api/chat?model={MODEL}&conversation_id={cid}&trace_id={trace_id}"
           f"&q={_q(st['text'])}")
    log(f"\n=== #{sid}: {st['text'][:36]}")
    events, terminal, err_code, timed_out, raw = stream_chat(cj, url, trace_id)
    sig = detect_signals(events)
    if timed_out:
        terminal = "timeout"
    # 自动确认: 若遇到 confirm/await_confirm 门, 二次重发 confirmed=1 让其续跑
    if terminal in ("confirm", "block") and isinstance(raw, type(None)):
        # block 一般不需续跑(安全拦截); confirm 需 confirmed=1
        pass
    if terminal == "confirm":
        log("  检测到 confirm 门, 二次重发 confirmed=1 续跑")
        url2 = url + "&confirmed=1"
        events2, terminal2, err_code2, _, _ = stream_chat(cj, url2, trace_id)
        events += events2
        terminal = terminal2
        err_code = err_code2
        sig = detect_signals(events)
    log(f"  终止={terminal} err={err_code} skills={sig['skills']} "
        f"orch={sig['has_orchestration']} subtasks={sig['subtask_count']} "
        f"plan_preview={sig['has_plan_preview']}")
    # bug 判定
    bug = None
    if terminal is None:
        bug = "SSE 静默断开(无终止事件)"
    elif terminal == "error" and err_code not in ("UNSUPPORTED",):
        bug = f"error code={err_code}"
    elif terminal == "timeout":
        bug = "SSE 超时无终止"
    return record(st, sig, terminal, err_code, bug)


# ---------------- 取消场景(级联 + cancel_summary) ----------------
def run_cancel_scenario(cj, project_id, sc, trace_base):
    sid = sc["id"]
    _, cjr = http_post(cj, "/api/conversations",
                       {"project_id": project_id, "name": f"cancel{sid}"})
    if not isinstance(cjr, dict) or "id" not in cjr:
        return record(sc, None, "HTTP_CONV_FAIL", None, str(cjr)[:200])
    cid = cjr["id"]
    trace_id = f"{trace_base}-{sid}"
    url = (f"/api/chat?model={MODEL}&conversation_id={cid}&trace_id={trace_id}"
           f"&q={_q(sc['text'])}")
    log(f"\n=== #{sid} [取消场景]: {sc['text'][:36]}")
    # 起流, 读到一个 running 子任务或 plan 后再取消
    p = http.client.HTTPConnection("127.0.0.1", 7101, timeout=260)
    cookie = "; ".join(f"{c.name}={c.value}" for c in cj)
    p.request("GET", url, headers={"Accept": "text/event-stream", "Cookie": cookie})
    resp = p.getresponse()
    events = []
    buf = ""
    cancelled_sent = False
    seen_running = False
    start = time.time()
    while True:
        if time.time() - start > 200:
            break
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("event:"):
            buf = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
            ev_type = buf or "message"
            try:
                payload = json.loads(data)
            except Exception:
                payload = data
            events.append({"event": ev_type, "data": payload})
            buf = ""
            if ev_type == "subtask_start":
                seen_running = True
            if ev_type == "done":
                break
            # 见到运行态即发取消(验证协作式中断 + cancel_summary)
            if seen_running and not cancelled_sent:
                cancelled_sent = True
                _, cr = http_post(cj, "/cancel", {"trace_id": trace_id})
                log(f"  >> 发送 /cancel trace={trace_id} -> {cr}")
    p.close()
    # 继续读完剩余流, 捕获 cancel_summary
    # (上面循环已读完本连接; cancel_summary 可能已在后续事件中)
    sig = detect_signals(events)
    log(f"  终止态含 cancel_summary={sig['has_cancel_summary']} subtasks={sig['subtask_statuses']}")
    note = None
    if not sig["has_cancel_summary"]:
        note = "未收到 cancel_summary(级联取消可能未生效)"
    return record(sc, sig, "cancel_scenario", None, note)


# ---------------- 断点续跑场景(G5) ----------------
def run_resume_scenario(cj, project_id, sc, trace_base):
    sid = sc["id"]
    _, cjr = http_post(cj, "/api/conversations",
                       {"project_id": project_id, "name": f"resume{sid}"})
    if not isinstance(cjr, dict) or "id" not in cjr:
        return record(sc, None, "HTTP_CONV_FAIL", None, str(cjr)[:200])
    cid = cjr["id"]
    trace_id = f"{trace_base}-{sid}"
    url = (f"/api/chat?model={MODEL}&conversation_id={cid}&trace_id={trace_id}"
           f"&q={_q(sc['text'])}")
    log(f"\n=== #{sid} [续跑场景]: {sc['text'][:36]}")
    # 第一轮: 读到 await_confirm/confirm 即停
    events1, term1, _, _, _ = stream_chat(cj, url, trace_id)
    sig1 = detect_signals(events1)
    saw_gate = term1 in ("confirm", "block") or any(
        e["event"] in ("confirm", "block") for e in events1)
    log(f"  第一轮终止={term1} gate={saw_gate}")
    if not saw_gate:
        return record(sc, sig1, "resume_no_gate", None,
                      "未命中 await_confirm, 无法验证 G5 续跑")
    # 第二轮: resume=true 重发, 应真正重跑(再次出现 plan_preview/subtask_start)
    url2 = url + "&resume=true&confirmed=1"
    events2, term2, _, _, _ = stream_chat(cj, url2, trace_id)
    sig2 = detect_signals(events2)
    reran = sig2["has_plan_preview"] or sig2["subtask_count"] > 0
    log(f"  第二轮终止={term2} 重跑信号(re-plan/subtask)={reran}")
    note = None if reran else "resume=true 后未见重跑信号(可能仅回放, G5 未生效)"
    return record(sc, sig2, "resume_scenario", None, note)


def _q(text):
    import urllib.parse
    return urllib.parse.quote(text)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    if args.reset:
        for f in (PROGRESS_FILE, RESULT_FILE):
            if os.path.exists(f):
                os.remove(f)
    progress = load_progress()

    cj = CJ  # 使用全局带 cookie 处理的 jar
    uname = f"e2e15_{uuid.uuid4().hex[:8]}"
    log("注册用户", uname)
    _, rr = http_post(cj, "/auth/register",
                      {"account": uname, "password": TMP_PW,
                       "nickname": "e2e15", "email": f"{uname}@test.com"})
    if not isinstance(rr, dict) or rr.get("id") is None:
        log("!! 注册失败", rr)
        return
    log("  注册 OK")
    _, pr = http_post(cj, "/api/projects", {"name": f"e2e15_{uname}"})
    if not isinstance(pr, dict) or "id" not in pr:
        log("!! 建项目失败", pr)
        return
    project_id = pr["id"]
    log("  项目 ID =", project_id)
    trace_base = f"e2e15-{uuid.uuid4().hex[:6]}"

    only = set()
    if args.only:
        only = {int(x) for x in args.only.split(",") if x.strip()}

    for st in STATEMENTS:
        if only and st["id"] not in only:
            continue
        if str(st["id"]) in progress:
            log(f"-- 跳过已完成 #{st['id']}")
            continue
        row = run_statement(cj, project_id, st, trace_base)
        if row["terminal"] not in ("done", "unsupported", "clarify", "confirm", "block"):
            log(f"  ⚠️ #{st['id']} 终态异常={row['terminal']}, 进度已记录, 可修复后续跑")
        else:
            progress[str(st["id"])] = True
            save_progress(progress)

    for sc in INTERACTIVE:
        if only and sc["id"] not in only:
            continue
        if str(sc["id"]) in progress:
            log(f"-- 跳过已完成 #{sc['id']}")
            continue
        if sc["kind"] == "cancel":
            run_cancel_scenario(cj, project_id, sc, trace_base)
        elif sc["kind"] == "resume":
            run_resume_scenario(cj, project_id, sc, trace_base)
        progress[str(sc["id"])] = True
        save_progress(progress)

    log("\n=== 一轮结束, 结果见", RESULT_FILE)


if __name__ == "__main__":
    main()
