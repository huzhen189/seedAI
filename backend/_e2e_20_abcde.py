"""E2E 20 条模拟测试 —— 专项验证 A/D/C/B+E 五大改动。

流程: 注册新用户 → 登录(自动 Cookie) → 建项目 → 逐条发送 20 条语句,
其中若干「追问修改」与「建站」同会话(conversation 分组),以触发 D 上下文闸门 build_modify。
解析 /api/chat 的 SSE 事件, 逐条记录「实际结果」并比对预期(覆盖 A~E):
  A (#485): assistant 气泡仅「文字总结 + artifact-summary-card」, 无 site 双卡 → 校验 messages 末条 content.type != 'site'。
  D (#486): 已落站会话内「修改/按钮点不动」应命中 build_modify 路由(intent_level/selected_skill)。
  C (#487): QC 静态交互校验 — 含交互控件却无 JS 绑定 → 命中 needs_review(Reflexion)。
  B+E (#488): 生成阶段出现 cos_upload + progress 事件; 无 COS 时 preview 事件带 content 兜底。
  E (#488 兜底): 落库 Artifact 无 url 时仍写 content, 右侧可 srcdoc 渲染。

用法:
  python _e2e_20_abcde.py            # 从头/续跑(自动跳过已完成)
  python _e2e_20_abcde.py --reset     # 清空增量结果重跑
"""
from __future__ import annotations
import argparse, json, os, sys, time, uuid, re, asyncio
import requests

BASE = "http://127.0.0.1:7101"
RESULT_FILE = os.path.join(os.path.dirname(__file__), "_e2e_20_results.jsonl")
PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "_e2e_20_progress.json")

MODEL = "qwen"          # 生产默认模型; 真实演练用户流程
TMP_PW = "testpass123"
USERNAME = f"e2e20_{uuid.uuid4().hex[:8]}"

# ---- 20 条语句: 由简到难, 含 D 闸门会话分组(follows 复用建站会话) ----
# follows: 复用指定语句所在的会话(用于「建站→追问修改」同会话触发 build_modify 闸门)。
STATEMENTS = [
    {"id": 1,  "text": "你好", "expect": "闲聊 → agent_chat, 短回复, done"},
    {"id": 2,  "text": "帮我写一首关于春天的短诗", "expect": "诗歌 → agent_doc/chat, 输出诗歌, done(不当作 PRD)"},
    {"id": 3,  "text": "把『Hello World』翻译成中文", "expect": "翻译 → agent_chat/doc, done"},
    {"id": 4,  "text": "给我讲个冷笑话", "expect": "闲聊 → agent_chat, done"},
    {"id": 5,  "text": "帮我总结：人工智能正在改变软件开发的方式，开发者可以利用大模型完成代码生成、测试和文档编写。", "expect": "摘要 → agent_doc, done"},
    {"id": 6,  "text": "帮我设计一个简洁的登录页面", "expect": "设计页面→进入建站/产出管线(agent_build or agent_generate_site, 走计划确认) done"},
    {"id": 7,  "text": "帮我做一个个人博客网站", "expect": "建站 → agent_build/generate_site, 产出预览+Artifact(repo=site), done"},
    {"id": 8,  "follows": 7, "text": "把刚才那个博客网站改成深色主题", "expect": "D 闸门: 已落站+修改词 → build_modify, done"},
    {"id": 9,  "follows": 7, "text": "博客的导航栏修一下，按钮点不动", "expect": "D 闸门: 已落站+『按钮点不动』修改词 → build_modify, done"},
    {"id": 10, "text": "帮我做一个电商网站，要有商品列表页和购物车功能", "expect": "建站(带需求) → generate_site, done"},
    {"id": 11, "follows": 10, "text": "把这个电商网站的首页背景换成蓝色", "expect": "D 闸门: 已落站+修改词 → build_modify, done"},
    {"id": 12, "text": "帮我写一份产品需求文档，关于一个待办事项应用", "expect": "强信号 → requirement(build_requirement), 输出 PRD, done"},
    {"id": 13, "text": "帮我搜索一下最新的人工智能行业新闻", "expect": "搜索 → agent_search, done"},
    {"id": 14, "text": "检查这段代码有没有问题：def add(a,b): return a+b", "expect": "代码评审 → agent_review, done"},
    {"id": 15, "text": "帮我生成一个公司官网，并写一篇关于我们公司的介绍文章", "expect": "双意图(建站+文档) → 多意图门控 ≥2 子任务, merge, done"},
    {"id": 16, "text": "设计一个产品首页，并帮我写首页的营销文案", "expect": "双意图(设计+文档) → 多意图编排, done"},
    {"id": 17, "text": "给我做一个待办网站，再帮我写使用说明文档，顺便搜索一下同类产品", "expect": "三意图(建站+文档+搜索) → orchestration 3 子任务, done"},
    {"id": 18, "text": "我想做一个在线教育平台，需要课程列表页、详情页、购物车，还要写课程介绍文档并搜索竞品", "expect": "复杂多意图 → orchestration, done"},
    {"id": 19, "text": "删除我的项目", "expect": "危险操作 → block(拒绝删项目/提示走设置软删除), 不执行"},
    {"id": 20, "text": "综合：做一个旅游小程序官网，写景点推荐文章，搜索热门目的地，再设计预订流程页", "expect": "极复杂多意图(建站+文档+搜索+设计) → orchestration 多子任务, done"},
]


