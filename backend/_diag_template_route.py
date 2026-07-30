# -*- coding: utf-8 -*-
"""诊断: 模版文案 / 简历口吻 文案分别被分成什么意图。
直接调 detect_intent_v2(worker 同一函数)。build/site 启发式在 LLM 前短路, 秒级;
落到 agent_chat 的会走 LLM 终判(可能触发 web_search 工具)。
"""
import sys, asyncio, json
sys.path.insert(0, r"E:/work/myTencentYunHome/seedAI/backend")
from dotenv import load_dotenv
load_dotenv(r"E:/work/myTencentYunHome/seedAI/backend/.env")

from app.agent.core.router import detect_intent_v2
from app.agent.intent.store import reset_sir

CASES = [
    ("①模版原文(portfolio)", "帮我做一个个人作品集网站，包含首页（个人简介+精选作品）、作品展示、关于我、联系方式 4 个页面。风格极简有设计感，突出作品图片，支持点击放大查看。"),
    ("②简历口吻-无网站", "帮我做一份个人简历，要有个人简介、教育背景、工作经历、项目经验、技能特长。"),
    ("③简历+网页", "帮我做一个简历网页，包含个人简介、教育、工作、项目。"),
    ("④简历+网站", "帮我做一个个人简历网站，包含首页、关于我、作品、联系方式。"),
    ("⑤纯简历名词", "我要一份简历模板"),
]


async def main():
    for idx, (label, text) in enumerate(CASES):
        cid = 99100 + idx
        reset_sir(cid, 1, 1)  # 隔离 SIR 状态, 避免用例间 PM 粘性/上下文闸门污染
        res = await detect_intent_v2(
            messages=[{"role": "user", "content": text}],
            model_id="deepseek",
            conversation_id=cid,
            user_id=1,
            project_id=1,
        )
        print(f"\n=== {label} (cid={cid}) ===")
        print("  文本:", text[:30], "...")
        print("  意图:", res.get("level1"), "/", res.get("level2"),
              "| decision=", res.get("decision"),
              "| skill=", res.get("selected_skill"),
              "| conf=", res.get("confidence"))
        print("  reason:", (res.get("reason") or "")[:80])

asyncio.run(main())
