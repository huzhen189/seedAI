"""验证方案 A+B：建站必填信息收集硬闸门。

发送「你可以帮我做个网站吗？」——此前会被直接建站；改后应挂起并反问收集必填信息，
绝不执行 S6 建站（不应产生新 artifact）。

用法：先确保后端 7101 在线；`python scripts/_test_gate.py`
"""
from __future__ import annotations
import asyncio
import json
import re
import time
import httpx

BASE = "http://localhost:7101"
ACC, PW = "huzhen", "huzhen189"


def _find_event(text: str, event_type: str):
    """从 SSE 文本里抽取指定 type 的事件 data（取最后一个）。"""
    pat = re.compile(r'data:\s*(\{.*?\})\s*$', re.M)
    last = None
    for m in pat.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        if obj.get("type") == event_type:
            last = obj
    return last


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=90) as c:
        r = await c.post("/auth/login", json={"account": ACC, "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        c.headers["Authorization"] = f"Bearer {token}"

        r = await c.post("/api/projects", json={"name": "gate-test-project"})
        assert r.status_code in (200, 201), r.text
        project_id = r.json()["id"]
        print(f"[setup] project_id={project_id} (已有 project.name，闸门应只问 theme/brief/deploy_target)")

        r = await c.post("/api/conversations", json={"project_id": project_id, "name": "gate-test"})
        assert r.status_code in (200, 201), r.text
        conv_id = r.json()["id"]
        print(f"[setup] conv_id={conv_id}")

        client_msg_id = f"gate-{int(time.time())}"
        # /api/chat 直接返回 SSE 事件流（StreamingResponse）。消费其文本。
        async with c.stream("POST", "/api/chat", json={
            "conversation_id": conv_id,
            "message": "你可以帮我做个网站吗？",
            "client_msg_id": client_msg_id,
        }) as resp:
            assert resp.status_code == 200, await resp.aread()
            body = await resp.aread()
        text = body.decode("utf-8", "replace")
        print("[stream] len(bytes)=%d" % len(text))

        done = _find_event(text, "done")
        reply = (done or {}).get("data", {}).get("reply", "") if done else ""
        print("[reply]", reply)

        # 断言：不应直接建站 —— reply 必须包含"收集/确认/信息"等反问语义，
        # 且不应出现"建站完成/已为您生成/预览"等执行完成信号。
        expect_collect = any(k in reply for k in ("确认", "收集", "信息", "还需要", "补充", "动手"))
        # 执行完成信号（不应出现）：建站完成 / 已为您生成站点 / 已生成预览 等。
        # 注意 "部署" 本身是必填项的提问词，不能作为禁止词。
        forbid_build = any(k in reply for k in ("建站完成", "已为您生成", "已生成站点", "预览已就绪", "已发布"))
        print(f"[check] expect_collect={expect_collect} forbid_build={forbid_build}")
        assert expect_collect, f"回复未触发信息收集反问: {reply!r}"
        assert not forbid_build, f"回复疑似直接建站: {reply!r}"
        print("[PASS] 硬闸门生效：建站前先收集必填信息，未直接执行 S6。")

        # 5) 第二轮回填必填项：补充 风格+主题(brief)+部署，确认闸门放行并建站。
        client_msg_id2 = f"gate-{int(time.time())}-2"
        async with c.stream("POST", "/api/chat", json={
            "conversation_id": conv_id,
            "message": "做一个简约商务风格的网站，主要展示我们的烘焙教程与食谱，部署到平台托管。",
            "client_msg_id": client_msg_id2,
        }) as resp2:
            assert resp2.status_code == 200, await resp2.aread()
            body2 = await resp2.aread()
        text2 = body2.decode("utf-8", "replace")
        done2 = _find_event(text2, "done")
        reply2 = (done2 or {}).get("data", {}).get("reply", "") if done2 else ""
        print("[reply2]", reply2[:300])
        built = any(k in reply2 for k in ("建站完成", "已为您生成", "已生成", "预览", "部署完成"))
        collect2 = any(k in reply2 for k in ("确认", "还需要", "补充", "动手"))
        print(f"[check2] built={built} still_collect={collect2}")
        assert built or not collect2, f"回填后仍未建站/仍被反问: {reply2!r}"
        print("[PASS] 回填必填后闸门放行：进入建站执行。")


if __name__ == "__main__":
    asyncio.run(main())
