"""生成任务队列 + 进度分发(1-C: Redis 队列 + Worker 池)。

进度持久化改造(支撑「离线继续 + 重连回放」):
- 进度不再只走易失 PubSub,而是写入 **可回放的 Redis Stream** `gen:stream:<trace_id>`。
  Worker 每产出一个事件就 `XADD`;订阅端先 `XRANGE` 回放历史,再 `XREAD BLOCK` 续接实时,
  天然支持「客户端断线 → Worker 继续跑 → 重连从断点(或从头)回放」。
- 内存兜底(MemoryBackend)同样保存历史列表,支持按索引回放。

选择逻辑(get_queue):
- 环境变量 DEV_MEMORY_QUEUE=1 或 REDIS_URL 以 memory:// 开头 → MemoryBackend
- 否则 → RedisBackend(懒加载 redis 库,缺库时回退 MemoryBackend 并告警)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Any, Dict, Optional

from ..config import settings
from ..events import TERMINAL_EVENTS
from ..logging_config import set_trace
from .router import detect_intent_v2, skill_for
from .runner import run_skill
from ..registry import SkillRegistry
from ..intent.selection import set_pending_options
from .git_site import commit_site_for_trace
from ..analytics import (  # v0.9.0 新增功能统计
    record_repair, record_distill, record_code_index, record_refine, record_chat_retry,
)


_JOB_QUEUE = "queue:generate"
_STREAM_PREFIX = "gen:stream:"  # + trace_id -> Redis Stream(可回放进度)

logger = logging.getLogger(__name__)


async def _commit_after_done(trace_id: str, skill_name: str, user_text: str) -> None:
    """§8: 每轮生成(up to done)完成后,把站点目录就地提交为一次 git 版本。

    仅对产出站点/代码的 skill 提交(generate_site / write_code / orchestrator),
    explain 等纯文本 skill 不生成站点,跳过。
    失败仅告警(版本控制故障不能阻断主链路,与 QC 同策略)。
    """
    if skill_name in ("agent_build", "agent_generate_site", "orchestrator"):
        try:
            await asyncio.to_thread(commit_site_for_trace, trace_id, skill_name, user_text)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Worker] §8 git 提交失败(跳过) trace=%s: %s", trace_id, e)


async def _refine_assistant_dialog(raw_text: str, model_id: str = "deepseek-chat") -> str:
    """L2 对话精炼(v0.9.0): done 后 LLM 去冗余→保留完整信息→结构清晰。失败返回原文。"""
    if not raw_text.strip():
        return raw_text
    try:
        from ..providers import get_chat_model
        prompt = (
            "重写以下 AI 回复,去掉重复/口头语/冗余,保留完整信息,语气连贯自然,≤300字。\\n"
            f"原始回复:\\n{raw_text[:2000]}"
        )
        chat = get_chat_model(model_id, streaming=False)
        msgs = [{"role": "user", "content": prompt}]
        resp = await chat.ainvoke(msgs)
        refined = resp.content if hasattr(resp, "content") else str(resp)
        return refined.strip() or raw_text
    except Exception as e:
        logger.debug("[L2精炼] 失败, 回退原文: %s", e)
        return raw_text


async def _distill_memories(trace_id: str, user_id: int | None, project_id: int | None,
                            refined_text: str, skill_name: str) -> None:
    """L2+ 蒸馏(v0.9.0 P3): done 后从精炼对话抽取结构化项目记忆+用户偏好→写 Chroma。
    仅建站类 skill 触发; 失败仅 warn。"""
    if skill_name not in ("agent_build", "agent_generate_site", "orchestrator"):
        return
    if not (user_id or project_id) or not refined_text.strip():
        return
    try:
        from ..providers import get_chat_model
        from ..knowledge.chroma import upsert_project_memory, upsert_user_preference
        prompt = (
            "从以下对话中抽取关键信息,用 JSON 返回(不要代码块围栏):\\n"
            '{"project_memories":[{"type":"decision|constraint|requirement|artifact|fact",'
            '"content":"...","importance":1-5}],'
            '"user_prefs":[{"type":"style|constraint|habit","content":"...","importance":1-5}]}\\n'
            f"对话:\\n{refined_text[:2000]}"
        )
        chat = get_chat_model("deepseek-chat", streaming=False)
        resp = await chat.ainvoke([{"role": "user", "content": prompt}])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        import json, re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            # 写项目记忆
            for pm in data.get("project_memories", []):
                if project_id and user_id and pm.get("content"):
                    upsert_project_memory(
                        project_id, user_id, pm.get("type", "fact"),
                        pm["content"], int(pm.get("importance", 3)),
                    )
            # 写用户偏好
            for up in data.get("user_prefs", []):
                if user_id and up.get("content"):
                    upsert_user_preference(
                        user_id, up.get("type", "style"),
                        up["content"], int(up.get("importance", 3)), "distill",
                    )
            logger.info("[蒸馏] done trace=%s proj=%s user=%s proj_mems=%d user_prefs=%d",
                       trace_id, project_id, user_id,
                       len(data.get("project_memories", [])),
                       len(data.get("user_prefs", [])))
            record_distill(len(data.get("project_memories", [])),
                          len(data.get("user_prefs", [])))  # v0.9.0 统计
    except Exception as e:
        logger.debug("[蒸馏] 失败(跳过): %s", e)


async def _index_project_code(trace_id: str, project_id: int | None, skill_name: str) -> None:
    """P4(v0.9.0): 建站 done 后异步索引项目代码块到 Chroma project_code。
    仅 generate_site skill 触发; 失败仅 warn。"""
    if skill_name != "generate_site" or project_id is None:
        return
    try:
        import re
        from pathlib import Path
        from ..knowledge.chroma import upsert_project_code
        art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
        site_dir = art_dir / "anon" / (trace_id or "site")
        if not site_dir.exists():
            return
        for f in site_dir.rglob("*"):
            if f.suffix not in (".html", ".css", ".js"):
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            rel = str(f.relative_to(site_dir))
            lang = f.suffix.lstrip(".")
            # 简单按函数/区块切片(每300-800字一块)
            chunks = []
            if lang == "html":
                # 按 <section>, <div class, <article 分块
                for tag in re.finditer(r"<(section|article|div\s+class|nav|header|footer|main)\b[^>]*>.*?</\1>", text, re.DOTALL | re.IGNORECASE):
                    chunks.append(tag.group()[:1500])
                if not chunks:
                    chunks = [text[:1500]]
            else:
                # CSS/JS 按 800 字分块
                step = 800
                for i in range(0, len(text), step):
                    chunks.append(text[i:i + step][:1500])
            for chunk in chunks:
                if len(chunk.strip()) > 20:
                    import hashlib
                    h = hashlib.md5(chunk.encode()).hexdigest()[:16]
                    upsert_project_code(project_id, rel, chunk, h, language=lang)
            logger.info("[代码索引] done trace=%s proj=%s files=%d chunks=%d",
                       trace_id, project_id,
                       sum(1 for _ in site_dir.rglob("*") if _.suffix in (".html",".css",".js")),
                       sum(1 for _ in chunks if len(_.strip()) > 20))
            record_code_index(sum(1 for _ in chunks if len(_.strip()) > 20))  # v0.9.0 统计
    except Exception as e:
        logger.debug("[代码索引] 失败(跳过): %s", e)


def _skill_label(name: str) -> str:
    """取 skill 的前端展示名(用于多选项弹框标题)。"""
    try:
        entry = SkillRegistry.get(name)
        if entry and entry.display_name:
            return entry.display_name
    except Exception:
        pass
    return name


class QueueBackend:
    """队列抽象。子类实现具体存储。"""

    async def open_channel(self, trace_id: str):
        """建立进度通道句柄(在 enqueue 之前调用,避免丢首帧)。返回 subscribe 使用的键。"""
        raise NotImplementedError

    async def stream_exists(self, trace_id: str) -> bool:
        """该 trace_id 的进度流是否已存在(用于 /generate 判断是否续接而非重新入队)。"""
        raise NotImplementedError

    async def subscribe(
        self, trace_id: str, after: Optional[str] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """迭代进度事件,直到终止事件。

        - after=None:从流起点全量回放(新任务首次连接 / 重连全量回放);
        - after=<id>:仅回放该 id 之后的增量(断点续传)。
        """
        raise NotImplementedError

    async def enqueue(self, job: Dict[str, Any]) -> None:
        raise NotImplementedError

    async def dequeue(self) -> Dict[str, Any]:
        raise NotImplementedError

    async def publish(self, trace_id: str, event: Dict[str, Any]) -> None:
        raise NotImplementedError

    async def is_cancelled(self, trace_id: str) -> bool:
        raise NotImplementedError

    async def set_cancel(self, trace_id: str) -> None:
        raise NotImplementedError


class MemoryBackend(QueueBackend):
    def __init__(self):
        self._jobs: asyncio.Queue = asyncio.Queue()
        self._progress: Dict[str, asyncio.Queue] = {}  # 实时转发队列
        self._history: Dict[str, list] = {}  # trace_id -> [event, ...] 历史(可回放)
        self._cancel: set = set()

    async def open_channel(self, trace_id: str):
        self._history.setdefault(trace_id, [])
        return trace_id

    async def stream_exists(self, trace_id: str) -> bool:
        return trace_id in self._history

    async def subscribe(self, trace_id: str, after: Optional[str] = None):
        history = self._history.get(trace_id, [])
        # 回放历史(after 为 None 全量;为索引字符串时回放其后部分)
        start = 0
        if after is not None:
            try:
                start = int(after) + 1
            except ValueError:
                start = 0
        for ev in history[start:]:
            yield ev
            if ev.get("event") in TERMINAL_EVENTS:
                return
        # 历史已含终止事件 -> 直接结束(无需再等实时)
        if history and history[-1].get("event") in TERMINAL_EVENTS:
            return
        # 续接实时队列
        q = self._progress.setdefault(trace_id, asyncio.Queue())
        while True:
            ev = await q.get()
            yield ev
            if ev.get("event") in TERMINAL_EVENTS:
                break

    async def enqueue(self, job: Dict[str, Any]) -> None:
        await self._jobs.put(job)

    async def dequeue(self) -> Dict[str, Any]:
        return await self._jobs.get()

    async def publish(self, trace_id: str, event: Dict[str, Any]) -> None:
        self._history.setdefault(trace_id, []).append(event)
        q = self._progress.get(trace_id)
        if q is not None:
            await q.put(event)

    async def is_cancelled(self, trace_id: str) -> bool:
        return trace_id in self._cancel

    async def set_cancel(self, trace_id: str) -> None:
        self._cancel.add(trace_id)


class RedisBackend(QueueBackend):
    def __init__(self):
        import redis
        import redis.asyncio as aioredis

        # 关键:不能只做 TCP 探测。redis-py 默认走 RESP3 握手会先发 `HELLO` 命令,
        # 而部分云 Redis(老版本/代理)不支持 HELLO,会直接返回
        #   unknown command `HELLO` ... -> /generate 运行时 500。
        # 这里用「同步客户端 + protocol=2(避开 HELLO)」做一次真实 PING 握手,
        # 连不上 / 协议不兼容就抛 ConnectionError,让 get_queue 回退到 MemoryBackend。
        try:
            sync_r = redis.from_url(
                settings.redis_url,
                protocol=2,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            sync_r.ping()
            sync_r.close()
        except Exception as e:  # 含连接失败、HELLO/AUTH 不兼容等
            raise ConnectionError(f"Redis 不可用或不兼容: {e}") from e

        # 异步客户端:强制 protocol=2 + 心跳保活。
        # socket_timeout=15 > brpop timeout=5, 确保短轮询不会被 socket 超时误杀。
        self._r = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            protocol=2,
            health_check_interval=30,
            socket_keepalive=True,
            retry_on_timeout=True,
            socket_timeout=15,
        )

    def _key(self, trace_id: str) -> str:
        return f"{_STREAM_PREFIX}{trace_id}"

    async def open_channel(self, trace_id: str):
        # Stream 以 trace_id 为键,open 即确保后续 publish/subscribe 指向同一键。
        return trace_id

    async def stream_exists(self, trace_id: str) -> bool:
        try:
            return await self._r.exists(self._key(trace_id)) == 1
        except Exception:
            return False

    async def subscribe(self, trace_id: str, after: Optional[str] = None):
        key = self._key(trace_id)
        last_id = after or "0"
        # 1) 回放历史(增量或全量)
        if after is None:
            hist = await self._r.xrange(key, "-", "+")
        else:
            # 排他区间:(after, +] —— 只回放断点之后的事件
            hist = await self._r.xrange(key, f"({after}", "+")
        for entry_id, fields in hist:
            event = json.loads(fields.get("event", "{}"))
            yield event
            last_id = entry_id
            if event.get("event") in TERMINAL_EVENTS:
                return
        if hist:
            last_id = hist[-1][0]
        # 2) 续接实时(阻塞等待新事件,带连接断开重连)
        import redis.asyncio as aioredis

        while True:
            try:
                resp = await self._r.xread({key: last_id}, block=3000, count=100)
            except (aioredis.TimeoutError, aioredis.ConnectionError, OSError) as e:
                # 公网 NAT/防火墙掐断长连接时触发;重建客户端从 last_id 续接
                logger.warning("subscribe xread 断连, %s 秒后重连: %s", 1, e)
                await asyncio.sleep(1)
                with suppress(Exception):
                    await self._r.aclose()
                self._r = aioredis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    protocol=2,
                    health_check_interval=30,
                    socket_keepalive=True,
                    retry_on_timeout=True,
                    socket_timeout=15,
                )
                continue
            if not resp:
                # 超时无新数据:再探一次,避免长空闲误判;若流已含终止事件则退出
                continue
            for _k, entries in resp:
                for entry_id, fields in entries:
                    event = json.loads(fields.get("event", "{}"))
                    yield event
                    last_id = entry_id
                    if event.get("event") in TERMINAL_EVENTS:
                        return

    async def enqueue(self, job: Dict[str, Any]) -> None:
        await self._r.lpush(_JOB_QUEUE, json.dumps(job, ensure_ascii=False))

    async def dequeue(self) -> Dict[str, Any]:
        """阻塞出队。短轮询(brpop timeout=5s)防公网 Redis 空闲断开:
        - brpop 返回 None → 超时无任务, 正常空转
        - ConnectionError → 等 1s 重建客户端
        """
        import redis.asyncio as aioredis
        from contextlib import suppress

        while True:
            try:
                result = await self._r.brpop(_JOB_QUEUE, timeout=5)
                if result is None:
                    continue  # 5s 无任务, 正常空转
                _, raw = result
                return json.loads(raw)
            except (aioredis.ConnectionError, OSError) as e:
                logger.warning("dequeue 断连, 1s 后重建: %s", e)
                await asyncio.sleep(1)
                with suppress(Exception):
                    await self._r.aclose()
                self._r = aioredis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    protocol=2,
                    health_check_interval=30,
                    socket_keepalive=True,
                    retry_on_timeout=True,
                    socket_timeout=15,
                )

    async def publish(self, trace_id: str, event: Dict[str, Any]) -> None:
        # 持久化进度到可回放 Stream;XTRIM 限长避免无限膨胀
        await self._r.xadd(
            self._key(trace_id),
            {"event": json.dumps(event, ensure_ascii=False)},
            maxlen=5000,
            approximate=True,
        )

    async def is_cancelled(self, trace_id: str) -> bool:
        return await self._r.exists(f"cancel:{trace_id}") == 1

    async def set_cancel(self, trace_id: str) -> None:
        await self._r.set(f"cancel:{trace_id}", "1", ex=3600)


_backend: Optional[QueueBackend] = None


def get_queue() -> QueueBackend:
    """按环境选择队列后端(单例)。"""
    global _backend
    if _backend is not None:
        return _backend

    use_memory = os.getenv("DEV_MEMORY_QUEUE") == "1" or settings.redis_url.startswith("memory://")
    if use_memory:
        _backend = MemoryBackend()
        return _backend

    try:
        _backend = RedisBackend()
    except Exception as e:  # 缺 redis 库或连不上 → 退内存兜底,保证可跑
        import logging

        logging.warning("Redis 不可用,回退内存队列: %s", e)
        _backend = MemoryBackend()
    return _backend


async def worker_loop(concurrency: int = 1):
    """Worker 池:消费 queue:generate,运行 run_skill,把每个事件 publish 到对应进度流(持久化)。"""
    q = get_queue()

    # 用 asyncio 任务池模拟并发 Worker
    async def _one():
        while True:
            try:
                job = await q.dequeue()
                t_job = time.time()
                logger.info("[Worker] [1/6] 从队列取出任务 trace=%s", job.get("trace_id"))
            except Exception as e:
                logger.warning("[Worker] 取任务失败,1秒后重试: %s", e)
                await asyncio.sleep(1)
                continue
            trace_id = job.get("trace_id")
            set_trace(trace_id)  # 链路追踪:本 Job 处理期间所有日志带 trace=..
            model_id = job.get("model_id")
            messages = job.get("messages", [])
            skill = job.get("skill")
            conversation_id = job.get("conversation_id")
            qc_result = None  # v1.0: 全局初始化,非build类agent不跑QC
            # v1.0: 全局递归保护(借鉴 LangGraph recursion_limit)
            recursion_count = job.get("recursion_count", 0)
            if recursion_count >= 20:
                logger.error("[Worker] 递归超限 trace=%s count=%d,终止", trace_id, recursion_count)
                await q.publish(trace_id, {
                    "event": "error",
                    "data": {"message": "任务执行步数超限(20),已安全终止,请尝试简化需求或拆分任务"}
                })
                await q.publish(trace_id, {"event": "done", "data": {}})
                continue

            # ── [2/6] Chroma 向量索引 ──
            if conversation_id:
                from ..knowledge.chroma import index_message
                logger.info("[Worker] [2/6] Chroma向量索引 conv=%d msgs=%d 开始...",
                           conversation_id, len(messages))
                indexed = 0
                for i, msg in enumerate(messages):
                    idx = msg.get("_msg_id") or (conversation_id * 1000 + i)
                    try:
                        await asyncio.to_thread(index_message, idx, conversation_id, msg.get("role", "user"), msg.get("content", ""))
                        msg["_msg_id"] = idx  # 回写, 修复上下文模块 Chroma 死代码
                        indexed += 1
                    except Exception:
                        pass
                logger.info("[Worker] [2/6] Chroma索引完成 成功=%d/%d (+%.0fms)", indexed, len(messages),
                           (time.time() - t_job) * 1000)
            else:
                logger.info("[Worker] [2/6] 跳过Chroma索引(无conversation_id)")

            async def _cancelled(trace_id=trace_id):
                return await q.is_cancelled(trace_id) if trace_id else False

            try:
                # ── [3/6] 上下文检测 ──
                ctx_hint = job.get("context_hint", "")
                summary = job.get("conversation_summary", "")
                user_id = job.get("user_id")                    # v0.9.0
                project_id = job.get("project_id")              # v0.9.0
                doc = job.get("requirement_doc")
                proj_status = job.get("project_status", "draft")
                # Tier 1/2: 项目系统 prompt + 结构化硬约束(由 business 侧解析后下发)
                proj_prompt = job.get("project_system_prompt", "") or ""
                proj_constraints = job.get("project_constraints") or []
                user_text = ""
                for msg in messages:
                    if msg.get("role") == "user":
                        user_text = (msg.get("content", "") or "")[:100]
                        break
                logger.info("[Worker] [3/6] 上下文检测 输入=\"%.80s\" ctx_hint=%.40s summary=%.40s (+%.0fms)",
                           user_text, ctx_hint[:40] if ctx_hint else "无", summary[:40] if summary else "无",
                           (time.time() - t_job) * 1000)
                # 意图分类 v2(5模块并行, 35s超时)
                try:
                    intent = await asyncio.wait_for(
                        detect_intent_v2(messages, model_id,
                                         conversation_id=conversation_id,
                                         context_hint=ctx_hint,
                                         project_status=proj_status,
                                         project_constraints=proj_constraints,
                                         user_id=user_id, project_id=project_id),
                        timeout=35.0,
                    )
                except asyncio.TimeoutError:
                    logger.error("[Worker] [3/6] 意图分类超时(35s) → 降级")
                    intent = {"level1": "learn", "level2": "casual", "confidence": 0.3,
                              "industry": "other", "checkpoint_relation": "none",
                              "selected_skill": "agent_chat", "decision": "fallback"}
                ctx_result = ctx_hint or "检测完成"
                logger.info("[Worker] [3/6] 上下文结果 ctx=%.60s (+%.0fms, 含意图分类)", ctx_result,
                           (time.time() - t_job) * 1000)

                # ── [4/6] 意图分类(汇总器已算好最终 skill, 单一来源) ──
                decision = intent.get("decision", "route")
                confirmed = bool(job.get("confirmed", False))
                skill_name = skill or intent.get("selected_skill") or skill_for(intent["level1"], intent["level2"]) or "agent_chat"
                logger.info("[Worker] [4/6] 决策 decision=%s risk=%s 汇总skill=%s 最终skill=%s conf=%.0f%% (+%.0fms)",
                           decision, intent.get("risk_level", "?"), intent.get("selected_skill"),
                           skill_name, intent.get("confidence", 0) * 100,
                           (time.time() - t_job) * 1000)

                # ── [5/6] 决策分流(switch on decision) ──
                # 1) 高危拦截: 死红线, 即便用户确认也不可绕过
                if decision == "block":
                    reason = (intent.get("plan") or [{}])[0].get("reason", "高风险操作, 已拦截")
                    logger.warning("[Worker] [5/6] 安全拦截(不可绕过) reason=%s", reason)
                    await q.publish(trace_id, {"event": "block", "data": {"reason": reason}})
                    await q.publish(trace_id, {"event": "done", "data": {}})
                    continue

                # 2) 不支持的意图 → explain 降级(保留原有 unsupported 处理)
                if intent["level1"] == "unsupported":
                    logger.info("[Worker] [5/6] 不支持的功能 → explain降级")
                    async for event in run_skill(
                        "agent_chat", model_id, messages,
                        trace_id=trace_id, is_cancelled=_cancelled,
                        intent_info=intent,
                    ):
                        await q.publish(trace_id, event)
                    await q.publish(
                        trace_id,
                        {"event": "unsupported", "data": {
                            "input": (messages[-1].get("content", "") if messages else "")[:200],
                        }},
                    )
                    await q.publish(trace_id, {"event": "done", "data": {}})
                    logger.info("[Worker] [6/6] 执行完毕 unsupported→已降级")
                    continue

                # 3) 二次确认(high): 未确认则发 confirm 事件等前端回传(确认后带 confirmed 重发)
                if decision == "confirm" and not confirmed:
                    reason = (intent.get("plan") or [{}])[0].get("reason", "需确认")
                    logger.info("[Worker] [5/6] 二次确认 等待用户确认 skill=%s reason=%s", skill_name, reason)
                    await q.publish(trace_id, {"event": "confirm", "data": {"reason": reason, "skill": skill_name}})
                    await q.publish(trace_id, {"event": "done", "data": {}})
                    continue

                # 4) 多选项 → 改为非阻塞提示(系统已自己决定 top-1,不再阻塞用户)
                #    出 alternatives 事件供前端展示"已选 X,可切换 Y";随后照常执行 selected_skill。
                _alts_plan = next((p for p in (intent.get("plan") or [])
                                   if p.get("action") == "alternatives"), None)
                if decision == "options" or _alts_plan is not None:
                    alts = (_alts_plan or {}).get("skills", [])
                    hint = (_alts_plan or {}).get("hint", "")
                    logger.info("[Worker] [5/6] 非阻塞提示 alternatives=%s (系统已选 %s,不阻塞用户)", alts, skill_name)
                    if alts:
                        await q.publish(trace_id, {"event": "alternatives", "data": {
                            "selected": skill_name, "skills": alts, "hint": hint}})
                        if conversation_id:
                            # 存"完整有序候选列表"(top1 在前), 这样用户说 "B" → 第 2 个候选
                            # (仅 alts 不含 top1 会导致 "B" 越界); idx 0 = 已选 top1(无操作)。
                            set_pending_options(conversation_id, [skill_name, *alts])
                            logger.info("[Worker] [5/6] 已登记待选项供用户后续切换 conv=%s full=%s", conversation_id, [skill_name, *alts])
                    # 关键:不 continue,继续向下执行 selected_skill(决策自治)

                # 4.5) 多意图编排: 走 Orchestrator(子任务 DAG 调度 + 合并)
                if decision == "split":
                    sub_tasks_raw = intent.get("sub_tasks") or []
                    if not sub_tasks_raw:
                        logger.warning("[Worker] [5/6] split 决策但无 sub_tasks → 退化为单 skill")
                        skill_name = intent.get("selected_skill") or "agent_chat"
                    else:
                        from ..core.orchestrator import Orchestrator
                        from ..core.models import SharedContext, SubTask

                        def _dict_to_subtask(d: dict) -> SubTask:
                            valid = {k: v for k, v in d.items() if k in SubTask.__dataclass_fields__}
                            return SubTask(**valid)

                        sub_tasks = [_dict_to_subtask(d) for d in sub_tasks_raw]
                        confirmed_subtasks = set(job.get("confirmed_subtasks") or [])
                        shared_ctx = SharedContext(
                            requirement_doc=doc,
                            project_status={"status": proj_status},
                            conversation_summary=summary,
                            conversation_history=messages,
                        )
                        orch = Orchestrator()
                        logger.info("[Worker] [5/6] 多意图编排 sub_tasks=%d confirmed=%s",
                                    len(sub_tasks), confirmed_subtasks)
                        # 编排统计埋点(补充 6: 多意图必接统计)
                        from ..analytics import record_orchestration, record_sub_task
                        t0_split = time.time()
                        sub_start: dict[str, float] = {}
                        sub_meta = {s.id: (s.selected_skill, s.risk_level) for s in sub_tasks}
                        merge_data: dict = {}
                        event_cnt = 0
                        qc_user_text = user_text
                        qc_assistant_buf = []
                        done_event = None
                        async for event in orch.execute(
                            sub_tasks, model_id, messages,
                            trace_id=trace_id, is_cancelled=_cancelled,
                            confirmed_subtasks=confirmed_subtasks,
                            shared_ctx=shared_ctx,
                            original_query=user_text,
                            project_system_prompt=proj_prompt,
                            project_constraints=proj_constraints,
                        ):
                            if event.get("event") == "done":
                                done_event = event
                                continue
                            if event.get("event") == "token":
                                data = event.get("data", "")
                                if isinstance(data, str):
                                    qc_assistant_buf.append(data)
                            # 子任务级统计: 记录开始/完成耗时与状态
                            ev_name = event.get("event")
                            if ev_name == "subtask_start":
                                d = event.get("data") or {}
                                sid = d.get("sub_task_id")
                                if sid:
                                    sub_start[sid] = time.time()
                            elif ev_name == "subtask_done":
                                d = event.get("data") or {}
                                sid = d.get("sub_task_id")
                                skill, risk = sub_meta.get(sid, ("unknown", "low"))
                                dur = (time.time() - sub_start.get(sid, t0_split)) * 1000
                                await record_sub_task(skill, "done", risk, dur)
                            elif ev_name == "subtask_fail":
                                d = event.get("data") or {}
                                sid = d.get("sub_task_id")
                                skill, risk = sub_meta.get(sid, ("unknown", "low"))
                                dur = (time.time() - sub_start.get(sid, t0_split)) * 1000
                                reason = d.get("reason", "")
                                st = "blocked" if "高风险" in reason else ("skipped" if "中风险" in reason else "failed")
                                await record_sub_task(skill, st, risk, dur)
                            elif ev_name == "merge":
                                merge_data = event.get("data") or {}
                            await q.publish(trace_id, event)
                            event_cnt += 1
                        # 编排整体统计(成功率 + 总耗时 + 策略)
                        try:
                            sc = int(merge_data.get("success_count", 0))
                            fc = int(merge_data.get("fail_count", 0))
                            rate = sc / max(sc + fc, 1)
                            dur_ms = (time.time() - t0_split) * 1000
                            has_dep = any(s.dependencies for s in sub_tasks)
                            await record_orchestration(
                                len(sub_tasks), "mixed" if has_dep else "parallel", dur_ms, rate
                            )
                        except Exception as oe:  # noqa: BLE001
                            logger.warning("[Worker] 编排统计失败(跳过): %s", oe)
                        # 后置 QC(合并文本)
                        qc_assistant_text = "".join(qc_assistant_buf)
                        if qc_assistant_text.strip() and done_event is not None:
                            qc_result = None
                            try:
                                from ..qc import run_qc
                                from .safety import run_safety
                                safety_risk = run_safety(messages, project_constraints).risk_level
                                qc_result = await asyncio.wait_for(
                                    run_qc(qc_user_text, qc_assistant_text,
                                           project_constraints=project_constraints,
                                           safety_risk=safety_risk),
                                    timeout=60.0,
                                )
                                await q.publish(trace_id, {"event": "qc", "data": qc_result})
                            except Exception as qc_err:  # noqa: BLE001
                                logger.warning("[Worker] [6/6] 编排 QC 失败(跳过) trace=%s: %s", trace_id, qc_err)
                        if done_event is not None:
                            await q.publish(trace_id, done_event)
                        logger.info("[Worker] [6/6] 编排执行完毕 trace=%s 共%d事件", trace_id, event_cnt)
                        await _commit_after_done(trace_id, "orchestrator", user_text)
                        continue

                # 5) 正常路由 / fallback / 已确认 / 已选项 → 直接执行
                logger.info("[Worker] [5/6] 路由执行 skill=%s decision=%s doc=%s status=%s confirmed=%s (+%.0fms)",
                           skill_name, decision, "有" if doc else "无", proj_status, confirmed,
                           (time.time() - t_job) * 1000)
                event_cnt = 0
                qc_user_text = ""
                for m in messages:
                    if m.get("role") == "user":
                        qc_user_text = m.get("content", "") or ""
                        break
                qc_assistant_buf: list[str] = []
                done_event: dict | None = None
                async for event in run_skill(
                    skill_name, model_id, messages,
                    trace_id=trace_id, is_cancelled=_cancelled,
                    intent_info=intent,
                    requirement_doc=doc,
                    project_status=proj_status,
                    conversation_summary=summary,
                    project_system_prompt=proj_prompt,
                    project_constraints=proj_constraints,
                ):
                    # 拦截 done: 先发 QC 再发 done(QC 在 done 前, 不阻塞前端 done 渲染)
                    if event.get("event") == "done":
                        done_event = event
                        continue
                    if event.get("event") == "token":
                        data = event.get("data", "")
                        if isinstance(data, str):
                            qc_assistant_buf.append(data)
                    await q.publish(trace_id, event)
                    event_cnt += 1
                # ── [6/6] 后置 QC 三裁判(默认全量, v0.8.5 M1) ──
                qc_assistant_text = "".join(qc_assistant_buf)
                if qc_assistant_text.strip() and done_event is not None:
                    qc_result = None
                    try:
                        from ..qc import run_qc
                        from .safety import run_safety
                        safety_risk = run_safety(messages, project_constraints).risk_level
                        qc_result = await asyncio.wait_for(
                            run_qc(qc_user_text, qc_assistant_text,
                                   project_constraints=project_constraints,
                                   safety_risk=safety_risk),
                            timeout=60.0,
                        )
                        await q.publish(trace_id, {"event": "qc", "data": qc_result})
                        logger.info("[Worker] [6/6] QC 完成 trace=%s overall=%.2f needs_review=%s partial=%s",
                                   trace_id, qc_result.get("overall", 0),
                                   qc_result.get("needs_review"), qc_result.get("partial"))
                    except Exception as qc_err:  # noqa: BLE001
                        logger.warning("[Worker] [6/6] QC 执行失败(已跳过, 不影响主流程) trace=%s: %s",
                                       trace_id, qc_err)
                # Phase D(v0.9.0): 闲聊低分→1轮轻量重答
                _qc_ok = qc_result is not None  # qc_result 仅 QC 成功时赋值
                if skill_name == "agent_chat" and qc_assistant_text.strip() and _qc_ok:
                    try:
                        qc_overall = qc_result.get("overall", 10)
                        qc_needs = qc_result.get("needs_review", False)
                        if qc_overall < 5.0 or qc_needs:
                            logger.info("[闲聊重答] QC低分(%.1f)→触发1轮重答 trace=%s", qc_overall, trace_id)
                            from ..providers import get_chat_model
                            retry_prompt = (
                                "上一轮回答质量不佳,请重新回答。注意: 补充遗漏信息、修正事实错误、"
                                "语气自然流畅。\\n原始问题: " + qc_user_text
                            )
                            chat_r = get_chat_model(model_id, streaming=False)
                            resp_r = await chat_r.ainvoke([{"role": "user", "content": retry_prompt}])
                            retry_text = resp_r.content if hasattr(resp_r, "content") else str(resp_r)
                            if retry_text.strip():
                                qc_assistant_text = retry_text
                                done_event = {"event": "done", "data": {"content": retry_text}}
                                record_chat_retry(True)  # v0.9.0 统计
                                logger.info("[闲聊重答] 重答完成 len=%d", len(retry_text))
                    except Exception as _re:  # noqa: BLE001
                        logger.debug("[闲聊重答] 失败: %s", _re)
                if done_event is not None:
                    await q.publish(trace_id, done_event)
                # L2 对话精炼(v0.9.0): done 后 LLM 去冗余 → 改写 Message.content(仅建站类)
                if skill_name in ("agent_build", "agent_generate_site", "orchestrator") and qc_assistant_text.strip():
                    try:
                        refined = await _refine_assistant_dialog(qc_assistant_text)
                        await q.publish(trace_id, {"event": "refined", "data": refined[:500]})
                        logger.info("[Worker] L2 精炼完成 trace=%s len_before=%d len_after=%d",
                                   trace_id, len(qc_assistant_text), len(refined))
                        record_refine(len(qc_assistant_text), len(refined))  # v0.9.0 统计
                    except Exception as _le:  # noqa: BLE001
                        logger.debug("[Worker] L2 精炼失败: %s", _le)
                # L2+ 蒸馏(v0.9.0 P3): 从精炼对话抽取项目记忆+用户偏好→写 Chroma
                _user_id_job = job.get("user_id"); _project_id_job = job.get("project_id")
                if _user_id_job or _project_id_job:
                    try:
                        await _distill_memories(trace_id, _user_id_job, _project_id_job,
                                               qc_assistant_text, skill_name)
                    except Exception as _de:  # noqa: BLE001
                        logger.debug("[Worker] 蒸馏失败: %s", _de)
                # P4(v0.9.0): 项目代码索引(异步, 非阻塞)
                if _project_id_job:
                    try:
                        await _index_project_code(trace_id, _project_id_job, skill_name)
                    except Exception as _ie:  # noqa: BLE001
                        logger.debug("[Worker] 代码索引失败: %s", _ie)
                logger.info("[Worker] [6/6] 执行完毕 trace=%s skill=%s 共发出%d个事件 总耗时%.0fms",
                           trace_id, skill_name, event_cnt, (time.time() - t_job) * 1000)
                await _commit_after_done(trace_id, skill_name, qc_user_text)
            except Exception as e:
                logger.error("[Worker] 执行异常 trace=%s skill=%s 错误=%s: %s",
                            trace_id, skill_name, type(e).__name__, e)
                await q.publish(trace_id, {"event": "error", "data": str(e)})
                await q.publish(trace_id, {"event": "done", "data": {}})

    await asyncio.gather(*[_one() for _ in range(concurrency)])
