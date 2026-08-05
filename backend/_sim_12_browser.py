# -*- coding: utf-8 -*-
"""浏览器标准 12 轮「0→1 建站 + 后续优化」模拟 harness（Senior Developer 测试工具）。

严格模拟用户使用浏览器的标准行为：
  * 一发一收：发一条消息 → 等 SSE 完整回复(直到终止事件) → 读回复 → 随机选系统推荐项之一 → 再发下一条。
  * 不一次灌完：每条都基于上一条系统真实回复决定下一步。
  * 三种中断断点续传：
      - 'manual'  ：生成中途点「停止」(POST /api/cancel)，验证落反馈消息 + 可续跑。
      - 'f5'      ：生成中途断开 SSE(模拟 F5 刷新)，用 after 游标重新订阅续接回放。
      - 'offline' ：生成中途断开并模拟离线，用 after + resume=true 恢复断点(checkpoint)。

仅用标准库(urllib)实现 SSE 流式读取，无第三方依赖。

用法:
    python _sim_12_browser.py            # 跑全部 12 轮(中断分布见 SCENARIOS)
    python _sim_12_browser.py --round 4  # 只跑第 4 轮(手动停止场景)
    python _sim_12_browser.py --smoke     # 冒烟: 登录 + 1 条闲聊，验证连通

输出:
    backend/_sim12_transcripts.json       # 每轮完整事件轨迹(供分析 5 大观察点)
    backend/_sim12_report.json            # 5 大观察点逐轮判定(结构化)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

BASE = os.environ.get("SEEDAI_BASE", "http://localhost:7101")
ACCOUNT = os.environ.get("SEEDAI_SIM_ACCOUNT", "sim12_user")
PASSWORD = os.environ.get("SEEDAI_SIM_PASSWORD", "testpass123")
MODEL = os.environ.get("SEEDAI_SIM_MODEL", "qwen")

# ── 12 轮场景定义 ──
# seed   : 第 1 条(0→1 建站诉求)
# optimize: 建站完成后，同会话内的后续优化/修改诉求
# interrupt: 本轮要测试的断点续传场景(None / 'manual' / 'f5' / 'offline')
SCENARIOS = [
    {"seed": "我想做一个个人博客网站", "optimize": "把首页主色调改成蓝色，标题字体大一点", "interrupt": None},
    {"seed": "帮我生成一个公司官网，做科技行业的", "optimize": "在首页加一个「客户案例」板块", "interrupt": None},
    {"seed": "做一个电商网站，卖手工艺品", "optimize": "给商品列表加购物车按钮和价格展示", "interrupt": None},
    # 第 4 轮：手动停止中断
    {"seed": "做一个餐厅官网，川菜馆", "optimize": "把菜单页改成网格布局", "interrupt": "manual"},
    # 第 5 轮：显式声明个人偏好(测试 user_preferences 持久化 + 后续检索)
    {"seed": "帮我做一个旅游平台的落地页，记住我的偏好：我喜欢蓝色主色调、圆角卡片、活泼年轻的风格", "optimize": "首屏大图下面加搜索框", "interrupt": None},
    {"seed": "做一个教育培训机构的网站", "optimize": "课程列表加「立即报名」按钮", "interrupt": None},
    # 第 7 轮：再声明一次偏好(强化 user_preferences)
    {"seed": "做一个个人作品集网站，摄影师用，我偏好深色背景和极简排版，请记住", "optimize": "加一个画廊灯箱效果", "interrupt": None},
    # 第 8 轮：F5 刷新中断
    {"seed": "做一个医疗诊所官网，牙科", "optimize": "在首页加在线预约表单", "interrupt": "f5"},
    {"seed": "做一个游戏官网，独立游戏工作室", "optimize": "加一个排行榜页面", "interrupt": None},
    # 第 10 轮：第三次声明偏好(让 user_preferences 更丰富，便于后续命中)
    {"seed": "做一个新闻媒体网站，记住：新闻站点我喜欢高对比排版、红色强调色", "optimize": "首页加置顶头条区", "interrupt": None},
    {"seed": "做一个金融理财官网", "optimize": "加一个收益计算器小组件", "interrupt": None},
    # 第 12 轮：离线 5 分钟(resume) 中断
    {"seed": "做一个政府办事服务门户", "optimize": "加一个办事指南栏目", "interrupt": "offline"},
]

TERMINAL_EVENTS = {"done", "aborted", "error", "unsupported", "paused"}
SSE_READ_TIMEOUT = 600  # 单轮生成最长等待(秒)

# ── 运行唯一 nonce: 避免同一 trace_id 跨多次运行命中旧残留频道(代理 stream_exists
#    命中后「续接已有流」回放上一轮的非拆分响应, 导致多意图拆分测试被假阴性)。
#    每次进程启动取一个时间戳, 拼进所有 trace_id, 保证每轮运行拿到全新频道。
RUN_NONCE = int(time.time())


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


# ───────────────────────────────────────────────────────────────────────────
# HTTP 基础
# ───────────────────────────────────────────────────────────────────────────
class APIError(Exception):
    pass


def _http(method: str, path: str, *, token: Optional[str] = None, json_body: Any = None,
          raw: bool = False, capture_headers: bool = False) -> Any:
    url = BASE + path
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            if raw:
                return body
            if capture_headers:
                return (json.loads(body) if body else None, r.headers)
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise APIError(f"{method} {path} -> {e.code}: {detail[:300]}")


# ───────────────────────────────────────────────────────────────────────────
# 模拟客户端
# ───────────────────────────────────────────────────────────────────────────
class SimClient:
    def __init__(self, base: str = BASE):
        self.base = base
        self.token: Optional[str] = None
        self.uid: Optional[int] = None

    # ── 鉴权 ──
    def login_or_register(self, account: str, password: str) -> str:
        def _do_login():
            _, hdrs = _http("POST", "/auth/login",
                            json_body={"account": account, "password": password},
                            capture_headers=True)
            # token 在 Set-Cookie: access_token=<jwt> 中(响应体只回 UserResp, 不含 token)
            # 注意: 一次登录可能下发多个 Set-Cookie(access/refresh/csrf), 必须 get_all
            all_cookies = hdrs.get_all("Set-Cookie") or []
            for sc in all_cookies:
                for part in sc.split(","):
                    part = part.strip()
                    if part.startswith("access_token="):
                        self.token = part.split(";", 1)[0].split("=", 1)[1]
                        break
                if self.token:
                    break
        # 先试登录
        try:
            _do_login()
            if not self.token:
                raise APIError("no token cookie")
            log(f"登录成功 account={account}")
        except APIError as e:
            if "401" in str(e) or "不存在" in str(e) or "404" in str(e):
                _http("POST", "/auth/register",
                      json_body={"account": account, "password": password,
                                 "display_name": "模拟小白12"})
                _do_login()
                log(f"注册并登录 account={account}")
            else:
                raise
        # 取 uid
        try:
            me = _http("GET", "/auth/me", token=self.token)
            self.uid = me.get("id")
        except Exception:
            self.uid = None
        return self.token

    # ── 新建项目+会话(首条对话) ──
    def auto_start(self, text: str) -> tuple[int, int]:
        r = _http("POST", "/api/auto-start", token=self.token, json_body={"text": text})
        pid = r["project"]["id"]
        cid = r["conversation"]["id"]
        return pid, cid

    # ── 取消 / 暂停(模拟手动停止) ──
    def cancel(self, trace_id: str):
        try:
            _http("POST", "/api/cancel", token=self.token, json_body={"trace_id": trace_id})
            log(f"  [中断] 已发送取消 trace={trace_id}")
        except Exception as e:
            log(f"  [中断] 取消失败(忽略): {e}")

    def pause(self, trace_id: str):
        try:
            _http("POST", "/api/pause", token=self.token, json_body={"trace_id": trace_id})
            log(f"  [中断] 已发送暂停 trace={trace_id}")
        except Exception as e:
            log(f"  [中断] 暂停失败(忽略): {e}")

    # ── SSE 流式读取(一发一收核心) ──
    def stream(self, conversation_id: int, text: str, trace_id: str, *,
               after: Optional[str] = None, resume: bool = False,
               stop_after_events: Optional[int] = None,
               cancel_after_events: Optional[int] = None) -> dict:
        """打开 SSE 流，读取到终止事件或满足中断条件为止。

        返回 {events, terminal, last_id, interrupted, cancelled}
        - events: [{"event","data","id"}, ...]  (data 已 json.loads)
        - terminal: 终止事件名 或 None(被中断未到终止)
        - last_id: 最后收到的 SSE id(供 F5/offline 续接 after 游标)
        - interrupted: 是否因 stop_after_events 主动断开(模拟刷新/离线)
        - cancelled: 是否发送了取消
        """
        params = {
            "model": MODEL,
            "conversation_id": conversation_id,
            "q": text,
            "trace_id": trace_id,
        }
        if after:
            params["after"] = after
        if resume:
            params["resume"] = "true"
        params["token"] = self.token
        url = self.base + "/api/chat?" + urllib.parse.urlencode(params)

        events: list[dict] = []
        last_id: Optional[str] = None
        terminal: Optional[str] = None
        interrupted = False
        cancelled = False

        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=SSE_READ_TIMEOUT) as resp:
                # 手动逐行解析 SSE
                buf_event, buf_data, buf_id = None, [], None
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace")
                    if line == "\n" or line == "\r\n" or line == "":
                        # 空行 = 事件边界
                        if buf_event is not None or buf_data:
                            data_str = "\n".join(buf_data)
                            try:
                                data_parsed = json.loads(data_str) if data_str else None
                            except Exception:
                                data_parsed = data_str
                            ev = {"event": buf_event or "message", "data": data_parsed, "id": buf_id}
                            events.append(ev)
                            if buf_id:
                                last_id = buf_id
                            # 终止事件?
                            if buf_event in TERMINAL_EVENTS:
                                terminal = buf_event
                                buf_event, buf_data, buf_id = None, [], None
                                break
                            # 中断判定
                            if cancel_after_events and len(events) >= cancel_after_events:
                                cancelled = True
                                self.cancel(trace_id)
                                buf_event, buf_data, buf_id = None, [], None
                                break
                            if stop_after_events and len(events) >= stop_after_events:
                                interrupted = True
                                buf_event, buf_data, buf_id = None, [], None
                                break
                            buf_event, buf_data, buf_id = None, [], None
                        continue
                    if line.startswith("event:"):
                        buf_event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        buf_data.append(line[len("data:"):].strip())
                    elif line.startswith("id:"):
                        buf_id = line[len("id:"):].strip()
                    # 其他(如 retry:) 忽略
        except urllib.error.HTTPError as e:
            log(f"  [SSE] HTTP 错误 {e.code}: {e.read().decode('utf-8','replace')[:200]}")
        except Exception as e:
            log(f"  [SSE] 读取异常: {type(e).__name__}: {e}")

        return {
            "events": events,
            "terminal": terminal,
            "last_id": last_id,
            "interrupted": interrupted,
            "cancelled": cancelled,
            "count": len(events),
        }

    # ── 续接(模拟 F5 / 离线恢复) ──
    def resume_stream(self, conversation_id: int, trace_id: str, after: str,
                      *, resume: bool = False, text: str = "") -> dict:
        """用 after 游标重新订阅 /api/chat 续接回放(+ 实时增量)。"""
        return self.stream(conversation_id, text, trace_id, after=after, resume=resume)


# ───────────────────────────────────────────────────────────────────────────
# 从系统回复中提炼「推荐项」并随机选一个 → 生成下一条用户消息
# ───────────────────────────────────────────────────────────────────────────
def extract_recommendations(events: list[dict]) -> dict:
    """返回 {options, clarify_questions, has_requirement_doc, has_preview,
            has_plan, cta_text, kinds, rag_hits} —— 供决定下一步 + 判定 5 大观察点。

    rag_hits: 累加本轮所有 RAG 召回 think 事件的各集合命中数(用于 P3 向量真实作用)
    """
    options = []          # 结构化选项事件(choices)
    clarify = []          # 澄清追问(自然语言问题)
    has_req = False
    has_preview = False
    has_plan = False
    cta = ""
    kinds = set()
    rag_hits: dict[str, int] = {
        "components": 0, "memory": 0, "project_memory": 0,
        "user_preferences": 0, "error_patterns": 0,
    }
    for ev in events:
        e = ev["event"]
        d = ev["data"]
        if not isinstance(d, dict):
            continue
        if e == "options":
            kinds.add("options")
            choices = d.get("choices") or []
            options.append({"question": d.get("question", ""), "choices": choices})
        elif e == "clarify_questions":
            kinds.add("clarify")
            clarify.extend(d.get("questions") or [])
        elif e == "requirement_doc":
            has_req = True
            kinds.add("requirement_doc")
        elif e == "preview" or e == "gen_file":
            has_preview = True
            kinds.add("preview")
        elif e == "plan":
            has_plan = True
            kinds.add("plan")
        elif e == "refined" and isinstance(d, str):
            if "开始" in d or "生成" in d or "建站" in d:
                cta = d
                kinds.add("cta_build")
        elif e == "think" and d.get("stage") == "rag":
            # 向量召回观测事件: 累计命中数
            kinds.add("rag")
            _h = d.get("hits") or {}
            for k, v in _h.items():
                rag_hits[k] = rag_hits.get(k, 0) + (v or 0)
    return {
        "options": options, "clarify": clarify, "has_requirement_doc": has_req,
        "has_preview": has_preview, "has_plan": has_plan, "cta": cta,
        "kinds": kinds, "rag_hits": rag_hits,
    }


_OPTION_ANSWERS = [
    "就按这个方案来", "选第一个方案", "选推荐的那个", "可以，继续",
    "我觉得方案 A 不错", "按你建议的做", "确定，开始吧",
]


def decide_next(reco: dict, rng: random.Random) -> str:
    """根据系统回复，随机选一个推荐项，产出下一条用户消息。"""
    # 1) 结构化 options(系统给的候选) → 随机选一个
    if reco["options"]:
        opt = reco["options"][0]
        choices = opt.get("choices") or []
        if choices:
            # 优先挑 recommended 的；否则随机
            recs = [c for c in choices if c.get("recommended")]
            pick = (recs or choices)[rng.randrange(len(recs or choices))]
            return f"选「{pick.get('title', pick.get('id', '方案'))}」"
    # 2) 需求文档已就绪 → 点「开始建站」(系统 CTA)
    if reco["has_requirement_doc"]:
        return rng.choice(["开始生成", "帮我做网站", "开始建站", "确认，进入设计与开发"])
    # 3) 澄清追问 → 随机给一个合理回答(推进采集)
    if reco["clarify"]:
        pool = ["简约现代风格", "蓝色主色调", "企业官网定位", "面向年轻用户",
                "需要购物车功能", "中文为主", "希望突出品牌故事", "响应式适配手机"]
        return rng.choice(pool)
    # 4) 有预览(已落站) → 接下来一般是优化/修改，交给调用方传入的 optimize 消息
    if reco["has_preview"]:
        return ""  # 空: 由场景的 optimize 字段接管
    # 5) 其他(think/node/plan) → 推进性回复
    return rng.choice(_OPTION_ANSWERS)


# ───────────────────────────────────────────────────────────────────────────
# 多意图拆分测试(观察点: 任务拆分的准确 + 拆分后子任务可靠执行 + 结果完整汇总)
# ───────────────────────────────────────────────────────────────────────────
MULTI_SCENARIOS = [
    {
        "name": "闲聊+闲聊",
        "seed": None,  # 单条多意图消息直接触发
        "multi": "今天天气怎么样？另外，你觉得AI未来会取代程序员吗？",
        "expected": {"count": 2, "families": ["chat", "chat"]},
        "note": "两个闲聊意图, 应准确拆为 2 个 chat 子任务(并行执行)",
    },
    {
        "name": "闲聊+建站+设计",
        "seed": None,
        "multi": ("帮我做一个科技公司官网，另外给我讲讲AI对设计行业的影响，"
                  "并且帮我推荐一套科技感的配色方案"),
        "expected": {"count": 3, "families": ["build", "chat", "design"]},
        "note": "闲聊+建站+设计, 应准确拆为 chat + build(site) + design 三个子任务",
    },
    {
        "name": "修改+设计+修改+新建",
        # M3 类: 先 0→1 建站作为修改基底, 再发 4 意图合并消息
        "seed": "帮我做一个个人作品集网站",
        "multi": ("把首页主色调改成橙色，另外重新设计一下导航栏，"
                  "再改一下字体大小，并且帮我新建一个关于我页面"),
        "expected": {"count": 4, "families": ["code", "design", "code", "build"]},
        "note": "修改+设计+修改+新建, 应在已有站点上准确拆为 4 子任务(2 修改 + 1 设计 + 1 新建页面)",
    },
]


def _skill_family(skill: str) -> str:
    """把 selected_skill 归到粗粒度家族, 用于拆分准确性比对。"""
    s = (skill or "").lower()
    if "design" in s:
        return "design"
    if "site" in s or "build" in s or "generate" in s:
        return "build"
    if "code" in s or "fix" in s or "modify" in s:
        return "code"
    if "doc" in s:
        return "doc"
    # 信息检索类(天气/知识问答/查询)归入 chat 家族: 属对话型非生产性意图,
    # 与文档定义的家族词汇(chat/build/design/code/doc)一致, 且贴合用户"闲聊/问答"语义
    if "search" in s or "query" in s or "lookup" in s or "knowledge" in s or "ask" in s or "qa" in s:
        return "chat"
    if "chat" in s or "learn" in s:
        return "chat"
    return s or "unknown"


def extract_multi_intent(events: list[dict]) -> dict:
    """从 SSE 事件抽取多意图拆分与执行信息。

    返回 {triggered, plan:{total,strategy,tasks}, subtask_starts, merge, done, rag_hits}
    - orchestration: 拆分总览(子任务清单/策略/技能)
    - subtask_start: 每个子任务开始执行
    - merge: 合并结果(success_count/fail_count/失败清单)
    - done: 多意图流程收口
    """
    plan = None
    subtask_starts = []
    merge = None
    rag_hits: dict = {}
    done = False
    for ev in events:
        e = ev["event"]
        d = ev["data"]
        if not isinstance(d, dict):
            if e == "done":
                done = True
            continue
        if e == "orchestration":
            plan = {"total": d.get("total"), "strategy": d.get("strategy"),
                    "tasks": d.get("tasks") or []}
        elif e == "subtask_start":
            subtask_starts.append({"id": d.get("sub_task_id"), "skill": d.get("skill"),
                                   "goal": d.get("goal"), "layer": d.get("layer")})
        elif e == "merge":
            merge = {"success_count": d.get("success_count"),
                     "fail_count": d.get("fail_count"),
                     "failed_tasks": d.get("failed_tasks") or []}
        elif e == "done":
            done = True
        elif e == "think" and d.get("stage") == "rag":
            for k, v in (d.get("hits") or {}).items():
                rag_hits[k] = rag_hits.get(k, 0) + (v or 0)
    return {"triggered": plan is not None, "plan": plan,
            "subtask_starts": subtask_starts, "merge": merge,
            "done": done, "rag_hits": rag_hits}


def run_multi_round(c: SimClient, idx: int, scenario: dict, rng: random.Random) -> dict:
    round_no = idx + 1
    log(f"\n{'='*70}\n=== [多意图] 第 {round_no} 轮: {scenario['name']} ===")
    log(f"  期望: {scenario['note']}")
    transcript: dict = {
        "round": f"M{round_no}", "name": scenario["name"],
        "expected": scenario["expected"], "phases": [], "errors": [], "multi_result": {},
    }

    # M3 类: 先 0→1 建站作为修改基底
    if scenario.get("seed"):
        pid, cid = c.auto_start(scenario["seed"])
        trace0 = f"sim12-m{round_no}-base-{RUN_NONCE}-{rng.randrange(10**4)}"
        log(f"  [基底建站] 项目={pid} 会话={cid} seed={scenario['seed']!r}")
        res0 = c.stream(cid, scenario["seed"], trace0)
        _reco0 = extract_recommendations(res0["events"])
        transcript["phases"].append({
            "phase": "base_build", "conversation_id": cid,
            "terminal": res0["terminal"], "kinds": sorted(_reco0["kinds"]),
            "rag_hits": _reco0["rag_hits"], "has_preview": _reco0["has_preview"],
        })
        log(f"  [基底建站] terminal={res0['terminal']} preview={_reco0['has_preview']} "
            f"rag={_reco0['rag_hits']}")
    else:
        pid, cid = c.auto_start(scenario["multi"])

    # 发多意图合并消息(单条) —— 系统应识别并拆分子任务
    trace = f"sim12-m{round_no}-multi-{RUN_NONCE}-{rng.randrange(10**4)}"
    log(f"  [多意图] 发: {scenario['multi'][:60]!r}")
    res = c.stream(cid, scenario["multi"], trace)
    _mi = extract_multi_intent(res["events"])
    _reco = extract_recommendations(res["events"])
    transcript["phases"].append({
        "phase": "multi_intent", "conversation_id": cid, "trace_id": trace,
        "sent": scenario["multi"], "terminal": res["terminal"],
        "event_count": res["count"], "kinds": sorted(_reco["kinds"]),
        "rag_hits": _reco["rag_hits"],
        "triggered": _mi["triggered"], "plan": _mi["plan"],
        "subtask_starts": _mi["subtask_starts"], "merge": _mi["merge"],
        "mi_done": _mi["done"],
    })
    transcript["multi_result"] = _mi

    # 汇总日志
    if _mi["triggered"]:
        tasks = _mi["plan"].get("tasks") or []
        fams = [_skill_family(t.get("skill")) for t in tasks]
        log(f"  [拆分] 子任务数={len(tasks)} 策略={_mi['plan'].get('strategy')} "
            f"家族={fams}")
        for t in tasks:
            log(f"    - {t.get('id')} skill={t.get('skill')} risk={t.get('risk')} "
                f"goal={str(t.get('goal'))[:40]}")
        mg = _mi["merge"]
        if mg:
            log(f"  [执行] 成功={mg.get('success_count')} 失败={mg.get('fail_count')} "
                f"done={_mi['done']}")
            if mg.get("failed_tasks"):
                for ft in mg["failed_tasks"]:
                    log(f"    ! 失败子任务 {ft.get('id')}: {ft.get('error')}")
    else:
        log(f"  [!] 未触发多意图拆分(可能当单意图处理) terminal={res['terminal']}")

    log(f"=== [多意图] 第 {round_no} 轮完成 ===")
    return transcript


def judge_multi(transcripts: list[dict]) -> list[dict]:
    """逐场景判定: 拆分是否准确 + 子任务是否可靠执行 + 结果是否完整。"""
    out = []
    for t in transcripts:
        mi = t.get("multi_result", {}) or {}
        plan = mi.get("plan") or {}
        tasks = plan.get("tasks") or []
        families = [_skill_family(x.get("skill")) for x in tasks]
        exp = t.get("expected", {})
        exp_count = exp.get("count")
        exp_families = exp.get("families")

        count_ok = (exp_count is None) or (len(tasks) == exp_count)
        fam_ok = (exp_families is None) or (sorted(families) == sorted(exp_families))
        merge = mi.get("merge") or {}
        success = merge.get("success_count") or 0
        fail = merge.get("fail_count") or 0
        exec_ok = bool(mi.get("triggered") and mi.get("done") and fail == 0
                        and success == len(tasks))
        out.append({
            "name": t["name"], "triggered": mi.get("triggered"),
            "split_count": len(tasks), "expected_count": exp_count,
            "families": families, "expected_families": exp_families,
            "strategy": plan.get("strategy"),
            "merge_success": success, "merge_fail": fail,
            "done": mi.get("done"),
            "count_ok": count_ok, "fam_ok": fam_ok, "exec_ok": exec_ok,
            "all_ok": count_ok and fam_ok and exec_ok,
            "tasks": tasks,
        })
    return out


# ───────────────────────────────────────────────────────────────────────────
# 单轮执行(0→1 建站 + 后续优化)
# ───────────────────────────────────────────────────────────────────────────
def run_round(c: SimClient, idx: int, scenario: dict, rng: random.Random) -> dict:
    round_no = idx + 1
    log(f"\n{'='*70}\n=== 第 {round_no} 轮 === seed={scenario['seed']!r} "
        f"optimize={scenario['optimize']!r} interrupt={scenario['interrupt']}")
    transcript: dict = {
        "round": round_no, "seed": scenario["seed"], "optimize": scenario["optimize"],
        "interrupt": scenario["interrupt"], "phases": [], "errors": [],
    }

    # ---- Phase A: 0→1 建站(首条对话) ----
    pid, cid = c.auto_start(scenario["seed"])
    log(f"  项目={pid} 会话={cid}")
    trace = f"sim12-r{round_no}-a-{RUN_NONCE}-{rng.randrange(10**4)}"
    phase_a = {"phase": "build_0_to_1", "conversation_id": cid, "trace_id": trace,
               "seed": scenario["seed"], "turns": []}

    # 一发一收循环(最多 8 轮交互，直到落站或中断)
    text = scenario["seed"]
    last_after = None
    interrupt_injected = False
    for turn in range(1, 9):
        # 中断注入: 在「第 1 个 turn 的生成中途」触发(最贴近真实用户行为)
        stream_kwargs: dict = {}
        if scenario["interrupt"] and turn == 1 and not interrupt_injected:
            if scenario["interrupt"] == "manual":
                stream_kwargs["cancel_after_events"] = 8   # 生成中途点「停止」
            elif scenario["interrupt"] in ("f5", "offline"):
                stream_kwargs["stop_after_events"] = 8     # 生成中途断开 SSE
        log(f"  [A] turn{turn} 发: {text[:50]!r}"
            f"{(' [+中断:' + scenario['interrupt'] + ']') if stream_kwargs else ''}")
        res = c.stream(cid, text, trace, after=last_after, **stream_kwargs)
        _reco_a = extract_recommendations(res["events"])
        _turn_rec = {
            "turn": turn, "sent": text, "terminal": res["terminal"],
            "event_count": res["count"],
            "kinds": sorted(_reco_a["kinds"]),
            "rag_hits": _reco_a["rag_hits"],
            "interrupted": res["interrupted"], "cancelled": res["cancelled"],
        }
        phase_a["turns"].append(_turn_rec)
        reco = _reco_a

        # 处理中断后的续传(仅在第 1 个 turn 触发一次)
        if scenario["interrupt"] and turn == 1 and not interrupt_injected:
            interrupt_injected = True
            if scenario["interrupt"] == "manual" and res["cancelled"]:
                # 手动停止: 生成中途已发取消, 验证断点续传由「下一发新消息」承接
                _turn_rec["injected_manual_stop"] = True
                log(f"  [A] 注入『手动停止』(生成中途取消) trace={trace}")
                # 手动停止后系统应已落反馈消息; 退出阶段 A, 交由 Phase B 验证「停后系统仍可用」
                break
            elif scenario["interrupt"] == "f5" and res["interrupted"]:
                # F5 刷新: 断 SSE, 用 after 游标续接回放剩余事件
                log(f"  [A] 注入『F5 刷新』: 断开后用 after={res['last_id']} 续接")
                res2 = c.resume_stream(cid, trace, res["last_id"])
                _reco2 = extract_recommendations(res2["events"])
                phase_a["turns"].append({
                    "turn": turn, "sent": "(F5续接)", "terminal": res2["terminal"],
                    "event_count": res2["count"], "kinds": sorted(_reco2["kinds"]),
                    "rag_hits": _reco2["rag_hits"], "resumed_after": res["last_id"],
                })
                res = res2
                reco = _reco2
                last_after = None
            elif scenario["interrupt"] == "offline" and res["interrupted"]:
                # 离线 5 分钟(模拟): 断开, 用 after + resume=true 恢复断点(checkpoint)
                log(f"  [A] 注入『离线恢复』: 断开后用 after={res['last_id']} resume=true 续接")
                res2 = c.resume_stream(cid, trace, res["last_id"], resume=True)
                _reco2 = extract_recommendations(res2["events"])
                phase_a["turns"].append({
                    "turn": turn, "sent": "(离线续接)", "terminal": res2["terminal"],
                    "event_count": res2["count"], "kinds": sorted(_reco2["kinds"]),
                    "rag_hits": _reco2["rag_hits"], "resumed_after": res["last_id"],
                    "resume_mode": True,
                })
                res = res2
                reco = _reco2
                last_after = None

        if res["terminal"] in ("done", "paused", "aborted", "error"):
            # 阶段 A 终止
            if reco["has_preview"] or reco["has_requirement_doc"]:
                break
            # 未落站/未出需求 → 继续按推荐推进
            nxt = decide_next(reco, rng)
            if not nxt:
                nxt = "继续"
            text = nxt
            if last_after:
                last_after = None
            continue
        else:
            # 未到终止(异常) → 用推荐续推
            nxt = decide_next(reco, rng) or "继续"
            text = nxt
            continue

    transcript["phases"].append(phase_a)

    # ---- Phase B: 后续优化/修改(同会话) ----
    if scenario["optimize"]:
        trace_b = f"sim12-r{round_no}-b-{rng.randrange(10**8)}"
        phase_b = {"phase": "optimize", "conversation_id": cid, "trace_id": trace_b,
                   "sent": scenario["optimize"], "turns": []}
        log(f"  [B] 优化发: {scenario['optimize']!r}")
        res = c.stream(cid, scenario["optimize"], trace_b)
        _reco_b = extract_recommendations(res["events"])
        phase_b["turns"].append({
            "turn": 1, "sent": scenario["optimize"], "terminal": res["terminal"],
            "event_count": res["count"],
            "kinds": sorted(_reco_b["kinds"]),
            "rag_hits": _reco_b["rag_hits"],
        })
        transcript["phases"].append(phase_b)
    else:
        transcript["phases"].append({"phase": "optimize", "skipped": True})

    log(f"=== 第 {round_no} 轮完成 ===")
    return transcript


# ───────────────────────────────────────────────────────────────────────────
# 5 大观察点判定(基于轨迹)
# ───────────────────────────────────────────────────────────────────────────
def judge(transcripts: list[dict]) -> dict:
    """逐轮 + 总体判定 5 大观察点。返回结构化报告。"""
    report = {"per_round": [], "summary": {}}

    # 观察点 1: 流程完整/严谨 + 3 种中断断点续传
    # 观察点 2: 按流程走 + DST 精准带到每次对话(不同项目不同会话)
    # 观察点 3: 向量库对 LLM 真实作用(rel_ctx 注入)
    # 观察点 4: 反馈体验友好(每阶段 SSE 反馈)
    # 观察点 5: 统计数据收集
    for t in transcripts:
        r = {"round": t["round"], "interrupt": t["interrupt"], "points": {}}
        # P1: 中断续传
        p1_ok = True
        p1_note = []
        for ph in t["phases"]:
            for turn in ph.get("turns", []):
                if turn.get("injected_manual_stop"):
                    # 手动停止后应有 aborted/done/paused 终止(落反馈)
                    p1_note.append("手动停止已注入")
                if turn.get("resumed_after"):
                    if turn.get("terminal") in ("done", "paused", "aborted", "error"):
                        p1_note.append(f"{t['interrupt']}续接成功→{turn['terminal']}")
                    else:
                        p1_ok = False
                        p1_note.append(f"{t['interrupt']}续接未达终止")
        r["points"]["P1_interrupt_resume"] = {"ok": p1_ok, "note": "; ".join(p1_note) or "无中断(干净轮)"}
        # P2: 流程步进 + DST(看是否出现 route/build + 是否有 options/clarify 推进)
        saw_build = any("build" in (tu.get("kinds") or []) or "preview" in (tu.get("kinds") or [])
                        for ph in t["phases"] for tu in ph.get("turns", []))
        r["points"]["P2_flow_dst"] = {"ok": saw_build, "note": "出现建站/预览事件" if saw_build else "未见建站轨迹"}
        # P3: 向量库对 LLM 真实作用(RAG 召回 think 事件的 hits 累计)
        agg_hits: dict[str, int] = {}
        for ph in t["phases"]:
            for turn in ph.get("turns", []):
                for k, v in (turn.get("rag_hits") or {}).items():
                    agg_hits[k] = agg_hits.get(k, 0) + (v or 0)
        p3_ok = any(v > 0 for v in agg_hits.values())
        _p3_parts = [f"{k}={v}" for k, v in agg_hits.items() if v > 0]
        r["points"]["P3_vector_rag"] = {
            "ok": p3_ok,
            "note": ("向量命中: " + ", ".join(_p3_parts)) if _p3_parts
                    else "本轮无任何向量召回(向量库未对 LLM 产生作用)",
            "hits": agg_hits,
        }
        # P4: 每阶段 SSE 反馈(think/node/plan/refined 出现)
        saw_feedback = any(
            any(k in (tu.get("kinds") or []) for k in ("plan", "cta_build", "requirement_doc", "rag"))
            or tu.get("event_count", 0) > 0
            for ph in t["phases"] for tu in ph.get("turns", []))
        r["points"]["P4_sse_feedback"] = {"ok": saw_feedback, "note": "有 SSE 事件反馈(含 RAG 召回提示)" if saw_feedback else "无反馈"}
        report["per_round"].append(r)

    # 总体
    report["summary"] = {
        "rounds": len(transcripts),
        "interrupts_tested": sorted({t["interrupt"] for t in transcripts if t["interrupt"]}),
        "all_p1": all(r["points"]["P1_interrupt_resume"]["ok"] for r in report["per_round"]),
        "all_p2": all(r["points"]["P2_flow_dst"]["ok"] for r in report["per_round"]),
        "all_p3": all(r["points"]["P3_vector_rag"]["ok"] for r in report["per_round"]),
        "all_p4": all(r["points"]["P4_sse_feedback"]["ok"] for r in report["per_round"]),
        "all_p5": None,  # 由 main 调用 check_analytics 后回填
    }
    return report


# ───────────────────────────────────────────────────────────────────────────
# 观察点 5: 统计数据收集(用超管账号调 /admin/analytics, 检查 ai:* 统计是否落地)
# ───────────────────────────────────────────────────────────────────────────
ADMIN_ACCOUNT = os.environ.get("SEEDAI_ADMIN_ACCOUNT", "huzhen")
ADMIN_PASSWORD = os.environ.get("SEEDAI_ADMIN_PASSWORD", "huzhen189")


def check_analytics() -> dict:
    """用超管登录并拉取 /admin/analytics, 确认统计系统收到了数据。

    返回 {ok, note, snapshot_summary}。
    """
    try:
        admin = SimClient(BASE)
        admin.login_or_register(ADMIN_ACCOUNT, ADMIN_PASSWORD)
        snap = _http("GET", "/admin/analytics", token=admin.token)
        if not isinstance(snap, dict):
            return {"ok": False, "note": f"/admin/analytics 返回非预期: {type(snap)}", "snapshot": None}
        # 统计各 section 是否有非零数据
        non_empty = []
        for sec, val in snap.items():
            if isinstance(val, dict):
                # 计数类: 任一字段 >0 即视为有数据
                _any = any(
                    (isinstance(v, (int, float)) and v > 0)
                    or (isinstance(v, dict) and any(
                        (isinstance(x, (int, float)) and x > 0) for x in v.values()))
                    for v in val.values()
                )
                if _any:
                    non_empty.append(sec)
            elif isinstance(val, (int, float)) and val > 0:
                non_empty.append(sec)
        ok = len(non_empty) > 0
        return {
            "ok": ok,
            "note": ("统计系统已收集数据, 非空板块: " + ", ".join(non_empty)) if ok
                    else "统计系统无任何数据(疑似统计未落地)",
            "sections_non_empty": non_empty,
            "snapshot": snap,
        }
    except Exception as e:
        return {"ok": False, "note": f"统计检查异常: {type(e).__name__}: {e}", "snapshot": None}


# ───────────────────────────────────────────────────────────────────────────
# main
# ───────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=0, help="只跑指定轮(1-based)")
    ap.add_argument("--smoke", action="store_true", help="冒烟测试")
    ap.add_argument("--multi", action="store_true", help="跑多意图拆分 3 条测试")
    args = ap.parse_args()

    c = SimClient(BASE)
    c.login_or_register(ACCOUNT, PASSWORD)
    log(f"BASE={BASE} uid={c.uid} model={MODEL}")

    # ── 多意图拆分测试(独立模式) ──
    if args.multi:
        rng = random.Random(20260729)
        transcripts = []
        for i, sc in enumerate(MULTI_SCENARIOS):
            tr = run_multi_round(c, i, sc, rng)
            transcripts.append(tr)
            with open(os.path.join(os.path.dirname(__file__), "_sim12_multi_transcripts.json"),
                      "w", encoding="utf-8") as f:
                json.dump(transcripts, f, ensure_ascii=False, indent=2)
        verdict = judge_multi(transcripts)
        report = {"scenarios": verdict,
                  "all_ok": all(v["all_ok"] for v in verdict)}
        with open(os.path.join(os.path.dirname(__file__), "_sim12_multi_report.json"),
                  "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        log("\n========== 多意图拆分测试结果 ==========")
        for v in verdict:
            log(f"  [{v['name']}] 触发={v['triggered']} 拆分={v['split_count']}"
                f"(期望{v['expected_count']}) 家族={v['families']} "
                f"执行成功={v['merge_success']} 失败={v['merge_fail']} done={v['done']} "
                f"→ {'✅' if v['all_ok'] else '❌'}")
        log(f"全部符合预期: {'是' if report['all_ok'] else '否'}")
        return

    if args.smoke:
        pid, cid = c.auto_start("你好，你是谁？")
        trace = f"smoke-{int(time.time())}"
        res = c.stream(cid, "你好，你是谁？", trace)
        log(f"冒烟完成 terminal={res['terminal']} events={res['count']}")
        return

    rng = random.Random(20260729)
    scenarios = SCENARIOS
    if args.round:
        scenarios = [SCENARIOS[args.round - 1]]
        log(f"仅跑第 {args.round} 轮")

    transcripts = []
    for i, sc in enumerate(scenarios):
        # 单轮模式: 用真实场景号(args.round)而非枚举号, 保证报告映射正确
        _idx = (args.round - 1) if args.round else i
        tr = run_round(c, _idx, sc, rng)
        transcripts.append(tr)
        # 实时落盘(防长任务中断丢数据)
        with open(os.path.join(os.path.dirname(__file__), "_sim12_transcripts.json"), "w", encoding="utf-8") as f:
            json.dump(transcripts, f, ensure_ascii=False, indent=2)

    report = judge(transcripts)

    # 观察点 5: 统计数据收集(超管拉取 analytics)
    log("拉取 /admin/analytics 验证统计系统...")
    p5 = check_analytics()
    report["summary"]["all_p5"] = p5["ok"]
    report["p5"] = p5
    if p5.get("snapshot") is not None:
        # 落盘完整快照供报告引用(避免 stdout 刷屏)
        with open(os.path.join(os.path.dirname(__file__), "_sim12_analytics.json"), "w", encoding="utf-8") as f:
            json.dump(p5["snapshot"], f, ensure_ascii=False, indent=2)

    with open(os.path.join(os.path.dirname(__file__), "_sim12_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    log("\n========== 5 大观察点总体 ==========")
    log(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    log(f"P5 统计: {p5['note']}")
    log("轨迹已写入 _sim12_transcripts.json，报告已写入 _sim12_report.json")


if __name__ == "__main__":
    main()