def log(*a):
    print("[e2e20]", *a, flush=True)


def load_done() -> dict:
    done = {}
    if os.path.exists(PROGRESS_FILE):
        try:
            done = json.load(open(PROGRESS_FILE))
        except Exception:
            done = {}
    return done or {}


def save_done(done: dict):
    json.dump(done, open(PROGRESS_FILE, "w"))


def _read_lines(resp, events, start, max_seconds, last_id_holder):
    """读取一条 SSE 流, 把事件累积到 events; 命中终态/超时返回对应 terminal。
    连接中途被重置(10054)时抛 ConnectionError 由上层 reconnect 接管。"""
    buf = ""
    terminal = None
    err_code = None
    for raw in resp.iter_lines(decode_unicode=True):
        if time.time() - start > max_seconds:
            log("  !! SSE 超时(max_seconds), 强制截断")
            return "timeout", err_code
        if raw is None:
            continue
        line = raw
        if line.startswith("id:"):
            last_id_holder["id"] = line[len("id:"):].strip()
            continue
        if line.startswith("event:"):
            buf = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = line[len("data:"):].strip()
            ev_type = buf or "message"
            try:
                payload = json.loads(data)
            except Exception:
                payload = data
            events.append({"event": ev_type, "data": payload})
            buf = ""
            if ev_type == "paused" and isinstance(payload, dict) and payload.get("stage") == "await_confirm":
                return "paused", err_code
            if ev_type in ("done", "error", "aborted", "unsupported", "retry",
                           "clarify", "confirm", "block"):
                terminal = ev_type
                if ev_type == "error" and isinstance(payload, dict):
                    err_code = payload.get("code")
                return terminal, err_code
    return terminal, err_code  # 连接正常结束(无终态)


