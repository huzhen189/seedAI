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
from ..intent.selection import (
    set_pending_clarify, get_pending_clarify, clear_pending_clarify,
)
from .git_site import commit_site_for_trace
from ..analytics import (  # AI 核心原生统计(合并后同库, 命名空间 ai:/an:)
    record_repair, record_distill, record_code_index, record_refine,
    record_chat_retry, record_generate_request,
)
from ...analytics import (  # 业务端每步统计: skill 成效 + 生成阶段耗时 + 需求文档
    record_skill_outcome, record_gen_stage, record_requirement_doc,
)


_JOB_QUEUE = "queue:generate"
_STREAM_PREFIX = "gen:stream:"  # + trace_id -> Redis Stream(可回放进度)

logger = logging.getLogger("ai_service.queue")


async def _persist_qc_score(trace_id: str, model_id: str | None,
                            conversation_id: int | None, qc_result: dict) -> None:
    """补齐 QC 落库断点(v0.8.5 遗留): 每次三裁判评审都写进 qc_scores 表,
    供后台「AI 质量」报表 / 六维雷达图按历史逐条留痕、聚合统计。

    此前 run_qc 只写 Redis + 推 SSE, qc_scores 表始终为空 → 报表看不到 AI 质量。
    非阻塞: 调用方用 asyncio.create_task 触发, 失败仅告警, 绝不拖累主链路(done 流)。"""
    if not qc_result or qc_result.get("error"):
        return
    try:
        from ...db import SessionLocal
        from ...repos.trace_repos import qc_score_repo
        async with SessionLocal() as db:
            await qc_score_repo.upsert(
                db, trace_id, model_id, conversation_id, qc_result,
            )
            await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[Worker] QC 落库失败(跳过, 不影响主链路) trace=%s: %s", trace_id, e)


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


async def _qc_fix_loop(
    trace_id: str,
    q,  # QueueBackend 发布通道(用于回传重评分进度事件)
    qc_user_text: str,
    qc_assistant_text: str,
    model_id: str,
    project_constraints: Optional[list],
    version: Optional[int],
    user_id: Optional[int],
    project_id: Optional[int],
    skill_name: str,
    is_cancelled,
) -> tuple[Optional[dict], bool, int]:
    """质量闭环(v1.2.4): 后置 QC 标记 needs_review → 自动 agent_review 修复 → 重打分。

    仅对建站类技能(agent_build / agent_generate_site)生效; 闲聊(agent_chat)走自有 Phase D 重试,
    编排(orchestrator)多文件站点风险高, 暂不自动改写。流程(有界 qc_fix_max_rounds 轮, 默认 2):
      1. 以当前代码作为 user 消息调用 agent_review_handler, 取 fixed_code;
      2. 重写盘(ARTIFACT_DIR/anon/<trace_id>/) + 重传 COS(同版本 key 覆盖)
         → 后续 _commit_after_done 与预览均为修复版, 用户最终拿到改进产物;
      3. 对 fixed_code 重跑 run_qc; 收敛(needs_review=False)即停止, 否则继续(直至轮次上限)。
    返回 (final_qc_result, fix_applied, rounds)。任意异常仅告警并回退原始 qc_result, 绝不阻断主流程。
    """
    if not settings.qc_fix_enabled:
        return None, False, 0
    if skill_name not in ("agent_build", "agent_generate_site"):
        return None, False, 0
    if not qc_assistant_text or not qc_assistant_text.strip():
        return None, False, 0

    from ..skills.agent_review import agent_review_handler
    from ..tools.cos_upload import cos_upload
    from ..skills.agent_generate_site import _parse_multi_files

    art_dir = Path(os.getenv("ARTIFACT_DIR", "./artifacts"))
    ver_seg = f"v{version}" if version else (trace_id or "site")
    uid = user_id if user_id is not None else "anon"
    pid = project_id if project_id is not None else "anon"
    base_key = f"{os.getenv('COS_BASE_PATH', 'previews').strip('/')}/{uid}/{pid}/{ver_seg}"
    site_root = art_dir / "anon" / (trace_id or "site")

    current = qc_assistant_text
    final_result: Optional[dict] = None
    fix_applied = False
    rounds = 0
    max_rounds = max(1, settings.qc_fix_max_rounds)
    try:
        for rnd in range(max_rounds):
            if is_cancelled and await is_cancelled():
                logger.info("[质量闭环] 用户取消, 终止修复 trace=%s", trace_id)
                break
            # 1) agent_review 取 fixed_code
            review_msgs = [{"role": "user", "content": current}]
            fixed_code = None
            try:
                async for rev_ev in agent_review_handler(
                    model_id, review_msgs, trace_id=trace_id, is_cancelled=is_cancelled
                ):
                    d = rev_ev.get("data") or {}
                    if rev_ev.get("event") == "node" and d.get("fixed_code"):
                        fixed_code = d["fixed_code"]
            except Exception as e:  # noqa: BLE001
                logger.warning("[质量闭环] agent_review 异常(跳过本轮) trace=%s: %s", trace_id, e)
                break
            if not fixed_code or not fixed_code.strip():
                logger.info("[质量闭环] 无 fixed_code, 终止修复 trace=%s round=%d", trace_id, rnd + 1)
                break
            # 2) 重写盘 + 重传 COS(同版本 key 覆盖)
            try:
                for fname, content in _parse_multi_files(fixed_code).items():
                    fpath = site_root / fname
                    fpath.parent.mkdir(parents=True, exist_ok=True)
                    fpath.write_text(content, encoding="utf-8")
                    try:
                        cos_upload(str(fpath), f"{base_key}/{fname}")
                    except Exception as ce:  # noqa: BLE001
                        logger.debug("[质量闭环] COS 重传失败(忽略) %s: %s", f"{base_key}/{fname}", ce)
            except Exception as we:  # noqa: BLE001
                logger.warning("[质量闭环] 写盘失败(忽略本轮) trace=%s: %s", trace_id, we)
            fix_applied = True
            rounds = rnd + 1
            # 3) 重跑 QC
            try:
                from ..qc import run_qc
                from .safety import run_safety
                safety_risk = run_safety(review_msgs, project_constraints).risk_level
                qc2 = await asyncio.wait_for(
                    run_qc(qc_user_text, fixed_code,
                           project_constraints=project_constraints, safety_risk=safety_risk,
                           model_id=model_id),
                    timeout=settings.qc_timeout_seconds,
                )
                final_result = qc2
                if qc2.get("needs_review"):
                    # 未收敛: 回传本轮重评分(供前端展示进度), 继续修复
                    prog = dict(qc2)
                    prog["fix_round"] = rounds
                    prog["fix_applied"] = True
                    try:
                        await q.publish(trace_id, {"event": "qc", "data": prog})
                    except Exception:  # noqa: BLE001
                        pass
                    current = fixed_code
                    logger.info("[质量闭环] 仍未通过, 继续修复 trace=%s round=%d overall=%.2f",
                               trace_id, rounds, qc2.get("overall", 0))
                else:
                    logger.info("[质量闭环] 修复收敛 trace=%s round=%d overall=%.2f",
                               trace_id, rounds, qc2.get("overall", 0))
                    break
            except Exception as qe:  # noqa: BLE001
                logger.warning("[质量闭环] 重打分失败(终止) trace=%s: %s", trace_id, qe)
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("[质量闭环] 异常(回退原 QC) trace=%s: %s", trace_id, e)
        return None, fix_applied, rounds
    return final_result, fix_applied, rounds


