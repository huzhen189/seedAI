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
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Any, Dict, Optional

from ..config import settings
from ..events import TERMINAL_EVENTS
from .router import detect_intent_v2, skill_for
from .runner import run_skill
from ..registry import SkillRegistry


_JOB_QUEUE = "queue:generate"
_STREAM_PREFIX = "gen:stream:"  # + trace_id -> Redis Stream(可回放进度)

logger = logging.getLogger(__name__)


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
                logger.info("[Worker] [1/6] 从队列取出任务 trace=%s", job.get("trace_id"))
            except Exception as e:
                logger.warning("[Worker] 取任务失败,1秒后重试: %s", e)
                await asyncio.sleep(1)
                continue
            trace_id = job.get("trace_id")
            model_id = job.get("model_id")
            messages = job.get("messages", [])
            skill = job.get("skill")
            conversation_id = job.get("conversation_id")

            # ── [2/6] Chroma 向量索引 ──
            if conversation_id:
                from ..knowledge.chroma import index_message
                logger.info("[Worker] [2/6] Chroma向量索引 conv=%d msgs=%d 开始...",
                           conversation_id, len(messages))
                indexed = 0
                for i, msg in enumerate(messages):
                    idx = msg.get("_msg_id") or (conversation_id * 1000 + i)
                    try:
                        index_message(idx, conversation_id, msg.get("role", "user"), msg.get("content", ""))
                        msg["_msg_id"] = idx  # 回写, 修复上下文模块 Chroma 死代码
                        indexed += 1
                    except Exception:
                        pass
                logger.info("[Worker] [2/6] Chroma索引完成 成功=%d/%d", indexed, len(messages))
            else:
                logger.info("[Worker] [2/6] 跳过Chroma索引(无conversation_id)")

            async def _cancelled(trace_id=trace_id):
                return await q.is_cancelled(trace_id) if trace_id else False

            try:
                # ── [3/6] 上下文检测 ──
                ctx_hint = job.get("context_hint", "")
                summary = job.get("conversation_summary", "")
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
                logger.info("[Worker] [3/6] 上下文检测 输入=\"%.80s\" ctx_hint=%.40s summary=%.40s",
                           user_text, ctx_hint[:40] if ctx_hint else "无", summary[:40] if summary else "无")
                # 意图分类 v2(5模块并行, 35s超时)
                try:
                    intent = await asyncio.wait_for(
                        detect_intent_v2(messages, model_id,
                                         conversation_id=conversation_id,
                                         context_hint=ctx_hint,
                                         project_status=proj_status,
                                         project_constraints=proj_constraints),
                        timeout=35.0,
                    )
                except asyncio.TimeoutError:
                    logger.error("[Worker] [3/6] 意图分类超时(35s) → 降级")
                    intent = {"level1": "learn", "level2": "casual", "confidence": 0.3,
                              "industry": "other", "checkpoint_relation": "none",
                              "selected_skill": "explain", "decision": "fallback"}
                ctx_result = ctx_hint or "检测完成"
                logger.info("[Worker] [3/6] 上下文结果 ctx=%.60s", ctx_result)

                # ── [4/6] 意图分类(汇总器已算好最终 skill, 单一来源) ──
                decision = intent.get("decision", "route")
                confirmed = bool(job.get("confirmed", False))
                skill_name = skill or intent.get("selected_skill") or skill_for(intent["level1"], intent["level2"]) or "explain"
                logger.info("[Worker] [4/6] 决策 decision=%s risk=%s 汇总skill=%s 最终skill=%s conf=%.0f%%",
                           decision, intent.get("risk_level", "?"), intent.get("selected_skill"),
                           skill_name, intent.get("confidence", 0) * 100)

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
                        "explain", model_id, messages,
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

                # 4) 多选项(options): 用户未选则发 options 事件; 已带 skill 参数则直接执行
                if decision == "options" and not confirmed and not job.get("skill"):
                    plan = intent.get("plan") or []
                    skill_list = []
                    for p in plan:
                        if p.get("action") == "options":
                            skill_list = p.get("skills", [])
                    choices = [{"id": s, "title": _skill_label(s), "desc": ""} for s in skill_list]
                    logger.info("[Worker] [5/6] 多选项 候选=%s", skill_list)
                    await q.publish(trace_id, {"event": "options", "data": {
                        "question": "请选择处理方式", "choices": choices, "mode": "skill"}})
                    await q.publish(trace_id, {"event": "done", "data": {}})
                    continue

                # 5) 正常路由 / fallback / 已确认 / 已选项 → 直接执行
                logger.info("[Worker] [5/6] 路由执行 skill=%s decision=%s doc=%s status=%s confirmed=%s",
                           skill_name, decision, "有" if doc else "无", proj_status, confirmed)
                event_cnt = 0
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
                    await q.publish(trace_id, event)
                    event_cnt += 1
                logger.info("[Worker] [6/6] 执行完毕 trace=%s skill=%s 共发出%d个事件",
                           trace_id, skill_name, event_cnt)
            except Exception as e:
                logger.error("[Worker] 执行异常 trace=%s skill=%s 错误=%s: %s",
                            trace_id, skill_name, type(e).__name__, e)
                await q.publish(trace_id, {"event": "error", "data": str(e)})
                await q.publish(trace_id, {"event": "done", "data": {}})

    await asyncio.gather(*[_one() for _ in range(concurrency)])