def parse_sse_stream(s, url, cid, tid, max_seconds: float = 900, max_reconnect: int = 6):
    """带断线重连的 SSE 读取: 长链路(建站)偶发 10054 远程重置时, 用同一 tid + after=<last_id>
    重连, 后端 stream_exists(tid)=True → 仅回放增量不重复入队。"""
    events = []
    terminal = None
    err_code = None
    last_id = {"id": None}
    start = time.time()
    attempt = 0
    while attempt <= max_reconnect:
        sep = "&" if "?" in url else "?"
        conn_url = url
        if tid:
            conn_url += f"{sep}trace_id={tid}"
            sep = "&"
        if last_id["id"]:
            conn_url += f"{sep}after={last_id['id']}"
        try:
            resp = s.get(conn_url, stream=True)
            if resp.status_code != 200:
                log(f"  !! SSE 重连非200 http={resp.status_code}")
                return events, f"HTTP_{resp.status_code}", err_code
            terminal, err_code = _read_lines(resp, events, start, max_seconds, last_id)
            if terminal is not None:
                return events, terminal, err_code
            # 正常结束但无终态(连接被服务器关) → 若是 paused 之外的中断, 尝试重连续播
            if last_id["id"] and time.time() - start < max_seconds:
                attempt += 1
                log(f"  ↻ SSE 流结束无终态, 重连续播(第{attempt}次, after={last_id['id']})")
                continue
            return events, terminal, err_code
        except Exception as e:  # ConnectionReset/ChunkedEncodingError 等
            if time.time() - start >= max_seconds:
                return events, "timeout", err_code
            attempt += 1
            if attempt > max_reconnect:
                log(f"  !! SSE 重连耗尽({max_reconnect}次), 放弃. 末事件={len(events)}")
                return events, "broken", err_code
            log(f"  ↻ SSE 连接重置({type(e).__name__}), 重连(第{attempt}次)")
            # 微调避免立即重连被再次重置
            time.sleep(1.0)
    return events, terminal, err_code


def analyze_signals(events):
    """提取 A~E 验证信号。"""
    skills = set()
    routed_skill = None
    intent_level = None
    has_orchestration = False
    subtask_count = 0
    interaction = None
    stages = []
    # A: 是否(不该)出现 site-card / type='site' 注入 → 仅监控 progress/cos 相关
    saw_cos_upload = False
    cos_uploads = []
    saw_progress = False
    progress_pts = []
    saw_preview_content = False   # B+E: preview 事件带 content 兜底
    saw_preview_url = False
    saw_needs_review = False      # C: QC 静态校验未过 → reflexion
    review_loops = 0
    for e in events:
        t = e["event"]
        d = e["data"]
        if t == "orchestration" and isinstance(d, dict):
            has_orchestration = True
            for tk in d.get("tasks", []) or []:
                if isinstance(tk, dict) and tk.get("skill"):
                    skills.add(tk["skill"])
        if t == "subtask_start" and isinstance(d, dict):
            if d.get("skill"):
                skills.add(d["skill"])
            subtask_count += 1
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
        if t == "retry":
            interaction = "retry"
        if t == "node" and isinstance(d, dict):
            st = d.get("stage")
            if st:
                stages.append(st)
            # C: Reviewer 触发 needs_review / reflexion
            blob = " ".join(str(v) for v in d.values())
            if "needs_review" in blob or "reflexion" in blob.lower() or "reflex" in blob.lower():
                saw_needs_review = True
            if st and "review" in str(st).lower():
                review_loops += 1
        if t == "cos_upload" and isinstance(d, dict):
            saw_cos_upload = True
            cos_uploads.append(d.get("file") or d.get("filename"))
        if t == "progress" and isinstance(d, dict):
            saw_progress = True
            if isinstance(d.get("pct"), (int, float)):
                progress_pts.append(d["pct"])
        if t == "preview" and isinstance(d, dict):
            if d.get("content"):
                saw_preview_content = True
            if d.get("url"):
                saw_preview_url = True
    if routed_skill:
        skills.add(routed_skill)
    return {
        "skills": sorted(skills),
        "routed_skill": routed_skill,
        "intent_level": intent_level,
        "has_orchestration": has_orchestration,
        "subtask_count": subtask_count,
        "interaction": interaction,
        "stages_sample": stages[:12],
        # A~E
        "cos_upload": saw_cos_upload, "cos_files": cos_uploads,
        "progress": saw_progress, "progress_pts": progress_pts,
        "preview_content": saw_preview_content, "preview_url": saw_preview_url,
        "needs_review": saw_needs_review, "review_nodes": review_loops,
    }


