"""异步缓冲操作日志器（队列 + 周期落盘）。

用途
----
跟踪多步操作的执行过程，形成「顶层 operation → 子任务 subtask → 步骤 step」的树形记录。
典型场景：一次性大重构 / 批量清理 / 发布流程，需要可审计、可回放的操作轨迹。

机制（回答「日志 IO 是否有队列、间隔 10 秒异步填写」）
------------------------------------------------
- 所有写操作（`subtask` / `step` / `warn` / `error`）只把记录 **put 进内存队列 `queue.Queue`**，
  主流程不触碰磁盘，**零阻塞**。
- 一个 **后台守护线程** 每隔 `flush_interval` 秒（默认 10s）从队列搬空记录并落盘；
  当队列积压超过 `max_pending` 阈值时也立即提前落盘（防崩溃丢太多）。
- 落盘产物两份：
  - `<base>.jsonl` ：每行一条 JSON 记录，机器可读、完整事件流（便于日后分析/回放）。
  - `<base>.md`    ：每次 flush 重建的可读大纲（子任务 → 步骤树），便于人读。
- 进程退出 / 显式 `close()` 时**强制把残留队列 flush 落盘**，不丢记录。

仅依赖标准库，**不 import 任何业务包**，可独立运行。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class OpLogger:
    """异步缓冲操作日志器。"""

    def __init__(
        self,
        operation: str,
        log_path: str,
        flush_interval: float = 10.0,
        max_pending: int = 50,
    ) -> None:
        self.operation = operation
        self.base = Path(log_path)
        self.base.parent.mkdir(parents=True, exist_ok=True)
        self.flush_interval = flush_interval
        self.max_pending = max_pending

        self._q: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._records: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._current_subtask: Optional[str] = None

        self._record("op", "operation_start", {"operation": operation})
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="OpLogger"
        )
        self._thread.start()
        # 立即落一次盘，确保文件（含头部）存在
        self._persist()

    # ---- 对外 API -------------------------------------------------------------

    def subtask(self, name: str, desc: str = "") -> "OpLogger":
        """标记进入一个子任务（返回 self 便于链式调用）。"""
        self._current_subtask = name
        self._record("subtask", "enter", {"name": name, "desc": desc})
        return self

    def end_subtask(self, name: Optional[str] = None) -> "OpLogger":
        """标记退出当前（或指定）子任务。"""
        self._record("subtask", "exit", {"name": name or self._current_subtask})
        self._current_subtask = None
        return self

    def step(self, msg: str, level: str = "info", meta: Optional[dict] = None) -> "OpLogger":
        """记录一个步骤（隶属于当前子任务，若未进入子任务则为顶层）。"""
        self._record(
            "step",
            level,
            {"msg": msg, "subtask": self._current_subtask, "meta": meta or {}},
        )
        return self

    def warn(self, msg: str, meta: Optional[dict] = None) -> "OpLogger":
        return self.step(msg, "warn", meta)

    def error(self, msg: str, meta: Optional[dict] = None) -> "OpLogger":
        return self.step(msg, "error", meta)

    def close(self) -> None:
        """强制停止后台线程并 flush 全部残留记录。"""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._drain()
        try:
            self._thread.join(timeout=2)
        except Exception:
            pass

    def __enter__(self) -> "OpLogger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- 内部实现 -------------------------------------------------------------

    def _record(self, kind: str, action: str, payload: dict) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "action": action,
            **payload,
        }
        self._q.put(rec)

    def _run(self) -> None:
        """后台线程：周期落盘 + 阈值提前落盘。"""
        while not self._stop.is_set():
            # 等一个 flush 周期；期间若被 stop 则退出
            self._stop.wait(self.flush_interval)
            if self._stop.is_set():
                break
            # 队列积压过多则立即落，否则按周期落
            if self._q.qsize() >= self.max_pending or not self._q.empty():
                self._drain()

    def _drain(self) -> None:
        drained: list[dict] = []
        while True:
            try:
                rec = self._q.get_nowait()
            except queue.Empty:
                break
            drained.append(rec)
        if not drained:
            return
        with self._lock:
            self._records.extend(drained)
        self._persist()

    def _persist(self) -> None:
        with self._lock:
            records = list(self._records)
        jsonl_path = self.base.with_suffix(".jsonl")
        md_path = self.base.with_suffix(".md")
        jsonl_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        md_path.write_text(self._render_md(records), encoding="utf-8")

    def _render_md(self, records: list[dict]) -> str:
        lines: list[str] = []
        lines.append(f"# 操作日志：{self.operation}")
        lines.append("")
        lines.append(f"- 开始时间：{records[0]['ts'] if records else '?'}")
        lines.append(f"- 记录数：{len(records)}")
        lines.append(f"- 落盘机制：`queue.Queue` + 后台线程每 {self.flush_interval}s 批落（阈值 {self.max_pending} 提前落）")
        lines.append("")
        lines.append("## 流程（子任务 → 步骤）")
        lines.append("")
        cur_sub: Optional[str] = None
        for r in records:
            if r["kind"] == "subtask" and r["action"] == "enter":
                cur_sub = r.get("name")
                desc = r.get("desc") or ""
                lines.append(f"### ▶ 子任务：{cur_sub}" + (f" — {desc}" if desc else ""))
                lines.append("")
            elif r["kind"] == "subtask" and r["action"] == "exit":
                lines.append("")
                lines.append(f"✓ 子任务结束：{r.get('name') or cur_sub}")
                lines.append("")
                cur_sub = None
            elif r["kind"] == "step":
                lvl = r.get("action", "info")
                icon = {"error": "❌", "warn": "⚠️", "info": "•"}.get(lvl, "•")
                sub = r.get("subtask")
                # 子任务内的步骤挂在其下；顶层步骤独立列出
                prefix = f"  - " if sub else "- "
                lines.append(f"{prefix}{icon} `{r['ts']}` {r.get('msg','')}")
        lines.append("")
        lines.append("---")
        lines.append(f"_由 `scripts/op_logger.py` 生成（异步队列 + {self.flush_interval}s 周期落盘）_")
        return "\n".join(lines)


if __name__ == "__main__":
    # 自检：演示队列 + 10s 异步落盘
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        log = OpLogger("demo", f"{td}/demo", flush_interval=2.0)
        log.subtask("A", "第一个子任务").step("做点事").step("又做点事")
        log.end_subtask()
        log.subtask("B").step("第二批").warn("有个警告")
        log.end_subtask()
        time.sleep(2.5)  # 等后台线程自动落盘一次
        print(Path(f"{td}/demo.md").read_text())
        log.close()
        print("=== JSONL ===")
        print(Path(f"{td}/demo.jsonl").read_text())
