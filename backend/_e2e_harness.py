"""端到端测试 harness（手动测试用，不进版本控制）。

流程: 注册新用户 → 登录 → 建项目 → 逐条发送 20 条语句(强制 model=deepseek)，
解析 /api/chat 的 SSE 事件，逐条记录「实际结果」。遇到崩溃(HTTP>=500 / 异常 /
SSE 静默断开无终止事件)即中止，便于人工修复后从断点续跑。

用法:
  python _e2e_harness.py            # 从头跑或续跑(自动跳过已完成)
  python _e2e_harness.py --reset     # 清空增量结果重跑
"""
from __future__ import annotations
import argparse, json, os, sys, time, uuid, re
import requests

BASE = "http://127.0.0.1:7101"
RESULT_FILE = os.path.join(os.path.dirname(__file__), "_e2e_results.jsonl")
PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "_e2e_progress.json")

# ---- 20 条语句: 由简到难、由单一到多意图 ----
# expect: 我对每条的预期(人类可读)。actual 由 harness 从 SSE 推断。
STATEMENTS = [
    {"id": 1, "text": "你好", "expect": "简单闲聊 → agent_chat，短回复，done"},
    {"id": 2, "text": "今天天气怎么样？", "expect": "闲聊/天气 → agent_chat（无工具则普通回复）"},
    {"id": 3, "text": "帮我写一首关于春天的短诗", "expect": "诗歌创作 → agent_doc/agent_chat 输出诗歌，done（不误伤为需求文档/PRD）"},
    {"id": 4, "text": "把『Hello World』翻译成中文", "expect": "翻译 → agent_chat/doc，输出中文，done"},
    {"id": 5, "text": "给我讲个冷笑话", "expect": "闲聊 → agent_chat，done"},
    {"id": 6, "text": "帮我总结一下这段话：人工智能正在改变软件开发的方式，开发者可以利用大模型完成代码生成、测试和文档编写。", "expect": "摘要 → agent_doc/agent_chat 输出要点，done（摘要不当作需求文档/PRD）"},
    {"id": 7, "text": "帮我设计一个简洁的登录页面", "expect": "单意图设计 → agent_design，输出设计稿/描述，done"},
    {"id": 8, "text": "帮我做一个个人博客网站", "expect": "单意图建站 → agent_build/agent_generate_site，产出预览，done"},
    {"id": 9, "text": "我想做一个电商网站，要有商品列表页和购物车功能", "expect": "建站带需求 → agent_generate_site，plan+建站，done"},
    {"id": 10, "text": "帮我写一份产品需求文档，关于一个待办事项应用", "expect": "强信号 → agent_requirement（build_requirement），输出 PRD，done（触发 requirement_doc 统计，不走 vector/LLM 误判）"},
    {"id": 11, "text": "帮我搜索一下最新的人工智能行业新闻", "expect": "搜索 → agent_search，输出检索结果，done"},
    {"id": 12, "text": "检查这段代码有没有问题：def add(a,b): return a+b", "expect": "代码评审 → agent_review，输出评审，done"},
    {"id": 13, "text": "帮我生成一个公司官网，并写一篇关于我们公司的介绍文章", "expect": "双意图(建站+文档) → 多意图门控命中连词『并』→ orchestration ≥2 子任务，merge，done"},
    {"id": 14, "text": "设计一个产品首页，并帮我写首页的营销文案", "expect": "双意图(设计+文档) → 多意图门控 → orchestration ≥2 子任务，done"},
    {"id": 15, "text": "给我做一个待办网站，再帮我写使用说明文档，顺便搜索一下同类产品", "expect": "三意图(建站+文档+搜索) → orchestration 3 子任务，done"},
    {"id": 16, "text": "我想做一个在线教育平台，需要课程列表页、课程详情页、购物车，还要写课程介绍文档，并且搜索竞品", "expect": "复杂多意图(建站含多页+文档+搜索) → orchestration，done"},
    {"id": 17, "text": "帮我把刚才那个博客网站改成深色主题", "expect": "迭代修改(需历史站) → 若无误判则走建站/修改，done 或 clarify"},
    {"id": 18, "text": "删除我的项目", "expect": "危险操作 → 18 号规则 block/拒绝删项目（提示改走设置软删除或删内部产物），不执行"},
    {"id": 19, "text": "帮我做一个金融数据看板，包含实时图表，还要生成使用文档并对比三个竞品", "expect": "复杂多意图(建站+文档+搜索) → 多意图门控命中『还要/并』→ orchestration ≥2 子任务，done"},
    {"id": 20, "text": "综合：做一个旅游小程序官网，写景点推荐文章，搜索热门目的地，再设计预订流程页", "expect": "极复杂多意图(建站+文档+搜索+设计) → orchestration 多子任务，done"},
]

MODEL = "deepseek"
TMP_PW = "testpass123"
USERNAME = f"e2e_{uuid.uuid4().hex[:8]}"


def log(*a):
    print("[harness]", *a, flush=True)


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