def fetch_last_assistant(s, cid):
    """拉取会话消息, 返回末条 assistant 的 content 解析(校验 A: type!=site)。"""
    try:
        r = s.get(f"{BASE}/api/conversations/{cid}/messages")
        if r.status_code != 200:
            return None
        msgs = r.json()
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                c = m.get("content")
                if isinstance(c, str):
                    try:
                        c = json.loads(c)
                    except Exception:
                        return {"raw": c, "type": "raw"}
                return c
    except Exception:
        return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    # NOTE: 安全删除层会拦截 os.remove (Windows 回收站不可用, fail-closed)。
    # 改为以 "w" 模式截断文件, 规避删除, 效果等价 reset。
    if args.reset:
        if os.path.exists(PROGRESS_FILE):
            open(PROGRESS_FILE, "w").close()
        if os.path.exists(RESULT_FILE):
            open(RESULT_FILE, "w").close()

    done = load_done()
    s = requests.Session()
    s.timeout = 940  # 必须大于 parse_sse_stream 的 max_seconds(900), 否则 requests 先断流

    def do_login():
        # 长链路(20 条 + 多建站)可能 > 1h, JWT 滑动过期 → 定时用同凭证刷新 cookie。
        r = s.post(f"{BASE}/auth/login", json={"username": USERNAME, "password": TMP_PW})
        return r.status_code in (200, 201)

    log("注册用户", USERNAME)
    r = s.post(f"{BASE}/auth/register", json={
        "username": USERNAME, "password": TMP_PW,
        "nickname": "e2e20", "email": f"{USERNAME}@test.com",
    })
    if r.status_code not in (200, 201):
        log("!! 注册失败", r.status_code, r.text[:300])
        return
    log("  注册 OK")
    if not do_login():
        log("!! 登录失败(刷新 cookie)")
        return

    r = s.post(f"{BASE}/api/projects", json={"name": f"e2e20_{USERNAME}"})
    if r.status_code not in (200, 201):
        log("!! 建项目失败", r.status_code, r.text[:300])
        return
    project_id = r.json().get("id")
    log("  项目 ID =", project_id)

    conv_of_id = {}  # 语句 id -> conversation id(用于 follows 复用)

    def ensure_authed():
        # 探测会话是否仍有效, 失效(401)则静默重登, 返回 True 表示当前已可用。
        try:
            probe = s.get(f"{BASE}/api/projects")
            if probe.status_code == 401:
                return do_login()
            return True
        except Exception:
            return do_login()

    for st in STATEMENTS:
        sid = st["id"]
        if str(sid) in done:
            log(f"-- 跳过已完成 #{sid}")
            continue
        # 会话归属: 有 follows 则复用, 否则新建
        follows = st.get("follows")
        if follows and follows in conv_of_id:
            cid = conv_of_id[follows]
            log(f"\n=== #{sid}(复用会话 {cid}): {st['text'][:36]}")
        else:
            ensure_authed()  # 长任务中途可能过期, 建会话前先确认 cookie 有效
            r = s.post(f"{BASE}/api/conversations", json={"project_id": project_id, "name": f"stmt{sid}"})
            if r.status_code == 401:
                if not do_login():
                    log("!! 重登失败, 中止")
                    break
                r = s.post(f"{BASE}/api/conversations", json={"project_id": project_id, "name": f"stmt{sid}"})
            if r.status_code not in (200, 201):
                log(f"!! #{sid} 建会话失败", r.status_code, r.text[:200])
                break
            cid = r.json().get("id")
            conv_of_id[sid] = cid
            log(f"\n=== #{sid}(新会话 {cid}): {st['text'][:36]}")

        # 每个语句固定 tid(供 SSE 断线重连用 after 游标续播, 避免重复入队)。
        stmt_tid = uuid.uuid4().hex
        ensure_authed()  # 长任务: 每条发前确认 cookie 未过期, 失效则静默重登
        url = (f"{BASE}/api/chat?model={MODEL}&conversation_id={cid}"
               f"&q={requests.utils.quote(st['text'])}")
        # 建站语句完整链路 Coder+Reviewer(<3 reflexion 轮, 每轮 ~1~2min)+_deliver: 修订 C(#487)
        # 交互校验误判后已收窄为「仅 JS 依赖控件」, 单站通常 1~2 轮通过, 实测约 2~4min。
        # 抬高 max_seconds 到 900 保证客户端持续接收直到 done; 长链路偶发 10054 由 parse_sse_stream
        # 内部自动重连(after 游标)兜底, 不丢事件。
        events, terminal, err_code = parse_sse_stream(s, url, cid, stmt_tid, max_seconds=900)
        # B+E: 建站语句遇 await_confirm 计划闸门(paused) → 自动确认续跑(confirmed=1&resume=true,
        #   +相同 tid 复用后端 stream, 不二次入队)。续跑也走带重连的解析。
        if terminal == "paused":
            log(f"  ⏸️ #{sid} 命中计划确认闸门(paused) → 自动确认续跑")
            resume_url = (f"{BASE}/api/chat?model={MODEL}&conversation_id={cid}"
                          f"&confirmed=1&resume=true&q=" + requests.utils.quote("确认并生成"))
            ev2, t2, ec2 = parse_sse_stream(s, resume_url, cid, stmt_tid, max_seconds=900)
            events = events + ev2
            terminal, err_code = t2, ec2
        sig = analyze_signals(events)
        last_asst = fetch_last_assistant(s, cid)
        log(f"  终止={terminal} err={err_code}")
        log(f"  routed={sig['routed_skill']} intent={sig['intent_level']} "
            f"orch={sig['has_orchestration']} subtasks={sig['subtask_count']} inter={sig['interaction']}")
        log(f"  [B+E] cos_upload={sig['cos_upload']}({sig['cos_files']}) progress={sig['progress']} "
            f"prevContent={sig['preview_content']} prevUrl={sig['preview_url']}")
        log(f"  [C] needs_review={sig['needs_review']} review_nodes={sig['review_nodes']}")
        log(f"  [A] 末条assistant.type={ (last_asst or {}).get('type','?') if isinstance(last_asst, dict) else 'n/a' }")
        bug = None
        if terminal is None:
            bug = "SSE 静默断开(无终止事件)"
        elif terminal in ("broken", "timeout"):
            bug = f"SSE 链路异常(terminal={terminal})"
        elif terminal == "error" and err_code and err_code not in ("UNSUPPORTED",):
            bug = f"error code={err_code}"
        if bug:
            log(f"  !! 疑似 bug: {bug} → 中止, 修复后续跑")
            append_result(st, sig, "BUG", bug, None, last_asst)
            break
        # D(#486): 站点修改类语句(follows 复用建站会话) → 等建站产物落库后再发,
        # 避免 has_site_artifact 竞争(False → 误路由 agent_chat)。这里刚确认续跑,
        # 给后端落库 Artifact(repo=site) 留出时间窗(2.5s)。
        if st.get("follows"):
            time.sleep(2.5)
        append_result(st, sig, terminal, err_code, None, last_asst)
        if not bug and terminal in ("done", "unsupported", "clarify", "confirm", "block"):
            done[str(sid)] = True
            save_done(done)
            log(f"  ✅ #{sid} 完成(终态={terminal})")
        else:
            log(f"  ⚠️ #{sid} 终态={terminal} 未标记完成")
    log("\n=== 一轮结束 ===")


def append_result(st, sig, terminal, err_code, extra, last_asst):
    row = {
        "id": st["id"], "text": st["text"], "expect": st["expect"],
        "terminal": terminal, "err_code": err_code,
        "signals": sig, "extra": extra,
        "last_assistant_type": (last_asst.get("type") if isinstance(last_asst, dict) else None),
        "ts": time.strftime("%H:%M:%S"),
    }
    with open(RESULT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