async def _refine_assistant_dialog(raw_text: str, model_id: str = "deepseek") -> str:
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
        chat = get_chat_model("deepseek", streaming=False)
        resp = await chat.ainvoke([{"role": "user", "content": prompt}])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        import json, re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            # 写项目记忆(🔧 Chroma upsert 为同步阻塞调用, 隔离到线程池避免冻结 worker loop)
            for pm in data.get("project_memories", []):
                if project_id and user_id and pm.get("content"):
                    await asyncio.to_thread(
                        upsert_project_memory,
                        project_id, user_id, pm.get("type", "fact"),
                        pm["content"], int(pm.get("importance", 3)),
                    )
            # 写用户偏好
            for up in data.get("user_prefs", []):
                if user_id and up.get("content"):
                    await asyncio.to_thread(
                        upsert_user_preference,
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
    if skill_name not in ("agent_build", "agent_generate_site") or project_id is None:
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

    async def delete_channel(self, trace_id: str):
        """删除进度流(用于 resume 时清掉 await_confirm 阶段残留的 paused 流, 强制重新入队执行)。"""
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

    # ── 暂停原语(v4 断点复联): 与 cancel/abort 区分, 跑完当前阶段后再停 ──
    async def is_paused(self, trace_id: str) -> "str | None":
        """返回暂停原因(user_interrupt / offline_timeout), 未暂停返回 None。"""
        raise NotImplementedError

    async def set_pause(self, trace_id: str, reason: str) -> None:
        raise NotImplementedError

    async def clear_pause(self, trace_id: str) -> None:
        raise NotImplementedError

    async def clear_cancel(self, trace_id: str) -> None:
        raise NotImplementedError


class MemoryBackend(QueueBackend):
    def __init__(self):
        self._jobs: asyncio.Queue = asyncio.Queue()
        self._progress: Dict[str, asyncio.Queue] = {}  # 实时转发队列
        self._history: Dict[str, list] = {}  # trace_id -> [event, ...] 历史(可回放)
        self._cancel: set = set()
        self._pause: Dict[str, str] = {}  # trace_id -> reason(暂停原语)

    async def open_channel(self, trace_id: str):
        self._history.setdefault(trace_id, [])
        return trace_id

    async def stream_exists(self, trace_id: str) -> bool:
        return trace_id in self._history

    async def delete_channel(self, trace_id: str):
        # 内存兜底: 清掉历史 + 实时队列, 让 resume 能重新 open_channel + enqueue
        self._history.pop(trace_id, None)
        self._progress.pop(trace_id, None)

    async def subscribe(self, trace_id: str, after: Optional[str] = None):
        history = self._history.get(trace_id, [])
        # 回放历史(after 为 None 全量;为索引字符串时回放其后部分)
        start = 0
        if after is not None:
            try:
                start = int(after) + 1
            except ValueError:
                start = 0
        for i, ev in enumerate(history):
            if i < start:
                continue
            ev = dict(ev)
            ev["_id"] = str(i)
            yield ev
            if ev.get("event") in TERMINAL_EVENTS:
                return
        # 历史已含终止事件 -> 直接结束(无需再等实时)
        if history and history[-1].get("event") in TERMINAL_EVENTS:
            return
        # 续接实时队列
        q = self._progress.setdefault(trace_id, asyncio.Queue())
        idx = len(history)
        while True:
            ev = await q.get()
            ev = dict(ev)
            ev["_id"] = str(idx)
            idx += 1
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

    async def is_paused(self, trace_id: str) -> "str | None":
        return self._pause.get(trace_id)

    async def set_pause(self, trace_id: str, reason: str) -> None:
        self._pause[trace_id] = reason

    async def clear_pause(self, trace_id: str) -> None:
        self._pause.pop(trace_id, None)

    async def clear_cancel(self, trace_id: str) -> None:
        self._cancel.discard(trace_id)


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

    async def delete_channel(self, trace_id: str):
        try:
            await self._r.delete(self._key(trace_id))
        except Exception as e:  # noqa: BLE001
            logger.debug("[RedisBackend] delete_channel 失败(忽略) %s: %s", trace_id, e)

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
            event["_id"] = entry_id
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
                    event["_id"] = entry_id
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

    async def clear_cancel(self, trace_id: str) -> None:
        try:
            await self._r.delete(f"cancel:{trace_id}")
        except Exception:
            pass

    async def is_paused(self, trace_id: str) -> "str | None":
        try:
            return await self._r.get(f"pause:{trace_id}")
        except Exception:
            return None

    async def set_pause(self, trace_id: str, reason: str) -> None:
        await self._r.set(f"pause:{trace_id}", reason, ex=3600)

    async def clear_pause(self, trace_id: str) -> None:
        try:
            await self._r.delete(f"pause:{trace_id}")
        except Exception:
            pass


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

        logger.warning("Redis 不可用,回退内存队列: %s", e)
        _backend = MemoryBackend()
    return _backend


# 阶段进度 → 百分比(与 proxy.py 断点回放保持一致)
_PAUSE_STAGE_PROGRESS = {
    "planner_done": 25, "coder_done": 65,
    "reviewer_r0": 75, "reviewer_r1": 85, "reviewer_r2": 95,
}


async def _worker_handle_pause(
    trace_id: str,
    conversation_id: int | None,
    user_id: int | None,
    reason: str,
    stage: str | None,
    ck: tuple | None,
) -> None:
    """Worker 阶段边界暂停(v4 断点复联)。

    - 重存 checkpoint 到 Redis + MySQL(即使 SSE 客户端已断开, 断点数据也持久化, 供 resume 续跑)
    - 发 paused 事件(供仍连着的 SSE 客户端渲染「已暂停」)
    - 写 user_states.status=paused(权威状态源, 即使无 SSE 客户端也能恢复)
    """
    # 1) 重存 checkpoint
    if ck and conversation_id:
        try:
            from ..proxy import ck_set, _sync_checkpoint_to_mysql
            _stage, _data, _pct = ck
            await ck_set(conversation_id, _stage, _data, _pct)
            await _sync_checkpoint_to_mysql(conversation_id, _stage, _data, _pct)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Worker] 暂停时 checkpoint 持久化失败(忽略) trace=%s: %s", trace_id, e)
    # 2) 发 paused 事件(若已被 resume 取消接管, 跳过 —— 旧 Worker 的 paused 事件无意义且会误导客户端)
    try:
        q = get_queue()
        if await q.is_cancelled(trace_id):
            logger.info("[Worker] 暂停被取消(已被续跑接管),跳过 paused 事件与 status 回写 trace=%s", trace_id)
        else:
            await q.publish(trace_id, {"event": "paused", "data": {"reason": reason, "stage": stage or "?"}})
            # 3) 写 user_states(权威状态源)
            if user_id:
                try:
                    from ...user_state import touch_user_state
                    await touch_user_state(
                        user_id,
                        status="paused",
                        pause_reason=reason,
                        current_stage=stage,
                        pending_decision="continue_instruction",
                        active_trace_id=trace_id,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[Worker] 暂停时 user_states 写入失败(忽略) trace=%s: %s", trace_id, e)
    except Exception:
        pass
    logger.info("[Worker] 阶段边界暂停 trace=%s reason=%s stage=%s", trace_id, reason, stage)


async def worker_loop(concurrency: int = 1):
    """Worker 池:消费 queue:generate,运行 run_skill,把每个事件 publish 到对应进度流(持久化)。"""
    logger.info("[Worker] worker_loop 启动, concurrency=%s backend=%s", concurrency, type(get_queue()).__name__)
    q = get_queue()

    # 用 asyncio 任务池模拟并发 Worker
    async def _one():
        while True:
            try:
                job = await q.dequeue()
                t_job = time.time()
                logger.info("[Worker] [1/6] 从队列取出任务 trace=%s", job.get("trace_id"))
                # 统计: AI 核心总生成请求数(反映真实负载, 独立于编排统计)
                await record_generate_request()
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
                version = job.get("version")                     # v0.9.x: 产物版本号(COS 版本化)
                doc = job.get("requirement_doc")
                # 需求文档统计(满足"新增功能必接统计"约定): 仅当本次确由 requirement_agent
                # 产出了文档(job 传入非空前)时记一次成功, 并从正文粗算页面数/功能数供均值展示。
                if isinstance(doc, (str, dict)) and doc:
                    try:
                        doc_text = doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False)
                        _pages = doc_text.count("```") // 2 + doc_text.count("页面") + doc_text.count("page")
                        _features = doc_text.count("功能") + doc_text.count("特性") + doc_text.count("feature")
                        await record_requirement_doc(
                            project_id or 0, True, max(_pages, 1), max(_features, 1)
                        )
                        logger.info("[Worker] [3/6] 需求文档统计已记入(异步尽力) trace=%s", trace_id)
                    except Exception as _rd_e:  # noqa: BLE001
                        logger.debug("[Worker] 需求文档统计失败(忽略): %s", _rd_e)
                proj_status = job.get("project_status", "draft")
                has_req_doc = bool(doc)  # v1.0.7: 是否已存在需求文档(决定工具路由是否放行建站)
                # D(#486): 上下文闸门——本会话是否已落地建站产物(repo="site" 的 Artifact)。
                # 命中站点词 + 非 delete/reset/纯闲聊 → 直路由 build_modify(网站迭代),
                # 解决"html 里按钮点不动/打不开"等追问进不了修改流程的问题。
                has_site_artifact = False
                try:
                    from ...repos.business_repos import artifact_repo as _art_repo
                    from ...db import SessionLocal as _Sess
                    from ...cache import ck_get
                    if conversation_id:
                        async with _Sess() as _s:
                            has_site_artifact = await _art_repo.exists_repo_for_conversation(
                                _s, conversation_id, repo="site")
                        # D(#486) 竞态加固: 已落地建站产物 == 已生成 Artifact(repo=site)
                        #   或 存在「await_confirm 建站计划断点」(方案已确认将生成站点)。
                        #   否则 #8/#11 类「建站→追问修改」同会话, 修改请求若早于上一条
                        #   建站 Artifact 落库即发, has_site_artifact=False → 误路由 agent_chat。
                        if not has_site_artifact:
                            try:
                                _ck = await ck_get(conversation_id)
                                if isinstance(_ck, dict) and (
                                    _ck.get("status") == "paused"
                                    and _ck.get("stage") == "await_confirm"
                                ):
                                    has_site_artifact = True
                                    logger.info("[Worker] [3/6] 上下文闸门: 命中await_confirm建站计划断点, "
                                                "视为已落站(竞态加固) conv=%s", conversation_id)
                            except Exception as _ck_e:  # noqa: BLE001
                                logger.debug("[Worker] 读 ck 失败(忽略) conv=%s: %s", conversation_id, _ck_e)
                except Exception as _art_e:  # noqa: BLE001
                    logger.debug("[Worker] 查 site artifact 失败(忽略) conv=%s: %s", conversation_id, _art_e)
                logger.info("[Worker] [3/6] 上下文闸门 has_site_artifact=%s conv=%s", has_site_artifact, conversation_id)
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
                # 精细日志:打印本次所有上下文内容(完整,不截断关键信息)
                _msg_preview = [
                    {"role": m.get("role"), "len": len(m.get("content", "") or "")}
                    for m in messages
                ]
                _doc_preview = (doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False)) if doc else ""
                logger.info(
                    "[Worker][3/6][上下文内容] user_id=%s project_id=%s version=%s "
                    "project_status=%s has_req_doc=%s\n"
                    "  ctx_hint=%s\n  summary=%s\n  project_system_prompt=%.300s\n"
                    "  project_constraints=%s\n  messages(role/len)=%s\n"
                    "  requirement_doc(长度=%d)=%.1500s",
                    user_id, project_id, version, proj_status, bool(doc),
                    ctx_hint or "无", summary or "无", proj_prompt[:300] if proj_prompt else "无",
                    proj_constraints or [], _msg_preview, len(_doc_preview), _doc_preview,
                )
                # ── [3.5] 澄清续跑(clarified 重发): 跳过意图分类, 复用已存意图直接路由 ──
                #    用户在前端 clarify 卡选完并确认后, 带 clarified=1 重发(答案作为 q 已随 messages 追加)。
                #    这里直接用首轮判定存的 pending_clarify 意图路由执行, 补齐槽位, 不走 2 轮对话。
                _clarified = bool(job.get("clarified", False))
                _skip_classify = False
                if _clarified and conversation_id:
                    _pc = get_pending_clarify(conversation_id)
                    if _pc:
                        _skip_classify = True
                        logger.info("[Worker] [3.5] 澄清续跑: 跳过意图分类, 复用已存意图 skill=%s (+%.0fms)",
                                    _pc.get("selected_skill"), (time.time() - t_job) * 1000)
                        intent = {
                            "level1": _pc.get("level1", "chat"),
                            "level2": _pc.get("level2", "casual"),
                            "confidence": float(_pc.get("confidence", 0.8)),
                            "industry": _pc.get("industry", "other"),
                            "decision": "route",
                            "selected_skill": _pc.get("selected_skill") or "agent_chat",
                            "risk_level": "low",
                            "requires_confirm": False,
                            "evidence": {},
                            "plan": [{"action": "route", "skill": _pc.get("selected_skill"),
                                      "confidence": float(_pc.get("confidence", 0.8)), "reason": "clarify_resume"}],
                            "sub_tasks": [],
                            "split_reason": "",
                            "clarify_questions": [],
                            "clarify_rounds": 0,
                        }
                        clear_pending_clarify(conversation_id)
                    else:
                        logger.warning("[Worker] [3.5] clarified 重发但无 pending_clarify(可能已过期), 退化为普通分类")

                # 意图分类 v2(5模块并行, 35s超时)
                if not _skip_classify:
                    if conversation_id:
                        clear_pending_clarify(conversation_id)  # 新一轮正常分类: 清除陈旧澄清态
                    try:
                        intent = await asyncio.wait_for(
                            detect_intent_v2(messages, model_id,
                                             conversation_id=conversation_id,
                                             context_hint=ctx_hint,
                                             project_status=proj_status,
                                             project_constraints=proj_constraints,
                                             user_id=user_id, project_id=project_id,
                                             has_requirement_doc=has_req_doc,
                                             has_site_artifact=has_site_artifact),
                            timeout=35.0,
                        )
                    except asyncio.TimeoutError:
                        logger.error("[Worker] [3/6] 意图分类超时(35s) → 降级")
                        intent = {"level1": "learn", "level2": "casual", "confidence": 0.3,
                                  "industry": "other", "checkpoint_relation": "none",
                                  "selected_skill": "agent_chat", "decision": "fallback"}
                    except Exception as e:  # 🔧 兜底: 非超时异常(如 classify 内部报错)也会静默杀死 Worker,
                        # 导致后续所有任务卡死且无日志。这里捕获并打全栈, 降级响应, 保证 Worker 不挂。
                        logger.exception("[Worker] [3/6] 意图分类异常(非超时) → 降级: %s", e)
                        intent = {"level1": "learn", "level2": "casual", "confidence": 0.3,
                                  "industry": "other", "checkpoint_relation": "none",
                                  "selected_skill": "agent_chat", "decision": "fallback"}
                ctx_result = ctx_hint or "检测完成"
                logger.info("[Worker] [3/6] 上下文结果 ctx=%.60s (+%.0fms, 含意图分类)", ctx_result,
                           (time.time() - t_job) * 1000)

                # ── [3.6] 3.3 多轮上下文连贯:语义召回本会话相关历史消息,注入为 system 上下文 ──
                #    即使前端只下发近期窗口,模型也能看到语义相关的历史片段,实现跨轮连贯。
                #    role=system 以免污染下游 qc_user_text(其按 role=='user' 取首个用户消息)。
                rel_ctx_msg = None
                if conversation_id:
                    try:
                        from ..knowledge.chroma import find_relevant_message_contents
                        _hist_q = ""
                        for _m in reversed(messages):
                            if _m.get("role") == "user":
                                _hist_q = (_m.get("content") or "")[:300]
                                break
                        if not _hist_q:
                            _hist_q = user_text
                        _hist = await asyncio.to_thread(
                            find_relevant_message_contents, _hist_q, conversation_id, 6)
                        if _hist:
                            _snip = "\n\n".join(f"- {h['content']}" for h in _hist)
                            rel_ctx_msg = {
                                "role": "system",
                                "content": f"【相关历史对话片段(来自本会话语义相似消息,供上下文连贯)】\n{_snip}",
                            }
                            logger.info("[Worker] [3.6] 注入历史上下文 conv=%s 条=%d 最高相似度=%.3f",
                                        conversation_id, len(_hist), max(h["score"] for h in _hist))
                    except Exception as _he:  # noqa: BLE001
                        logger.debug("[Worker] [3.6] 历史上下文召回失败(忽略): %s", _he)

                # ── [4/6] 意图分类(汇总器已算好最终 skill, 单一来源) ──
                decision = intent.get("decision", "route")
                confirmed = bool(job.get("confirmed", False))
                skill_name = skill or intent.get("selected_skill") or skill_for(intent["level1"], intent["level2"]) or "agent_chat"
                req_id = intent.get("request_id")
                logger.info("[Worker] [4/6] 决策 decision=%s risk=%s 汇总skill=%s 最终skill=%s conf=%.0f%% (+%.0fms)",
                           decision, intent.get("risk_level", "?"), intent.get("selected_skill"),
                           skill_name, intent.get("confidence", 0) * 100,
                           (time.time() - t_job) * 1000)

                # 🔧 断点续跑 / 强制技能(resume): 跳过意图决策门控, 直接路由执行。
                #    否则对续跑短消息(如"确认并开始生成网站")的 clarify/learn 决策会拦截,
                #    导致 checkpoint 注入后永不执行建站 → 无限 await_confirm 暂停循环。
                if (skill or job.get("checkpoint")) and decision != "block":
                    if decision != "route":
                        logger.info("[Worker] [4.5] 强制技能续跑: decision=%s → route (skill=%s, checkpoint=%s)",
                                    decision, skill_name, "有" if job.get("checkpoint") else "无")
                    decision = "route"

                # §9: 执行前计划预览(所有意图通用) —— 下发 plan_preview 事件, 前端渲染
                # 「执行计划」卡 + SOP 角色链路 badge(仅当 skill 归属 4 角色之一时)。
                # 多意图走 SubTaskTrack 自有 SOP 进度条, 此处不重复发, 避免双卡。
                if decision != "split":
                    try:
                        from ..roles.handoff import ROLE_FOR_SKILL
                        _role = ROLE_FOR_SKILL.get(skill_name)
                        if _role:
                            await q.publish(trace_id, {
                                "event": "plan_preview",
                                "data": {"title": "执行计划", "roles": [_role]},
                            })
                    except Exception as _pp_e:  # noqa: BLE001
                        logger.debug("[Worker] plan_preview 失败(忽略) trace=%s: %s", trace_id, _pp_e)

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
                    risk_level = (intent.get("plan") or [{}])[0].get("risk_level", "high")
                    logger.info("[Worker] [5/6] 二次确认 等待用户确认 skill=%s reason=%s risk=%s",
                                skill_name, reason, risk_level)
                    await q.publish(trace_id, {"event": "confirm", "data": {
                        "reason": reason, "skill": skill_name, "risk_level": risk_level}})
                    await q.publish(trace_id, {"event": "done", "data": {}})
                    continue

                # 3.5) 澄清(CLARIFY): 发 clarify 事件(含结构化选项) + 存 pending_clarify, 然后暂停流程
                #     等用户在前端卡片选完并确认(clarified=1 重发)后, 由 [3.5] 分支跳过分类直接路由。
                if decision == "clarify":
                    questions = intent.get("clarify_questions", [])
                    rounds = intent.get("clarify_rounds", 0)
                    options = intent.get("clarify_options") or []
                    multi = bool(intent.get("clarify_multi", False))
                    free_text_hint = intent.get("clarify_free_text_hint") or ""
                    logger.info("[Worker] [5/6] 澄清轮次=%d questions=%s options=%d multi=%s",
                                rounds, questions, len(options), multi)
                    # 存 pending_clarify(含已判定意图与结构化选项), 供 clarified 重发时复用
                    if conversation_id:
                        set_pending_clarify(conversation_id, {
                            "level1": intent.get("level1"),
                            "level2": intent.get("level2"),
                            "selected_skill": skill_name,
                            "confidence": intent.get("confidence", 0.6),
                            "industry": intent.get("industry", "other"),
                            "questions": questions,
                            "options": options,
                            "multi": multi,
                            "free_text_hint": free_text_hint,
                        })
                    await q.publish(trace_id, {"event": "clarify", "data": {
                        "questions": questions,
                        "rounds": rounds,
                        "options": options,
                        "multi": multi,
                        "free_text_hint": free_text_hint,
                    }})
                    await q.publish(trace_id, {"event": "done", "data": {}})
                    if req_id:
                        try:
                            from ..intent.observation import mark_outcome
                            mark_outcome(req_id, "clarified_sent")
                        except Exception:
                            pass
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
                        from ..roles.handoff import ROLE_ORCHESTRATOR_ENABLED
                        from ..roles.orchestrator import RoleOrchestrator

                        def _dict_to_subtask(d: dict) -> SubTask:
                            valid = {k: v for k, v in d.items() if k in SubTask.__dataclass_fields__}
                            return SubTask(**valid)

                        sub_tasks = [_dict_to_subtask(d) for d in sub_tasks_raw]
                        confirmed_subtasks = set(job.get("confirmed_subtasks") or [])
                        # 3.3: 多意图路径同样注入历史上下文(避免污染原 messages,复制后前置)
                        orch_messages = list(messages)
                        if rel_ctx_msg is not None:
                            orch_messages.insert(0, rel_ctx_msg)
                        shared_ctx = SharedContext(
                            requirement_doc=doc,
                            project_status={"status": proj_status},
                            conversation_summary=summary,
                            conversation_history=orch_messages,
                        )
                        # §4 角色编排层:默认开启(ROLE_ORCHESTRATOR_ENABLED),关闭则回退原生 Orchestrator
                        if ROLE_ORCHESTRATOR_ENABLED:
                            orch = RoleOrchestrator()
                            logger.info("[Worker] [5/6] 启用角色编排层 RoleOrchestrator(SOP:产品→设计→开发→评审)")
                        else:
                            orch = Orchestrator()
                            logger.info("[Worker] [5/6] 角色编排层已关闭,回退原生 Orchestrator")
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
                        review_needs = False
                        paused = False
                        last_stage: str | None = None
                        last_ck: tuple | None = None
                        async for event in orch.execute(
                            sub_tasks, model_id, orch_messages,
                            trace_id=trace_id, is_cancelled=_cancelled,
                            confirmed_subtasks=confirmed_subtasks,
                            shared_ctx=shared_ctx,
                            original_query=user_text,
                            project_system_prompt=proj_prompt,
                            project_constraints=proj_constraints,
                            # 断点续跑(G5): 多意图拆分后, 顶层 resume 的 checkpoint(建站子任务的
                            # await_confirm 计划)必须下传到子任务 skill, 否则 sub_0(generate_site)
                            # 收不到、会重新走需求确认再次 paused, 永远产不出 preview。
                            # 非建站子任务(如天气)忽略该参数, 无副作用。
                            checkpoint=job.get("checkpoint"),
                            resume_mode=job.get("resume_mode", "resume"),
                        ):
                            if event.get("event") == "done":
                                done_event = event
                                continue
                            if event.get("event") == "token":
                                data = event.get("data", "")
                                if isinstance(data, str):
                                    qc_assistant_buf.append(data)
                            # 捕获 reviewer 评审结果(7维+needs_review), 供后置 QC 按需触发(任一子任务需复核即触发)
                            if event.get("event") == "review":
                                _rev = event.get("data") or {}
                                if _rev.get("needs_review"):
                                    review_needs = True
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
                            # ── v4 阶段边界暂停检测(断连即暂停 / 手动停止) ──
                            _ev = event.get("event")
                            _d = event.get("data") or {}
                            if _ev == "node" and isinstance(_d, dict):
                                last_stage = _d.get("stage") or last_stage
                            elif _ev == "checkpoint" and isinstance(_d, dict):
                                _ck_stage = _d.get("stage", "?")
                                last_ck = (_ck_stage, _d.get("data", {}),
                                          _PAUSE_STAGE_PROGRESS.get(_ck_stage, 50))
                            pr = await q.is_paused(trace_id)
                            if pr:
                                await _worker_handle_pause(
                                    trace_id, conversation_id, user_id, pr, last_stage, last_ck)
                                paused = True
                                break
                        if paused:
                            continue
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
                        # 后置 QC(合并文本, 按需触发)
                        # 编排场景: 仅当任一子任务 reviewer 标记 needs_review 才跑单裁判 QC(省 LLM 成本)。
                        qc_assistant_text = "".join(qc_assistant_buf)
                        qc_result = None
                        # 决策日志: 编排场景 QC 仅当子任务 reviewer 标 needs_review 才触发
                        if not qc_assistant_text.strip() or done_event is None:
                            logger.debug("[Worker] [6/6] 编排 QC 跳过 trace=%s (无合并文本或未收到 done)", trace_id)
                        elif review_needs:
                            logger.info("[Worker] [6/6] 编排 QC 触发 trace=%s 原因=子任务reviewer待复核", trace_id)
                        else:
                            logger.debug("[Worker] [6/6] 编排 QC 跳过 trace=%s 原因=无子任务待复核", trace_id)
                        if qc_assistant_text.strip() and done_event is not None and review_needs:
                            try:
                                from ..qc import run_qc
                                from .safety import run_safety
                                safety_risk = run_safety(messages, project_constraints).risk_level
                                qc_result = await asyncio.wait_for(
                                    run_qc(qc_user_text, qc_assistant_text,
                                           project_constraints=project_constraints,
                                           safety_risk=safety_risk,
                                           model_id=model_id),
                                    timeout=settings.qc_timeout_seconds,
                                )
                                await q.publish(trace_id, {"event": "qc", "data": qc_result})
                                # 补齐 QC 落库(逐条留痕, 供报表/雷达图统计)
                                asyncio.create_task(
                                    _persist_qc_score(trace_id, model_id, conversation_id, qc_result)
                                )
                            except Exception as qc_err:  # noqa: BLE001
                                logger.warning("[Worker] [6/6] 编排 QC 失败(跳过) trace=%s: %s", trace_id, qc_err)
                        if done_event is not None:
                            await q.publish(trace_id, done_event)
                        logger.info("[Worker] [6/6] 编排执行完毕 trace=%s 共%d事件", trace_id, event_cnt)
                        await _commit_after_done(trace_id, "orchestrator", user_text)
                        continue

                # 5) 正常路由 / fallback / 已确认 / 已选项 → 直接执行
                # §方案B P0: 单意图也进入角色编排层(上下文隔离 + 强交接物捕获),
                # 与多意图共用同一套角色注入逻辑,消除"单/多两套逻辑"观感。
                try:
                    from ..roles.handoff import ROLE_ORCHESTRATOR_ENABLED as _ROE, map_skill_to_role as _m2r
                    from ..roles.orchestrator import get_role_agent as _get_agent
                    _role = _m2r(skill_name) if _ROE else None
                    _agent = _get_agent(_role) if _role else None
                    _enriched_messages = _agent.inject_context(messages, None) if _agent else list(messages)
                    # 3.3: 注入历史上下文(role=system, 前置), 多轮对话连贯真正生效
                    if rel_ctx_msg is not None:
                        _enriched_messages.insert(0, rel_ctx_msg)
                    if _agent is not None:
                        logger.info("[Worker] [5/6] 单意图启用角色上下文 role=%s skill=%s", _agent.label, skill_name)
                except Exception as _re_e:  # noqa: BLE001
                    logger.debug("[Worker] 角色上下文注入失败(忽略,降级直跑) skill=%s: %s", skill_name, _re_e)
                    _agent = None
                    _enriched_messages = messages
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
                artifacts: list[str] = []
                done_event: dict | None = None
                terminal_seen = False  # 技能是否显式产出了终止事件(done/paused/aborted/error/unsupported)
                review_needs = False
                # 生成阶段耗时统计: 记录进入各阶段的时间戳(供 record_gen_stage 算时长)
                _stage_enter: dict[str, float] = {}
                paused = False
                last_stage: str | None = None
                last_ck: tuple | None = None
                async for event in run_skill(
                    skill_name, model_id, _enriched_messages,
                    trace_id=trace_id, is_cancelled=_cancelled,
                    intent_info=intent,
                    requirement_doc=doc,
                    project_status=proj_status,
                    conversation_summary=summary,
                    project_system_prompt=proj_prompt,
                    project_constraints=proj_constraints,
                    version=version, user_id=user_id, project_id=project_id,
                    checkpoint=job.get("checkpoint"),      # 断点续跑: 透传至 handler 跳过已完成阶段
                    resume_mode=job.get("resume_mode", "resume"),
                    confirmed=confirmed,                   # 透传确认标志(agent_delete 等依赖此字段)
                    site_generated=job.get("site_generated", False),
                ):
                    # 拦截 done: 先发 QC 再发 done(QC 在 done 前, 不阻塞前端 done 渲染)
                    _ev_name = event.get("event")
                    if _ev_name in ("done", "paused", "aborted", "error", "unsupported"):
                        terminal_seen = True
                    if _ev_name == "done":
                        done_event = event
                        continue
                    if event.get("event") == "review":
                        _rev = event.get("data") or {}
                        if _rev.get("needs_review"):
                            review_needs = True
                        # 不 continue: review 卡片需透传给前端展示(7维评分)
                    if event.get("event") == "token":
                        data = event.get("data", "")
                        if isinstance(data, str):
                            qc_assistant_buf.append(data)
                    if event.get("event") == "preview" and isinstance(event.get("data"), dict):
                        _pu = event["data"].get("url")
                        if _pu:
                            artifacts.append(_pu)
                    # 生成阶段进入埋点: node(stage=enter_planner|enter_coder|enter_reviewer|previewing)
                    # 仅记录「首次进入该阶段」的耗时基准, 阶段耗时统计走 record_gen_stage。
                    if event.get("event") == "node" and skill_name in ("agent_build", "agent_generate_site"):
                        d = event.get("data") or {}
                        stg = d.get("stage")
                        if stg in ("enter_planner", "enter_coder", "enter_reviewer", "previewing") and stg not in _stage_enter:
                            _stage_enter[stg] = time.time()
                    await q.publish(trace_id, event)
                    event_cnt += 1
                    # ── v4 阶段边界暂停检测(断连即暂停 / 手动停止) ──
                    _ev = event.get("event")
                    _d = event.get("data") or {}
                    if _ev == "node" and isinstance(_d, dict):
                        last_stage = _d.get("stage") or last_stage
                    elif _ev == "checkpoint" and isinstance(_d, dict):
                        _ck_stage = _d.get("stage", "?")
                        last_ck = (_ck_stage, _d.get("data", {}),
                                  _PAUSE_STAGE_PROGRESS.get(_ck_stage, 50))
                    pr2 = await q.is_paused(trace_id)
                    if pr2:
                        await _worker_handle_pause(
                            trace_id, conversation_id, user_id, pr2, last_stage, last_ck)
                        paused = True
                        break
                if paused:
                    continue
                # §方案B P0: 单意图角色强交接物捕获 + 入参/出参日志(与多意图对齐)
                if _agent is not None:
                    try:
                        _hf = _agent.capture_handoff(qc_assistant_text, artifacts)
                        if _hf is not None:
                            _agent.log_io(
                                skill_name, model_id,
                                input_summary=f"single_intent skill={skill_name}",
                                status="done",
                                output_summary=_hf.summary[:120],
                                duration_ms=int((time.time() - t_job) * 1000),
                            )
                    except Exception as _hf_e:  # noqa: BLE001
                        logger.debug("[Worker] 单意图交接物捕获失败(忽略): %s", _hf_e)
                # ── [6/6] 后置 QC 单裁判(按需触发) ──
                # 生成类技能: 仅当 reviewer 标记 needs_review 才跑单裁判(省 LLM 成本); 闲聊强制兜底。
                # 闲聊(agent_chat) 无 reviewer, 始终 QC 兜底以保证对话质量 + 支撑低分重答(Phase D)。
                qc_assistant_text = "".join(qc_assistant_buf)
                qc_result = None
                force_qc = skill_name == "agent_chat"
                # 决策日志: 明确 QC 是否触发 + 原因(可追溯, 便于复盘「为什么这次没跑三裁判」)
                if not qc_assistant_text.strip() or done_event is None:
                    logger.debug("[Worker] [6/6] QC 跳过 trace=%s (无助手文本或未收到 done)", trace_id)
                elif review_needs:
                    logger.info("[Worker] [6/6] QC 触发 trace=%s 原因=reviewer待复核", trace_id)
                elif force_qc:
                    logger.info("[Worker] [6/6] QC 触发 trace=%s 原因=闲聊强制兜底", trace_id)
                else:
                    logger.debug("[Worker] [6/6] QC 跳过 trace=%s 原因=reviewer已通过(按需不复核)", trace_id)
                if qc_assistant_text.strip() and done_event is not None and (review_needs or force_qc):
                    try:
                        from ..qc import run_qc
                        from .safety import run_safety
                        safety_risk = run_safety(messages, project_constraints).risk_level
                        qc_result = await asyncio.wait_for(
                            run_qc(qc_user_text, qc_assistant_text,
                                   project_constraints=project_constraints,
                                   safety_risk=safety_risk,
                                   model_id=model_id),
                            timeout=settings.qc_timeout_seconds,
                        )
                        await q.publish(trace_id, {"event": "qc", "data": qc_result})
                        # 补齐 QC 落库(逐条留痕, 供报表/雷达图统计)
                        asyncio.create_task(
                            _persist_qc_score(trace_id, model_id, conversation_id, qc_result)
                        )
                        logger.info("[Worker] [6/6] QC 完成 trace=%s overall=%.2f needs_review=%s partial=%s",
                                   trace_id, qc_result.get("overall", 0),
                                   qc_result.get("needs_review"), qc_result.get("partial"))
                        # ── 质量闭环(v1.2.4): needs_review → agent_review → 重打分 ──
                        # 仅建站类技能(agent_build/agent_generate_site)生效; 闭环内重写盘+重传COS,
                        # 后续 _commit_after_done 与预览均为修复版。闲聊走自有 Phase D, 不在此触发。
                        if qc_result.get("needs_review"):
                            try:
                                fixed, fix_applied, fix_rounds = await _qc_fix_loop(
                                    trace_id, q, qc_user_text, qc_assistant_text, model_id,
                                    project_constraints, version, user_id, project_id,
                                    skill_name, _cancelled,
                                )
                                if fix_applied and fixed is not None:
                                    qc_result = dict(fixed)
                                    qc_result["fix_applied"] = True
                                    qc_result["fix_rounds"] = fix_rounds
                                    await q.publish(trace_id, {"event": "qc", "data": qc_result})
                                    # 修复收敛后的最终 QC 也要落库(覆盖原记录, upsert 幂等)
                                    asyncio.create_task(
                                        _persist_qc_score(trace_id, model_id, conversation_id, qc_result)
                                    )
                                    logger.info("[质量闭环] 自动修复完成 trace=%s rounds=%d overall=%.2f",
                                               trace_id, fix_rounds, qc_result.get("overall", 0))
                            except Exception as _fe:  # noqa: BLE001
                                logger.warning("[质量闭环] 触发异常(忽略) trace=%s: %s", trace_id, _fe)
                    except Exception as qc_err:  # noqa: BLE001
                        logger.warning("[Worker] [6/6] QC 执行失败(已跳过, 不影响主流程) trace=%s: %s",
                                       trace_id, qc_err)
                # Phase D(v0.9.0): 闲聊低分→1轮轻量重答
                if qc_result is not None and skill_name == "agent_chat" and qc_assistant_text.strip():
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
                # L2 对话精炼(v0.9.0): done 前 LLM 去冗余 → 改写 Message.content。
                # 注: agent_build / agent_generate_site 已由 skill 自身在收尾时 emit 结构化
                # 'refined'(本次生成结果汇总, Markdown), 此处若再用流式 HTML 精炼会覆盖掉它,
                # 故仅对产出自然语言正文的技能(agent_chat / orchestrator)启用通用精炼。
                # 必须在 done 之前发布: 前端收到 done 即关闭 SSE, refined 才能被消费
                if skill_name in ("agent_chat", "orchestrator") and qc_assistant_text.strip():
                    try:
                        refined = await _refine_assistant_dialog(qc_assistant_text)
                        await q.publish(trace_id, {"event": "refined", "data": refined[:500]})
                        logger.info("[Worker] L2 精炼完成 trace=%s len_before=%d len_after=%d",
                                   trace_id, len(qc_assistant_text), len(refined))
                        record_refine(len(qc_assistant_text), len(refined))  # v0.9.0 统计
                    except Exception as _le:  # noqa: BLE001
                        logger.debug("[Worker] L2 精炼失败: %s", _le)
                if done_event is not None:
                    await q.publish(trace_id, done_event)
                elif not terminal_seen:
                    # 兜底收尾契约: 技能未显式产出任何终止事件(如 requirement_agent 在
                    # 'options' 分支直接 return, 等待用户选择方案)→ 必须补发 done, 否则前端
                    # generating 永远为 true、停止按钮常驻、用户无法继续输入。属后端收尾契约缺口修复。
                    logger.info("[Worker] [6/6] 兜底补发 done(技能未显式终止) trace=%s skill=%s",
                                trace_id, skill_name)
                    await q.publish(trace_id, {"event": "done", "data": {}})
                if req_id:
                    try:
                        from ..intent.observation import mark_outcome
                        mark_outcome(req_id, "executed")
                    except Exception:
                        pass
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
                # [7/7] ── 兜底落库: 无论 SSE 客户端是否还活着, Worker 侧直接保存对话消息到 DB。
                #   解决「客户端断开→_do_persist 缺失→刷新后对话空白」的问题(与 publisher finally 互补, upsert 幂等无重复风险)
                if qc_assistant_text.strip():
                    try:
                        _conv_id = job.get("conversation_id"); _uid = job.get("user_id")
                        if _conv_id and _uid:
                            from ...proxy import _persist_worker_result
                            await _persist_worker_result(
                                trace_id=trace_id,
                                conversation_id=_conv_id,
                                user_id=_uid,
                                model_id=model_id,
                                user_text=qc_user_text,
                                assistant_text=qc_assistant_text,
                                terminal_status=done_event.get("event") == "paused" and "paused" or "done",
                                skill_name=skill_name,
                            )
                            logger.info("[Worker] [7/7] 兜底落库完成 trace=%s chars=%d", trace_id, len(qc_assistant_text))
                    except Exception as _pe:  # noqa: BLE001
                        logger.warning("[Worker] [7/7] 兜底落库失败(忽略) trace=%s: %s", trace_id, _pe)
                # 统计: 单 skill 路径成效(成功/失败/中断 + 耗时), 供「系统分析」skill 维度成功率
                try:
                    _ok = done_event is not None and not (qc_result and qc_result.get("error"))
                    await record_skill_outcome(
                        skill_name, "ok" if _ok else "fail", (time.time() - t_job) * 1000
                    )
                except Exception as _soe:  # noqa: BLE001
                    logger.debug("[Worker] skill_outcome 统计失败(忽略): %s", _soe)
                # 统计: 生成各阶段耗时(建站/生成站点类技能), 供「系统分析」生成链路耗时分布
                try:
                    for _s, _t in _stage_enter.items():
                        await record_gen_stage(_s, (time.time() - _t) * 1000)
                except Exception as _gse:  # noqa: BLE001
                    logger.debug("[Worker] gen_stage 统计失败(忽略): %s", _gse)
                try:
                    await _commit_after_done(trace_id, skill_name, qc_user_text)
                except Exception:
                    pass  # chat类skill无qc_user_text,跳过
            except Exception as e:
                logger.error("[Worker] 执行异常 trace=%s skill=%s 错误=%s: %s",
                            trace_id, skill_name, type(e).__name__, e)
                await q.publish(trace_id, {"event": "error", "data": str(e)})
                await q.publish(trace_id, {"event": "done", "data": {}})
                if req_id:
                    try:
                        from ..intent.observation import mark_outcome
                        mark_outcome(req_id, "error")
                    except Exception:
                        pass

    await asyncio.gather(*[_one() for _ in range(concurrency)])
