"""验证 /api/search/messages 的 turn_id 问答配对逻辑。

造两轮真实消息(同 turn_id 下 user + assistant 成对), 再搜索, 校验:
  1. 命中能正确配出 user_text / ai_reply
  2. 同轮双命中只出一条(按 turn 去重)
  3. project_name / conv_title 正确回填
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx
import pymysql

BASE = "http://127.0.0.1:7101"
ACCOUNT = "e2e20_seedai_test"
PASSWORD = "testpass123"


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def db_conn() -> pymysql.connections.Connection:
    env = dict(re.findall(r"^([A-Z_]+)=(.*)$", ENV_FILE.read_text(encoding="utf-8"), re.M))
    m = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)", env["MYSQL_URL"])
    assert m
    return pymysql.connect(
        host=m.group(3), port=int(m.group(4)), user=m.group(1),
        password=m.group(2), database=m.group(5), charset="utf8mb4", autocommit=True,
    )


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30.0) as c:
        r = c.post("/auth/login", json={"account": ACCOUNT, "password": PASSWORD})
        r.raise_for_status()
        tok = r.json().get("access_token") or r.json().get("token")
        if tok:
            c.headers["Authorization"] = f"Bearer {tok}"

        r = c.post("/api/auto-start", json={"text": "宠物医院预约系统"})
        r.raise_for_status()
        pid = r.json()["project"]["id"]
        cid = r.json()["conversation"]["id"]
        print(f"fixture: project={pid} conversation={cid}")

        conn = db_conn()
        cur = conn.cursor()
        rows = [
            # (turn_id, role, content)
            ("01JTURNAAAAAAAAAAAAAAAAAA1", "user", "首页要有预约挂号的入口，突出显示"),
            ("01JTURNAAAAAAAAAAAAAAAAAA1", "assistant", "好的，我把预约挂号做成首屏主 CTA 按钮"),
            ("01JTURNAAAAAAAAAAAAAAAAAA2", "user", "再加一个医生团队展示"),
            ("01JTURNAAAAAAAAAAAAAAAAAA2", "assistant", "已加入医生团队卡片列表，支持预约跳转"),
            # 无 turn_id 的历史消息(降级单条路径)
            (None, "user", "顺便把配色换成暖色调"),
        ]
        for turn_id, role, content in rows:
            cur.execute(
                "INSERT INTO messages (conversation_id, project_id, turn_id, role, content, content_refs) "
                "VALUES (%s,%s,%s,%s,%s,'[]')",
                (cid, pid, turn_id, role, content),
            )
        print(f"inserted {len(rows)} messages")

        fails = 0

        # --- 用例 1: 同轮双命中("预约"在 user 与 assistant 里都出现) 应去重为一条 ---
        r = c.get("/api/search/messages", params={"q": "预约挂号"})
        r.raise_for_status()
        hits = r.json()
        print(f"\n[case1] q='预约挂号' -> {len(hits)} 条")
        for h in hits:
            print(f"   turn配对: Q={h['user_text'][:24]!r} / A={h['ai_reply'][:24]!r}")
            print(f"   meta: project_name={h['project_name']!r} conv_title={h['conv_title']!r} msg_id={h['message_id']}")
        if len(hits) != 1:
            print(f"   XX 预期 1 条(同轮去重), 实得 {len(hits)}"); fails += 1
        elif not (hits[0]["user_text"] and hits[0]["ai_reply"]):
            print("   XX 问答未成对配出"); fails += 1
        elif hits[0]["project_name"] != "宠物医院预约系统":
            print(f"   XX project_name 错: {hits[0]['project_name']}"); fails += 1
        else:
            print("   OK 同轮去重 + 问答配对 + 元信息回填 均正确")

        # --- 用例 2: 仅 assistant 命中, 应反查出同轮的 user 提问 ---
        r = c.get("/api/search/messages", params={"q": "医生团队卡片"})
        hits = r.json()
        print(f"\n[case2] q='医生团队卡片'(只在 AI 回复中) -> {len(hits)} 条")
        if len(hits) == 1 and hits[0]["user_text"] == "再加一个医生团队展示":
            print(f"   OK 由 AI 回复反查出提问: {hits[0]['user_text']!r}")
        else:
            print(f"   XX 反查失败: {hits}"); fails += 1

        # --- 用例 3: turn_id 为空的历史消息, 降级单条 ---
        r = c.get("/api/search/messages", params={"q": "暖色调"})
        hits = r.json()
        print(f"\n[case3] q='暖色调'(turn_id 为空) -> {len(hits)} 条")
        if len(hits) == 1 and hits[0]["user_text"] == "顺便把配色换成暖色调" and hits[0]["ai_reply"] == "":
            print("   OK 无 turn_id 降级为单条, ai_reply 空")
        else:
            print(f"   XX 降级路径异常: {hits}"); fails += 1

        # --- 清理 ---
        cur.execute("DELETE FROM messages WHERE conversation_id=%s", (cid,))
        c.delete(f"/api/projects/{pid}")
        conn.close()
        print(f"\n==== 配对验证: FAIL={fails} ====")
        return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
