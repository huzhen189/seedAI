# 三端代码整体审查报告（v2.0.0 单进程合并后）

> 审查范围：`backend/shared`、`backend/app`（业务 + 网关 SSE + agent 核心）、`frontend/src`
> 审查性质：**只读审查 + 源码核实**，未改动任何代码（按"先看后改"约定，修复需你确认后再动手）
> 方法：3 个并行 Explore 子代理分端扫描 → 高危结论由我逐条回读源码核实（`models.py` / `proxy.py` / `orchestrator.py` / `file_io.py`）+ grep 确认

---

## 结论速览

合并主线是成功的：单进程跑通、业务名/登录/管理/统计保留、`shared` 作为事实源的主线已建立、前端**已无任何 `7102` / `ai-service` / `/generate` 残留引用**。

但审查发现若干**已核实的真实问题**，按严重度分四类（CRITICAL / BUG / SEC / 技术债+死代码），每条都带文件:行号。

---

## 🔴 CRITICAL（阻断级，必修）

### C1. ORM 模型缺口 —— `shared/models.py`（运行时必崩 500）
合并时业务/agent 层依赖的列没有同步进 `shared` 单一事实源，且 `_add_missing_columns` **只处理 ORM 已声明的列**，所以这些缺口永远不会被自动补建：

| 模型 | 缺失列（代码在引用） | 行号 |
|---|---|---|
| `Artifact` | `trace_id` / `repo` / `download_url` / `files` | `shared/models.py:116-131`（仅 id/project_id/conversation_id/version/name/url/created_at） |
| `Trace` | `started_at` / `finished_at` | `shared/models.py:134-150`（只有 created_at/updated_at） |
| `Project` | **字段名错位**：ORM 是 `build_status`（67 行），但 `proxy.py` 还在读 `proj.status` | 见 B5 |

影响：`business_repos.py` / `projects.py` / `proxy.py` / `admin.py` / `trace_repos.py` 访问上述列 → `AttributeError` / `TypeError` / 500。
核实：`app/db.py` 的 `_add_missing_columns`（42-91 行）diff 的是 `Base.metadata`（ORM 声明列）vs 实际表，缺列未声明 → 永远不 ALTER → **真缺口，非迁移遗漏**。

### C2. 多意图 orchestrator 事件永不进队列 —— `app/agent/core/orchestrator.py`（多意图流式全断）
`execute()` 在 135 行把 `q.put` 作为 `sink` 传入 `_run_one`；但 `_run_one` 内部调用 `sink(item)` 时**没有 `await`**（222 / 230 / 268 / 278 / 285 / 297 行）。`asyncio.Queue.put` 是协程，未 await → 这些事件变成"被丢弃的协程"，**永不进入队列**，外层 `q.get()` 取不到 → 前端多意图的 `subtask_start / token / subtask_done / subtask_fail` 全部丢失，多意图编排等于"哑火"。
核实：源码 131 行 `q = asyncio.Queue()`，135 行 `sink=q.put`，268 行 `sink(item)`（裸调用），确认为真。高严重、但仅在**多意图**路径触发（单意图由 skill 自身发 done，不受影响）。

### C3. 缓存命中丢当前问题 `q` —— `app/proxy.py`（多轮对话从第二轮起"失忆"）
`_build_messages_from_db`（223-308）：Redis 缓存命中时返回 `_append_q(messages, request, from_cache=True)`（251 行）。但缓存写入在 280-282 行、发生在 `_append_q(messages, request)`（289 行，无 `from_cache`）**之前**——即 Redis 里只存了"不含当前 q"的历史。
而 `_append_q`（292-308）在 `from_cache=True` 时 294-295 行直接 `return messages`，**永不追加 `q`**。
结果：从第 2 轮起，Redis 命中（TTL 30min 滑动），LLM 拿到的 messages **不含用户这一轮的问题**，只剩历史 → 答非所问/重复上文。
核实：源码 251 行 `from_cache=True` + 294-295 行短路返回，确认为真，高影响（所有有历史的多轮对话都中招）。

---

## 🟠 BUG（功能缺陷，本迭代修）

