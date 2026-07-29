"""Tools 包:导入即注册(§5.9 来源 A 内置工具)。

每个模块用 @tool 装饰器,在导入时把函数注册进 ToolRegistry。
新增内置工具:在此文件加一行 `from . import xxx` 即可(开闭原则)。
重依赖(chromadb/cos/playwright)均函数内懒加载,缺包也不影响包导入与注册。
"""

from __future__ import annotations

import itertools
import logging
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("ai_service.tools")

from . import (
    browser_screenshot,
    cos_upload,
    fetch_url,
    file_io,
    html_validate,
    image_generate,
    rag_retrieve,
    web_search,
)


__all__ = [
    "file_io",
    "fetch_url",
    "web_search",
    "html_validate",
    "rag_retrieve",
    "cos_upload",
    "browser_screenshot",
    "image_generate",
    "ToolEventBus",
    "get_tool_bus",
    "enter_trace_scope",
    "exit_trace_scope",
    "emit_tool_call",
    "emit_tool_result",
    "emit_reasoning",
]


# ──────────────────────────────────────────────────────────────────────────
# ToolEventBus(Phase 1): 让工具调用对前端"可见"(WorkBuddy 式 think→call→observe 循环)。
#
# 问题: 现有 9 个工具被 skill 内部直接调用, sync 工具还经 asyncio.to_thread 跑在别的线程,
# 无法用 async generator yield 透出事件。故用进程内「trace + 作用域栈」总线:
#   - skill 入口在 run_skill 里 enter(trace_id, sink), 工具 emit 时按 *当前线程登记的作用域*
#     路由到对应 sink(子任务/sub_task_id 隔离);
#   - 多意图同一 trace 内并发子任务共享线程池时, 每个 run_skill 调用 enter 自己的 scope,
#     工具事件始终落到发起它的那个 skill 作用域(而非全局 trace 单一 sink), 杜绝串味;
#   - 无作用域(离线/脚本)时 emit 静默降级, 不影响主链路。
# 作用域用 threading.local 栈实现: 进入 skill 时 push, 退出 pop; await 边界由 run_skill
# 在同一协程内管理(不跨 await 切换线程, 故线程局部栈稳定)。
# ──────────────────────────────────────────────────────────────────────────
class ToolEventBus:
    """进程内, 按 trace_id + 线程作用域栈路由工具事件。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # trace_id -> { scope_id: sink }  (支持同一 trace 内多并发作用域)
        self._scopes: Dict[str, Dict[int, Callable[[Dict[str, Any]], None]]] = {}
        self._seq = itertools.count(1)
        self._local = threading.local()

    def enter(self, trace_id: str, scope_id: int, sink: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._scopes.setdefault(trace_id, {})[scope_id] = sink
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        stack.append((trace_id, scope_id))
        logger.debug("[ToolBus] enter trace=%s scope=%s", trace_id, scope_id)

    def exit(self, trace_id: str, scope_id: int) -> None:
        with self._lock:
            sc = self._scopes.get(trace_id)
            if sc is not None:
                sc.pop(scope_id, None)
                if not sc:
                    self._scopes.pop(trace_id, None)
        stack = getattr(self._local, "stack", None)
        if stack:
            # 弹出栈顶匹配项(不强制末尾, 兼容嵌套)
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == (trace_id, scope_id):
                    stack.pop(i)
                    break
        logger.debug("[ToolBus] exit trace=%s scope=%s", trace_id, scope_id)

    def _current_scope(self, trace_id: str) -> Optional[Callable[[Dict[str, Any]], None]]:
        """取当前线程栈顶匹配 trace 的作用域 sink。"""
        stack = getattr(self._local, "stack", None)
        if stack:
            for (tt, sc) in reversed(stack):
                if tt == trace_id and sc is not None:
                    with self._lock:
                        sink = self._scopes.get(tt, {}).get(sc)
                    if sink is not None:
                        return sink
        # 兜底: 无栈顶但 trace 有唯一作用域时用它(单意图场景)
        with self._lock:
            sc = self._scopes.get(trace_id)
            if sc and len(sc) == 1:
                return next(iter(sc.values()))
        return None

    def publish(self, trace_id: str, event: Dict[str, Any]) -> None:
        if not trace_id:
            return
        sink = self._current_scope(trace_id)
        if sink is None:
            # 诊断: 无作用域(离线/脚本 / enter 未生效) → 静默降级, 打 WARNING 暴露
            with self._lock:
                _sc = self._scopes.get(trace_id)
            logger.warning(
                "[ToolBus] DROP(无作用域) trace=%s event=%s scopes_has_trace=%s stack=%s",
                trace_id, event.get("event"),
                bool(_sc), getattr(self._local, "stack", None),
            )
            return
        try:
            sink(event)
        except Exception as e:  # noqa: BLE001
            logger.debug("[ToolBus] sink 异常(忽略) trace=%s: %s", trace_id, e)

    def next_id(self, prefix: str = "tc") -> str:
        return f"{prefix}_{next(self._seq)}"


# 进程单例(同一 uvicorn worker 内共享)
_TOOL_BUS = ToolEventBus()


def get_tool_bus() -> ToolEventBus:
    return _TOOL_BUS


def enter_trace_scope(trace_id: str, scope_id: int, sink: Callable[[Dict[str, Any]], None]) -> None:
    """run_skill 调用: 为该次执行登记一个工具事件作用域(子任务级隔离)。"""
    _TOOL_BUS.enter(trace_id, scope_id, sink)


def exit_trace_scope(trace_id: str, scope_id: int) -> None:
    """run_skill 收尾: 注销作用域。"""
    _TOOL_BUS.exit(trace_id, scope_id)


def _ev(event: str, **data: Any) -> Dict[str, Any]:
    return {"event": event, "data": data}


def emit_tool_call(trace_id: str, name: str, args: Dict[str, Any]) -> str:
    """工具调用开始: 返回 tool_call_id 供 emit_tool_result 回粘。"""
    tc_id = _TOOL_BUS.next_id()
    _TOOL_BUS.publish(
        trace_id,
        _ev("tool_call", tool_call_id=tc_id, name=name, args=args),
    )
    return tc_id


def emit_tool_result(
    trace_id: str, tool_call_id: str, name: str, ok: bool, summary: str
) -> None:
    """工具调用结果(默认前端折叠)。"""
    _TOOL_BUS.publish(
        trace_id,
        _ev("tool_result", tool_call_id=tool_call_id, name=name, ok=ok, summary=summary),
    )


def emit_reasoning(trace_id: str, text: str) -> None:
    """结构化思考/打算(非流式), 让前端显示"我在想什么"。"""
    _TOOL_BUS.publish(trace_id, _ev("reasoning", text=text))
