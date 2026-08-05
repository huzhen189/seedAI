import requests, json, uuid, time, traceback
BASE = "http://127.0.0.1:7101"
try:
    s = requests.Session(); s.timeout = 600
    u = f"smokeR_{uuid.uuid4().hex[:6]}"
    s.post(f"{BASE}/auth/register", json={"account": u, "password": "testpass123", "display_name": "smokeR", "email": f"{u}@t.com"})
    r = s.post(f"{BASE}/api/projects", json={"name": "smokeR_p"}); pid = r.json()["id"]
    r = s.post(f"{BASE}/api/conversations", json={"project_id": pid, "name": "smokeR_c"}); cid = r.json()["id"]
    q = "帮我做一个个人博客网站"
    url = f"{BASE}/api/chat?model=qwen&conversation_id={cid}&q=" + requests.utils.quote(q)
    resp = s.get(url, stream=True)
    terms = []
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        if raw.startswith("event:"):
            buf = raw[6:].strip()
        elif raw.startswith("data:"):
            terms.append(buf)
    print("FIRST last:", terms[-4:], "len", len(terms), flush=True)
    url2 = f"{BASE}/api/chat?model=qwen&conversation_id={cid}&confirmed=1&resume=true&q=" + requests.utils.quote("确认并生成")
    resp2 = s.get(url2, stream=True)
    from collections import Counter
    cnt = Counter()
    for raw in resp2.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        if raw.startswith("event:"):
            buf = raw[6:].strip()
        elif raw.startswith("data:"):
            cnt[buf] = cnt.get(buf, 0) + 1
    print("SECOND COUNTS:", dict(cnt), flush=True)
except Exception as e:
    traceback.print_exc()