### B4. `project_id` 潜在 `NameError` —— `app/proxy.py`
668-670 行 `try: conv = await db.get(...); project_id = ...`，`project_id` 在 try 内绑定；若 `db.get` 抛异常（DB 抖动），`project_id` 未赋值，而 1019 行 `_do_persist(project_id=project_id)` 在调用方求值 → `UnboundLocalError` 崩溃。注释说"已修正"，但修正只是把绑定挪进 try，没做 `project_id = None` 初始化。
修复：在 668 行 `try` 前加 `project_id = None`。

### B5. `proj.status` → `proj.build_status` —— `app/proxy.py:682`
`Project` ORM 列是 `build_status`（67 行），此处读 `proj.status` → `AttributeError`。`requirement_doc` / `system_prompt` / `constraints` 下发全部连带失败。

### B6. 旧转发残留（自环回 + 死路由 + 非依赖 import）—— `app/proxy.py`
- 314-348 行 `/models` `/agents` 仍用 httpx 转发到 `settings.ai_service_url`（单进程自环回；且 `/agents` 路由不存在 → 500）。
- 1210-1235 行 `/cancel` 同样转发 + 1227 行 `import sqlmodel`（非项目依赖）。
- 52 / 78-81 / 191-202 / 795 行：转发层遗留的死 import / 死函数。

### B7. 前端 `sending` 标志不复位 —— `frontend/src/ChatView.vue`（用户被锁死）
`error` / `aborted` / `retry` 事件路径没有重置 `sending=false` → 一旦发生即使用户想重发也发不出去（按钮永久禁用）。

### B8. 前端 `EventSource` 泄漏/重复 —— `frontend/src/ChatView.vue`
resend 路径 + 各控制事件（confirm/clarify/…）会新建 `EventSource` 但未关闭上一个 → 累积连接、重复收流、内存泄漏。

---

## 🔴 SEC（安全）

### S9. 路径穿越 —— `app/agent/tools/file_io.py:44-50`
`fp = root / path` 未做 `resolve()` + 前缀校验。传入 `path="../../etc/passwd"` 即可越出 `artifact_dir` 读写任意文件。
修复：`fp = (root / path).resolve()` 后断言 `str(fp).startswith(str(root.resolve()))`。

### S10. 前端 `:href` scheme 未校验 —— `frontend/src`（XSS）
渲染某些链接未过滤 `javascript:` 等危险 scheme，可被注入 JS。

### S11. 日志命名违规 —— `app/agent/tools/file_io.py:14`
`logger = getLogger("ai_service.tools.file_io")`，是旧两进程命名，违反"统一 `app.*` / `app.agent.*`"约定（且 `MEMORY.md` 有铁律）。应改为 `app.agent.tools.file_io`。

---

## 🟡 技术债 + 死代码

- **C12. `shared/db.py` 是 1MB 死文件**：仅自 import 自己的 logger，未被 `app` 任何模块引用。"shared 为 db 单一事实源"的指令未落地，引擎/会话逻辑仍分散在 `app/db.py`。
- **C13. 前端 WebLLM 死代码 + `@mlc-ai/web-llm` 依赖**：未实际使用却增加打包体积与攻击面。
- **C14. 前端 `setupScrollLoading` 累积 `IntersectionObserver`**：每次调用新增 observer 不销毁 → 长会话内存泄漏。
- **C15. 默认模型标签不一致**：默认模型是 `qwen`（`proxy.py:540`），但前端 placeholder / 默认值还残留 `hy3` 字样，显示与行为不符。
- **C16. `frontend/app/page.tsx` 越权范围**：仍用 `/api/generate` + localStorage JWT（旧鉴权）；不属于本次 review 主栈（`src/` 已干净），建议单独评估/删除或改造。

---

## ✅ 建议修复顺序（确认后我执行）

1. **立即修（阻断）**：C1（补 ORM 列 + 改 B5 的 `build_status`）→ C2（`await sink(item)`）→ C3（cache 命中也追加 `q`）。
2. **本迭代修（BUG/SEC）**：B4 / B5 / B6 / B7 / B8 / S9 / S10 / S11。
3. **排期清理（技术债）**：C12 / C13 / C14 / C15 / C16。

---

## 备注（git 约定）
本任务为**审查**，未改任何代码。若你确认修复，我将按"本地 `git commit` + 打 tag，绝不自动 `git push`"的约定执行，push 由你掌控。
