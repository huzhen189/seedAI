# -*- coding: utf-8 -*-
"""线上验证: 发「简历」口吻消息, 捕获 SSE intent 事件内容, 确认路由到 build/site(非 chat/web_search)。
读 intent 事件即可证明路由完成, 不等 LLM 生成。
"""
import sys, json, threading, time, urllib.parse, urllib.request
sys.path.insert(0, r"E:/work/myTencentYunHome/seedAI/backend")
from _test_multipage import Client

SEED = "帮我做一份个人简历网站，包含首页、关于我、作品、联系方式。风格简洁专业。"
c = Client(); c.login()
pid, cid = c.auto_start(SEED)
print(f"auto-start -> pid={pid} cid={cid}", flush=True)

result = {}

def stream_reader():
    params = {"model": "deepseek", "conversation_id": cid, "q": SEED,
              "trace_id": f"vresume-{int(time.time())}", "token": c.token}
    url = "http://127.0.0.1:7101/api/chat?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            pending_event = None
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
                if line.startswith("event:"):
                    pending_event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data = line[len("data:"):].strip()
                    if pending_event == "intent":
                        try:
                            result["intent"] = json.loads(data)
                        except Exception:
                            result["intent"] = data
                        print(">> intent SSE:", data[:240], flush=True)
                        return
                    if pending_event in ("done", "error", "aborted", "unsupported", "paused"):
                        result["terminal"] = pending_event
                        print(f">> terminal '{pending_event}' (routing done)", flush=True)
                        return
                    pending_event = None
    except Exception as e:
        print(f"[reader] {e}", flush=True)

t = threading.Thread(target=stream_reader, daemon=True)
t.start()
t.join(timeout=25)

if "intent" in result:
    d = result["intent"]
    if isinstance(d, dict):
        lvl = f"{d.get('level1')}/{d.get('level2')}"
        skill = d.get("selected_skill")
    else:
        lvl, skill = "?", "?"
    print(f"\n>>> 线上路由: {lvl} skill={skill}")
    print(">>> 修复生效 ✅ (简历口吻不再进 web_search)" if lvl == "build/site" and skill == "agent_generate_site" else ">>> 仍未修复 ❌")
else:
    print("\n>>> 未捕获 intent 事件(超时/连接问题):", result)
sys.exit(0)
