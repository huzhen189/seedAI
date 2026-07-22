"""多意图 DAG 编排执行器(§多意图 v1.0)。

职责:
- 把 SubTask[] 按 dependencies 拓扑排序成执行层(layers)
- 层内并行(asyncio.gather + 队列流式), 层间串行
- 每个子任务: 风险门控 → 上下文补全 → run_skill → 包装事件(sub_task_id 贯穿)
- 部分失败交付: 成功子任务照常产出, 失败子任务记录原因不阻断其余
- 全部完成后调用 ResultMerger 合并为连贯中文回复

对外: Orchestrator.execute() 为 async 生成器, 逐事件 yield(供 worker 透传 SSE)。
注意: 编排器不.emit done —— done 由 worker 在 QC 之后统一发送(与单 skill 路径一致)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from ..events import ev
from ..core.models import (
    RISK_HIGH,
    RISK_MEDIUM,
    RISK_LOW,
    SUB_BLOCKED,
    SUB_DONE,
    SUB_FAILED,
    SUB_PENDING,
    SUB_RUNNING,
    SUB_SKIPPED,
    OrchestratorResult,
    SharedContext,
    SubTask,
    SubTaskResult,
)
from ..core.runner import run_skill
from .merger import ResultMerger

logger = logging.getLogger("ai_service.orchestrator")


def build_layers(sub_tasks: list[SubTask]) -> list[list[SubTask]]:
    """拓扑排序成执行层(Kahn 风格): 同层内子任务互相独立, 可并行。"""
    placed: set[str] = set()
    remaining = list(sub_tasks)
    layers: list[list[SubTask]] = []
    # guard 防死循环: 正常 DAG 最多 len(sub_tasks) 轮即可排空, +5 留余量防御异常
    guard = 0
    while remaining and guard < len(sub_tasks) + 5:
        guard += 1
        layer = [s for s in remaining if all(d in placed for d in s.dependencies)]
        if not layer:
            # 兜底: 出现环依赖或依赖指向不存在的子任务时, 没有任何子任务满足
            # "依赖均已放置", 若不处理会无限空转 → 把剩余全部并入当前层, 保证一定能结束
            layer = remaining[:]
        layers.append(layer)
        for s in layer:
            placed.add(s.id)
        remaining = [s for s in remaining if s.id not in placed]
    return layers


class Orchestrator:
    """子任务 DAG 执行器。"""

    def __init__(self, merger: Optional[ResultMerger] = None):
        self.merger = merger or ResultMerger()

    async def execute(
        self,
        sub_tasks: list[SubTask],
        model_id: str,
        messages: list[dict],
        *,
        trace_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        confirmed_subtasks: Optional[set[str]] = None,
        shared_ctx: Optional[SharedContext] = None,
        original_query: str = "",
        **extra_kwargs,
    ) -> Any:
        """async 生成器: 逐事件 yield(sub_task_id 贯穿)。"""
        confirmed_subtasks = confirmed_subtasks or set()
        shared_ctx = shared_ctx or SharedContext()
        # 策略: 有依赖 → mixed(分层串行+层内并行), 无依赖 → parallel(全并行)
        has_dep = any(s.dependencies for s in sub_tasks)
        strategy = "mixed" if has_dep else "parallel"

        # 总览事件
        yield ev(
            "orchestration",
            total=len(sub_tasks),
            strategy=strategy,
            tasks=[
                {
                    "id": s.id,
                    "goal": s.goal,
                    "skill": s.selected_skill,
                    "risk": s.risk_level,
                    "status": s.status,
                    "dependencies": s.dependencies,
                }
                for s in sub_tasks
            ],
        )

        layers = build_layers(sub_tasks)
        logger.info("[编排] 开始执行 策略=%s 层数=%d 子任务=%d",
                    strategy, len(layers), len(sub_tasks))
        results: list[SubTaskResult] = []

        for layer_idx, layer in enumerate(layers):
            # 层内并行; 记录本层范围便于排查卡层/慢子任务
            logger.info("[编排] 执行层 #%d/%d 并行=%d: %s",
                        layer_idx + 1, len(layers), len(layer), [s.id for s in layer])
            # 层中每个子任务发 start
            for st in layer:
                st.status = SUB_RUNNING
                yield ev(
                    "subtask_start",
                    sub_task_id=st.id,
                    goal=st.goal,
                    skill=st.selected_skill,
                    risk=st.risk_level,
                    layer=layer_idx,
                )

            # 层内并行执行(事件经队列流式合并)
            q: asyncio.Queue = asyncio.Queue()
            tasks = [
                asyncio.create_task(
                    self._run_one(
                        st, q.put, model_id, messages, trace_id,
                        is_cancelled, shared_ctx, confirmed_subtasks, **extra_kwargs,
                    )
                )
                for st in layer
            ]

            # 并发归并模式: 把「队列取事件协程(getter)」与「本层所有子任务协程」一起交给
            # asyncio.wait(FIRST_COMPLETED)。任一子任务通过 sink(q.put) 产出的事件会被
            # getter 立即取出并 yield, 前端因而能实时看到各子任务的流式进度(而非整层跑完
            # 才吐出)。所有子任务结束后 getter 不再有产出 → 被取消, 退出循环。
            pending = set(tasks)
            while pending:
                getter = asyncio.ensure_future(q.get())
                done, pending = await asyncio.wait(
                    pending | {getter}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    item = getter.result()
                    if item is None:
                        continue
                    yield item
                else:
                    getter.cancel()
            # 子任务全部结束 → 收尾各协程的 SubTaskResult(供最终合并), 并排空队列残留事件
            for t in tasks:
                r = await t
                results.append(r)
            while not q.empty():
                item = q.get_nowait()
                if item is not None:
                    yield item

        # 全部子任务完成 → 合并为连贯回复
        success = [r for r in results if r.status == SUB_DONE]
        failed = [r for r in results if r.status != SUB_DONE]

        merged_text = ""
        try:
            merged_text = await self.merger.merge(
                results, original_query, model_id=model_id
            )
        except Exception as e:
            logger.warning("[编排] 合并失败, 降级拼接: %s", e)
            merged_text = self.merger._fallback_concat(results)

        yield ev(
            "merge",
            success_count=len(success),
            fail_count=len(failed),
            failed_tasks=[{"id": r.id, "goal": r.goal, "error": r.error} for r in failed],
            text=merged_text,
        )
        # 合并文本作为 token 流(供前端气泡 + QC 落库)
        yield ev("token", data=merged_text, sub_task_id="__merge__")

        orch_result = OrchestratorResult(
            success_results=success, failed_results=failed,
            merged_text=merged_text, strategy=strategy,
        )
        logger.info(
            "[编排] 完成 trace=%s 总=%d 成功=%d 失败=%d 部分交付=%s",
            trace_id, orch_result.total, len(success), len(failed), orch_result.partial_delivery,
        )

    async def _run_one(
        self,
        st: SubTask,
        sink: Callable[[dict], None],
        model_id: str,
        base_messages: list[dict],
        trace_id: Optional[str],
        is_cancelled: Optional[Callable[[], bool]],
        shared_ctx: SharedContext,
        confirmed_subtasks: set[str],
        **extra_kwargs,
    ) -> SubTaskResult:
        """执行单个子任务, 事件经 sink 推送, 返回 SubTaskResult。"""
        t0 = time.time()

        # ── 风险门控(兜底 5: 风险分级) ──
        # HIGH: 死红线, 系统直接拒绝, 即便用户后续确认也不可绕过(返回 SUB_BLOCKED)
        if st.risk_level == RISK_HIGH:
            sink(ev("subtask_fail", sub_task_id=st.id, reason="高风险操作不予执行(系统拒绝)", recoverable=False))
            return SubTaskResult(
                id=st.id, status=SUB_BLOCKED, skill=st.selected_skill, goal=st.goal,
                error="高风险拦截", risk_level=st.risk_level,
            )
        # MEDIUM: 需用户二次确认; 未确认则跳过(返回 SUB_SKIPPED)并等前端回传,
        #         confirmed_subtasks 携带已确认 id 重发时, 此处放行执行
        if st.risk_level == RISK_MEDIUM and st.id not in confirmed_subtasks:
            sink(ev("subtask_fail", sub_task_id=st.id,
                    reason="中风险操作需用户确认(回复确认后重发)",
                    recoverable=True))
            return SubTaskResult(
                id=st.id, status=SUB_SKIPPED, skill=st.selected_skill, goal=st.goal,
                error="中风险待确认", risk_level=st.risk_level,
            )

        # ── 上下文补全(兜底 3: 子任务自洽) ──
        enriched = self._enrich(st, base_messages, shared_ctx)

        out_buf: list[str] = []
        artifacts: list[str] = []
        intent_info = {
            "level1": st.level1,
            "level2": st.level2,
            "confidence": 0.9,
            "industry": st.industry,
            "decision": "route",
            "selected_skill": st.selected_skill,
            "risk_level": st.risk_level,
        }

        try:
            async for item in run_skill(
                st.selected_skill, model_id, enriched,
                trace_id=trace_id, is_cancelled=is_cancelled,
                intent_info=intent_info,
                requirement_doc=shared_ctx.requirement_doc,
                project_status=shared_ctx.project_status.get("status", "draft") if isinstance(shared_ctx.project_status, dict) else "draft",
                conversation_summary=shared_ctx.conversation_summary,
                **extra_kwargs,
            ):
                ev_name = item.get("event")
                # 编排层已给概览, 内部 intent 丢弃; done 由编排器控制
                if ev_name in ("intent", "done"):
                    continue
                item.setdefault("sub_task_id", st.id)
                sink(item)
                if ev_name == "token":
                    data = item.get("data", "")
                    if isinstance(data, str):
                        out_buf.append(data)
                if ev_name == "preview" and isinstance(item.get("data"), dict):
                    url = item["data"].get("url")
                    if url:
                        artifacts.append(url)
            if await _cancelled_now(is_cancelled):
                sink(ev("subtask_fail", sub_task_id=st.id, reason="用户取消", recoverable=True))
                return SubTaskResult(
                    id=st.id, status=SUB_FAILED, skill=st.selected_skill, goal=st.goal,
                    error="用户取消", risk_level=st.risk_level, duration_ms=int((time.time() - t0) * 1000),
                )

            st.status = SUB_DONE
            sink(ev("subtask_done", sub_task_id=st.id,
                    result_summary="".join(out_buf)[:200],
                    artifacts=artifacts))
            # 注册产出到共享上下文(供依赖方读取)
            shared_ctx.register_output(st.id, "".join(out_buf)[:2000])
            return SubTaskResult(
                id=st.id, status=SUB_DONE, skill=st.selected_skill, goal=st.goal,
                output_text="".join(out_buf), artifacts=artifacts,
                risk_level=st.risk_level, duration_ms=int((time.time() - t0) * 1000),
            )
        except Exception as e:
            logger.error("[编排] 子任务 %s 失败: %s", st.id, e)
            sink(ev("subtask_fail", sub_task_id=st.id, reason=f"执行异常: {e}", recoverable=True))
            return SubTaskResult(
                id=st.id, status=SUB_FAILED, skill=st.selected_skill, goal=st.goal,
                error=str(e), risk_level=st.risk_level, duration_ms=int((time.time() - t0) * 1000),
            )

    def _enrich(
        self,
        st: SubTask,
        base_messages: list[dict],
        shared_ctx: SharedContext,
    ) -> list[dict]:
        """为每个子任务组装自洽的执行上下文(兜底 3: 补齐上下文)。"""
        dep_text = shared_ctx.get_dep_outputs(st.dependencies)
        system_extra = (
            f"## 当前子任务(多意图计划的一部分)\n"
            f"目标: {st.goal}\n"
            f"你只需完成这一子任务, 不要越界做其他子任务。\n"
            f"你的产出将被合并进最终回复, 请保证输出自洽完整。\n"
        )
        if dep_text:
            system_extra += f"\n## 前置子任务产出(你可能需要参考)\n{dep_text}\n"
        if st.context_hint:
            system_extra += f"\n## 该子任务的专属上下文\n{st.context_hint}\n"

        enriched = list(base_messages)
        # 把子任务聚焦文本追加为用户消息(若 original_text 非空)
        if st.original_text:
            enriched = enriched + [{"role": "user", "content": f"[子任务聚焦] {st.original_text}"}]
        # system_extra 通过最后一条 system 注入比较难; 改为追加一条 user 系统指令
        enriched = enriched + [{"role": "system", "content": system_extra}]
        return enriched


async def _cancelled_now(fn) -> bool:
    if not fn:
        return False
    res = fn()
    if hasattr(res, "__await__"):
        return bool(await res)
    return bool(res)