def parse_sse_stream(resp, max_seconds: float = 240):
    """逐行读取 SSE，返回 (events, terminated, terminal_event, error_code)。"""
    events = []
    terminal = None
    err_code = None
    start = time.time()
    buf = ""
    for raw in resp.iter_lines(decode_unicode=True):
        if time.time() - start > max_seconds:
            log("  !! SSE 超时(max_seconds), 强制截断")
            terminal = "timeout"
            break
        if raw is None:
            continue
        line = raw
        if line.startswith("event:"):
            buf = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = line[len("data:"):].strip()
            ev_type = buf or "message"
            # try parse json data
            try:
                payload = json.loads(data)
            except Exception:
                payload = data
            events.append({"event": ev_type, "data": payload})
            buf = ""
            # 终止事件
            if ev_type in ("done", "error", "aborted", "unsupported", "retry", "clarify", "confirm", "block"):
                terminal = ev_type
                if ev_type == "error" and isinstance(payload, dict):
                    err_code = payload.get("code")
                if ev_type in ("done", "error", "aborted", "unsupported", "retry"):
                    break
    return events, terminal, err_code


def detect_intent_signals(events):
    """从事件推断实际路由/意图信号。"""
    skills = set()
    stages = []
    has_orchestration = False
    subtasks = []
    interaction = None  # clarify/confirm/block/retry
    routed_skill = None  # 从 intent 事件捕获最终路由 skill
    intent_level = None  # 意图 level1/level2
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
            subtasks.append(d.get("goal"))
        # intent 事件(代理端/Worker 端都可能发): 含 selected_skill / level1/level2
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
        # node 阶段信息
        if t == "node" and isinstance(d, dict):
            st = d.get("stage")
            if st:
                stages.append(st)
    if routed_skill:
        skills.add(routed_skill)
    return {
        "skills": sorted(skills),
        "routed_skill": routed_skill,
        "intent_level": intent_level,
        "has_orchestration": has_orchestration,
        "subtask_count": len(subtasks),
        "interaction": interaction,
        "stages_sample": stages[:8],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
    if os.path.exists(RESULT_FILE) and args.reset:
        os.remove(RESULT_FILE)

    done = load_done()
    s = requests.Session()
    s.timeout = 300

    # 1) 注册
    log("注册用户", USERNAME)
    r = s.post(f"{BASE}/auth/register", json={
        "account": USERNAME, "password": TMP_PW,
        "display_name": "e2e测试", "email": f"{USERNAME}@test.com",
    })
    if r.status_code not in (200, 201):
        log("!! 注册失败", r.status_code, r.text[:300])
        return
    log("  注册 OK, 拿到 Cookie")

    # 2) 建项目
    r = s.post(f"{BASE}/api/projects", json={"name": f"e2e项目_{USERNAME}"})
    if r.status_code not in (200, 201):
        log("!! 建项目失败", r.status_code, r.text[:300])
        return
    project_id = r.json().get("id")
    log("  项目 ID =", project_id)

    for st in STATEMENTS:
        sid = st["id"]
        if str(sid) in done:
            log(f"-- 跳过已完成 #{sid}")
            continue
        # 每条新建会话, 隔离意图
        r = s.post(f"{BASE}/api/conversations", json={"project_id": project_id, "name": f"stmt{sid}"})
        if r.status_code not in (200, 201):
            log(f"!! #{sid} 建会话失败", r.status_code, r.text[:200])
            break
        cid = r.json().get("id")
        log(f"\n=== 发送 #{sid}: {st['text'][:40]}")
        url = f"{BASE}/api/chat?model={MODEL}&conversation_id={cid}&q={requests.utils.quote(st['text'])}"
        try:
            resp = s.get(url, stream=True)
        except Exception as e:
            log(f"!! #{sid} 请求异常: {e}")
            break
        if resp.status_code >= 500:
            log(f"!! #{sid} 服务端 500, 中止. body={resp.text[:300]}")
            # 记录失败
            append_result(st, None, "HTTP_500", None, resp.text[:300])
            break
        if resp.status_code != 200:
            log(f"!! #{sid} 非200 http={resp.status_code} body={resp.text[:200]}, 中止")
            append_result(st, None, f"HTTP_{resp.status_code}", None, resp.text[:200])
            break
        events, terminal, err_code = parse_sse_stream(resp, max_seconds=240)
        sig = detect_intent_signals(events)
        log(f"  终止事件={terminal} err_code={err_code}")
        log(f"  信号: skills={sig['skills']} orchestration={sig['has_orchestration']} subtasks={sig['subtask_count']} interaction={sig['interaction']}")
        # 是否视为 bug: 静默断开(无终止事件)或 error(非 AUTH/RETRY 类可恢复)
        bug = None
        if terminal is None:
            bug = "SSE 静默断开(无终止事件)"
        elif terminal == "error" and err_code not in (None,):
            # error 事件可能是合法(如 UNSUPPORTED)？记录但不一定中止
            if err_code not in ("UNSUPPORTED",):
                bug = f"error 事件 code={err_code}"
        if bug:
            log(f"  !! 检测到疑似 bug: {bug} → 中止, 待修复后续跑")
            append_result(st, sig, "BUG", bug, None)
            break
        append_result(st, sig, terminal, err_code, None)
        # 仅「成功终态」才标记为已完成; error/BUG 不标记, 便于修复后重跑。
        if not bug and terminal in ("done", "unsupported", "clarify", "confirm", "block"):
            done[str(sid)] = True
            save_done(done)
            log(f"  ✅ #{sid} 完成(终态={terminal})")
        else:
            log(f"  ⚠️ #{sid} 终态={terminal} 未标记为完成(将可重跑)")
    log("\n=== 测试一轮结束 ===")


def append_result(st, sig, terminal, err_code, extra):
    row = {
        "id": st["id"], "text": st["text"], "expect": st["expect"],
        "terminal": terminal, "err_code": err_code,
        "signals": sig, "extra": extra,
        "ts": time.strftime("%H:%M:%S"),
    }
    with open(RESULT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
