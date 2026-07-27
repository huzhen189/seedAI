# 对话断点复联 — 三场景状态机详细设计方案（v4，无心跳版）

>  目标：用**后端权威状态源 `user_states`** 统一追踪"我正在哪个项目/会话、任务跑到哪、是否需要续跑"，  
> 并精确处理 **手动停止 / 刷新 / 离开页面** 三种操作，做到：  
> ① 手动停止 → 做完手头事 → 暂停 → 暂存 → 推送进度 → 等用户指令；  
> ② 刷新 → 断连→暂停→自动重连 → status 翻回 running → 从 checkpoint 续跑 → 返回结果；  
> ③ 离开页面 → 断连→暂停→暂存 → 下次打开看到暂停态 → 发指令续跑。
>
> **v4 关键简化**：不再用「心跳」区分刷新/离开。两者在后端眼里都是"断连"，统一走  
> 「断连即暂停 + 重连续跑翻状态」链路，无需前端 ping 心跳、无需 30s 宽限计时器。  
> 所有后端注入点逐一对齐**现有任务流状态机**（上游 agent 事件 → 阶段 → user_states 字段）。  
> 本文档为**设计稿，尚未实施**。

---

## 0. 结论速览（先给答案）

| 场景         | 当前代码行为                                                                              | 目标行为（v4）                                                                                                      | 关键缺口                      |
| ---------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------- |
| **① 手动停止** | `stop()` → `cancelChat` 置 `cancel:{tid}` → Worker **abort** → `aborted` 死态，**无法续跑** | `POST /pause` 置 `pause:{tid}=user_interrupt` → Worker 跑完当前阶段 → 暂停 → 落 checkpoint → 发 `paused` → 等指令           | 没有 `pause` 原语；没有"暂停等指令"状态 |
| **② 刷新**   | 断连 → `_on_disconnect` 置 `cancel:{tid}` → 重连 `resume` 清 cancel + 重跑                  | 断连即置 `pause:{tid}=offline_timeout` → Worker 边界暂停；新页 `resume` → 清 pause + status 翻 `running` + 从 checkpoint 续跑 | `cancel` 触发太早且无 pause 语义  |
| **③ 离开页面** | 同 ②（无心跳）→ 大概率 abort                                                                 | 与 ② **同一链路**：断连即暂停（无重连则不翻回）→ 暂存 → 下次打开恢复                                                                      | 当前断连=取消，离开即丢              |

**核心论断（v4）**：② 与 ③ **不需要区分**。两者都是"断连"，后端统一"断连→暂停"。  
刷新后新页会自动 `resume`，此时把 `status` 从 `paused` 翻回 `running` 即可；  
离开后无 `resume`，自然停在 `paused`。区分点只在"重连早晚"，机制完全一致。  
**无需心跳、无需 30s 宽限计时器、无需前端 ping 循环。**

---

## 1. 现状逐场景盘点（含代码行号 + 缺口）

### 1.1 后端取消/断连机制（现状）

- `main.py:189-201` `POST /cancel` → `get_queue().set_cancel(trace_id)` 写 `cancel:{tid}`（ex=3600）。
- `proxy.py:786-806` `_on_disconnect()`：断连时 `SREM clients:{tid}`，若 `after is None and not saw_terminal and remaining==0` → `SET cancel:{tid}=1`。
- `proxy.py:1000-1019` SSE 发送失败（`BrokenPipe` 等）→ 调 `_on_disconnect()`，置 `terminal_status="aborted"`。
- `queue.py:344-347 / 406-410 / 558-562` 三个 backend 的 `is_cancelled/set_cancel`（Memory/Redis）。
- Worker 在 `orch.execute`（`queue.py:933-978`）与 `run_skill`（`queue.py:1037-1157`）的 `async for` 循环中消费 `is_cancelled=_cancelled`，**仅"取消/中止"语义，无"暂停"语义**。

### 1.2 前端停止/重连（现状）

- `ChatView.vue:1235-1243` `stop()`：`generating=false` → `cancelChat(traceId)` → `esRef.close()`。立即关闭、立即清 UI，不等 Worker 收尾。
- `ChatView.vue:1207-1233` `resume()/maybeResume()`：`resume` 用 `openEs({resume:true})` 重开 SSE；`maybeResume` 读 sessionStorage 的 `activeGen`。
- **无心跳**：`grep` 确认 ChatView / ChatInput 无任何 `setInterval/heartbeat/visibilitychange/beforeunload` 与聊天相关。（v4 也不再需要添加）

### 1.3 三个场景现状判定

| 场景     |     能否"做完手头事再停"    |   能否"暂存+等指令"   |        能否"刷新续跑"       | 能否"离开后恢复" |
| ------ | :----------------: | :------------: | :-------------------: | :-------: |
| ① 手动停止 |     ❌ 立即 abort     | ❌ 进 aborted 死态 |           ❌           |     ❌     |
| ② 刷新   | ⚠️ 竞态（断连可能先 abort） |        ❌       | ⚠️ 靠 resume 重跑（非续传游标） |   ⚠️ 同刷新  |
| ③ 离开   |    ❌ 等同刷新/abort    |        ❌       |           —           |  ❌ 无状态追踪  |

---

## 2. 三场景统一机制（v4 核心：断连即暂停 + 重连续跑翻状态）

```
                         ┌─ 手动点"停止" ──── POST /pause ──► pause:{tid}=user_interrupt ─┐
 意图/断连 ──────────────┤                                                              ├─► Worker 跑完当前阶段 → 暂停 → 落 checkpoint → 发 paused
                         └─ SSE 连接丢失 ── _on_disconnect ─► pause:{tid}=offline_timeout ┘    (status=paused, pause_reason=*)
                                                                                                │
        ┌───────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
   新页 onMounted → GET /api/my-info → 发现 status=paused(或 running 残留) + needs_resume=true
        ├─ resume(tid)：清 pause:{tid} + cancel 旧 Worker + re-enqueue(checkpoint) + status 翻 running
        └─ Worker 继续 → 发 token/node/... → 最终 done
```

- **① 手动停止**：前端**主动** `POST /pause`，后端**立即**登记 `pause:{tid}=user_interrupt`，Worker 在下一阶段边界暂停。
- **② 刷新**：旧页卸载 → SSE 断开 → `_on_disconnect` 置 `pause:{tid}=offline_timeout`（**不立即 abort**）→ Worker 在下一边界暂停。  
  新页 `onMounted` → `my-info` 看到 `paused` → `resume` 清 pause + status 翻 `running` + 从 checkpoint 续跑。  
  **刷新过程中那次短暂的 `paused` 翻转是设计内的、用户无感（通常 <1 个阶段）。**
- **③ 离开**：断连 → `pause:{tid}=offline_timeout` → Worker 暂停 → 无 `resume` → 永久 `paused`。  
  下次打开 `my-info` 看到 `paused`，渲染"已暂停于 XX 阶段，请输入指令继续"；用户发指令即 `resume` 续跑。

> **为什么不需要心跳**：刷新与离开的"区别"只有"重连早晚"，而两者的后端动作完全相同（断连→暂停）。  
> 重连（resume）自然把状态翻回 running；不重连则停在 paused。无需前端主动 ping 证明"我还活着"。  
> 这比 v3 的「心跳键 + 30s 宽限计时器」少 2 个 Redis 键、少 1 个前端 `setInterval`、少 1 个后端计时器协程，  
> 且消除了"心跳 TTL 边界误判"的风险。

---

## 3. 统一状态机扩展

### 3.1 现有任务流状态机（上游，`events.py` + `agent_generate_site.py`）

```
router → intent → dispatch → planning → coding → coding_done
       → reviewing → review_rN → previewing → await_confirm → done
决策门：router_block / router_confirm / router_options / clarify / retry / safety_block
```

`user_states.current_stage` 严格等于上游 `node(stage=...)` 的取值。

### 3.2 新增"暂停"语义（区分 cancel/abort）

| 标志键            | 触发方                              | Worker 行为                 | 终态(user_states.status) | 可续跑 |
| -------------- | -------------------------------- | ------------------------- | ---------------------- | :-: |
| `cancel:{tid}` | 现有 `/cancel`、"放弃"按钮              | 立即中断（下一 token/阶段前）        | `aborted`              |  ❌  |
| `pause:{tid}`  | 新增 `/pause`、**`_on_disconnect`** | **跑完当前阶段**后停，落 checkpoint | `paused`               |  ✅  |

> 手动停止与断连(**刷新/离开**)**都走 `pause:{tid}`**，仅 `pause_reason` 不同（`user_interrupt` / `offline_timeout`），前端据此渲染不同提示文案。

### 3.3 `user_states` 状态全集

```
status ∈ { idle, running, paused, aborted, done, error }
pause_reason ∈ { null, user_interrupt, offline_timeout }
pending_decision ∈ { null,
                     continue_instruction,   // 暂停后等用户发新指令继续
                     retry_model,            // 决策门 retry 卡住
                     confirm_plan,           // await_confirm 卡住
                     router_block, router_confirm, router_options, clarify, safety_block }
```

---

## 4. 机制改动（替代 v3 的心跳/grace-timer）

### 4.1 断连即暂停（改写 `proxy.py:786-806` `_on_disconnect`）

```python
async def _on_disconnect() -> int:
    rc = await get_redis()
    await rc.srem(f"clients:{tid}", conn_id)
    remaining = await rc.scard(f"clients:{tid}")
    if after is None and not saw_terminal and remaining == 0:
        # v4: 不再立即 cancel！改为置 pause(离线)，Worker 在下一阶段边界暂停
        await rc.set(f"pause:{tid}", "offline_timeout", ex=3600)
        logger.info("[chat] 断连→离线暂停 trace=%s", tid)
    return remaining
```

- 仅当 `remaining==0`（最后一个 SSE 客户端断开）才暂停，多标签页不误伤。
- **不调用 `set_cancel`**：Worker 不 abort，而是跑到边界自然暂停（保留现场）。

### 4.2 手动停止（`POST /pause`，新增 `main.py`）

```python
@router.post("/pause")
async def pause_chat(req: PauseReq, user=Depends(get_current_user)):
    rc = await get_redis()
    await rc.set(f"pause:{user.id}:{req.trace_id}", "user_interrupt", ex=3600)  # 或 pause:{tid}
    await _touch_user_state(user.id, status="paused", pause_reason="user_interrupt",
                            pending_decision="continue_instruction")
    return {"ok": True}
```

- Worker 在下一阶段边界检测到 `pause:{tid}` → 落 checkpoint → 发 `paused(reason=user_interrupt)`。

### 4.3 重连续跑翻状态（`resume` 分支，`proxy.py:829-839`）

```python
# resume 时（无论刷新还是离开后重开）：
await rc.delete(f"pause:{tid}")          # 清暂停
await rc.set(f"cancel:{tid}", "1", ex=3600)  # 先 cancel 可能还活着的老 Worker，避免双 Worker 并发
# 删除旧 stream + 重新入队(checkpoint)  ← 沿用现有 resume 逻辑
await _touch_user_state(uid, status="running", pause_reason=None,
                        pending_decision=None, current_stage=<checkpoint_stage>)
```

> **双 Worker 防护**：resume 先 `set_cancel`，确保任何仍在中途阶段运行的老 Worker 在边界处 abort；  
> 新 Worker 从 checkpoint 重新入队，保证续跑且唯一执行。

### 4.4 阶段边界暂停（Worker，`queue.py`）

在 `orch.execute` / `run_skill` 的 `async for` 循环每个**阶段边界**（即 `node` 事件 emit 之后）插入：

```python
if _paused():   # 读 pause:{tid}
    await _save_checkpoint(stage, partial, progress)   # Redis+MySQL(await)
    publish("paused", {"reason": pause_reason, "stage": stage, "progress": progress, ...})
    break   # 退出循环，不 abort
```

- 手动停止与断连共用此路径，仅 `pause_reason` 文案不同。

---

## 5. 三个场景端到端时序

### 5.1 ① 手动停止（用户点"停止"）

```
前端                                 后端                                  Worker
 │                                     │                                     │
 ├─ 点"停止"                          │                                     │
 ├─ (不立即 generating=false)         │                                     │
 ├─ POST /pause {tid} ───────────────►│ SET pause:{tid}=user_interrupt      │
 │                                     ├─ 返回 200                          │
 │                                     │                          ┌─ 当前阶段(async for 内)跑完
 │                                     │                          │  _paused() 检测 → True
 │                                     │                          │  落 checkpoint(Redis+MySQL, await)
 │                                     │                          │  发 paused(reason=user_interrupt)
 │                                     │◄─ publish paused ────────┤
 ├─ 收到 paused 事件                   │                          │  status=paused,
 │  才 generating=false                │                          │  pending_decision=continue_instruction
 │  渲染"已暂停，请输入指令继续"        │                          │
 │  (输入框保持可输入)                  │                          │
 ▼                                     │                          ▼
[用户在新输入框打字 → 回车]            │                          │
 ├─ POST /api/chat resume=true + 新指令►│ 清 pause, cancel 老 Worker, re-enqueue │
 │                                     │ status 翻 running → 续跑            │
```

**要点**：Worker 不立即死，而是"当前阶段收尾 → 暂存 → 发 paused → 等指令"。

### 5.2 ② 刷新页面（断连→暂停→重连续跑翻状态）

```
t0 用户提问 → SSE 建立, Worker 运行中
t1 用户按 F5
   ├─ 旧页卸载 → SSE TCP 断开
   ├─ 后端 _on_disconnect → remaining=0 → SET pause:{tid}=offline_timeout
   │     （不 cancel；Worker 继续跑到下一阶段边界）
   ├─ Worker 在边界检测到 pause → 落 checkpoint → 发 paused → status=paused
t2 新页 onMounted(通常 <3s)
   ├─ GET /api/my-info → status=paused, current_stage=..., pause_reason=offline_timeout, needs_resume=true
   ├─ maybeResume → resume(tid) → openEs(resume:true)
   ├─ 后端 resume 分支: 清 pause + cancel 老 Worker + re-enqueue(checkpoint) + status 翻 running
   ├─ 新 Worker 从 checkpoint 继续 publish → 前端累积 token
t_end 任务完成 → done
```

**要点**：刷新 = 断连→暂停→重连→翻 running→续跑。中间的 `paused` 翻转用户对无感（<1 阶段）。  
**限制**：单次超长 LLM 流内部不可"字面续传"，但阶段级 checkpoint（router→intent→planner→coder→reviewer→preview）足够细，用户感知为"接着跑完"。

### 5.3 ③ 离开页面（断连→暂停→暂存→下次恢复）

```
t0 生成中
t1 用户关闭标签页/导航走 → 无新连接
   ├─ 旧 SSE 断开 → _on_disconnect → SET pause:{tid}=offline_timeout
   ├─ Worker 当前阶段收尾 → 落 checkpoint → 发 paused(reason=offline_timeout)
   ├─ status=paused, pending_decision=continue_instruction
t2 用户再次打开页面(几分钟后)
   ├─ GET /api/my-info → status=paused, pause_reason=offline_timeout
   ├─ 渲染"上次任务已暂停于 XX 阶段，已保存进度。输入指令继续"
   ├─ 用户发指令 → resume 从 checkpoint 续跑（同 5.2 的 t2 之后）
```

**要点**：与 ① 同一条"暂停等指令"链路，仅 `pause_reason` 文案不同；与 ② 同一条断连→暂停链路，仅"是否重连"不同。

---

## 6. Redis / MySQL 状态字段

### 6.1 Redis（优先，低延迟）

| 键                   | 类型     | TTL  | 写入点                                           |
| ------------------- | ------ | ---- | --------------------------------------------- |
| `user_states:{uid}` | hash   | 3600 | 见 §7 注入点                                      |
| `pause:{tid}`       | string | 3600 | `/pause`、**`_on_disconnect`**（v4 新增）、grace 取消 |
| `cancel:{tid}`      | string | 3600 | `/cancel`、"放弃"、resume 时 kill 老 Worker         |
| `clients:{tid}`     | set    | 3600 | SSE 连接登记                                      |
| `gen:stream:{tid}`  | stream | —    | Worker 进度回放（已有）                               |

> v4 **移除** v3 的 `hb:{tid}` 心跳键（无需前端 ping）。

### 6.2 MySQL 表 `user_states`（权威落库，重启可读）

```sql
CREATE TABLE user_states (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id         INT NOT NULL,
  current_project_id   INT NULL,
  current_conversation_id INT NULL,
  active_trace_id INT NULL,
  status          VARCHAR(20) NOT NULL DEFAULT 'idle',  -- idle/running/paused/aborted/done/error
  current_stage   VARCHAR(40) NULL,   -- 对齐 node(stage)
  progress_pct    TINYINT DEFAULT 0,
  pause_reason    VARCHAR(20) NULL,   -- user_interrupt / offline_timeout
  pending_decision VARCHAR(30) NULL,  -- continue_instruction / retry_model / confirm_plan / ...
  checkpoint_stage VARCHAR(40) NULL,
  updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_user (user_id)
);
```

> 与现有 `conversations.checkpoint_*` / `traces.status` 互为补充：`user_states` 是**用户级入口索引**，`conversations`/`traces` 是**任务级明细**。

---

## 7. 后端改动点清单（按文件/函数/行）

### 7.1 `proxy.py`

| 位置                                     | 改动                                                                                                                               |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `chat()` 启动段（~603 行 `create_trace` 后）  | `[1.5/8]` 写 `user_states`: `active_trace_id=tid, status=running, current_stage=router`                                           |
| `publisher()` 首连（`proxy.py:779-782`）   | 清 `cancel:{tid}`；**不碰 `pause`**（pause 由 resume 分支清）                                                                              |
| `_on_disconnect()`（`proxy.py:786-806`） | **移除立即 `SET cancel`**；改为 `SET pause:{tid}=offline_timeout`（仅 `remaining==0`）                                                     |
| SSE 循环每个上游事件（~850-1020）                | `node/think/plan/token/checkpoint/paused/preview/...` → `await _touch_user_state(uid, stage=..., progress=..., status=running)`  |
| `paused` 事件处理（`proxy.py:898-910`）      | 写 `user_states`: `status=paused, pause_reason=<payload.reason>, pending_decision=continue_instruction, checkpoint_stage=<stage>` |
| `aborted` 事件（`proxy.py:915-925`）       | 写 `user_states`: `status=aborted`（仅"放弃"走此路）                                                                                      |
| `done` 事件                              | 写 `user_states`: `status=done, current_stage=done, progress=100`；清 `pause/cancel`                                                |
| `finally`（`proxy.py:1032-1059`）        | `_do_persist` 完成后同步 `user_states.status=terminal_status`                                                                         |
| `terminal_status` 初值（`proxy.py:815`）   | **修复**：默认 `"running"`（原 `"done"` 会在未收终止事件时误记 done，见 §9.2）                                                                        |
| resume 分支（`proxy.py:829-839`）          | **清 `pause:{tid}` + `set_cancel`(kill 老 Worker) + re-enqueue(checkpoint) + status 翻 `running`**                                  |

### 7.2 `queue.py`（Worker）

| 位置                                                              | 改动                                                                                                     |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `is_cancelled/set_cancel`（`queue.py:344-347/406-410/558-562`）   | 新增 `is_paused/set_pause`（三 backend 同构），读 `pause:{tid}`                                                 |
| `orch.execute` / `run_skill` 调用（`queue.py:933-978 / 1037-1157`） | 将 `_cancelled` 改为同时传入 `_paused`；循环内每个**阶段边界**（node emit 后）检查 `is_paused()`                             |
| 阶段边界暂停                                                          | 若 `is_paused()` → 落 checkpoint（Redis+MySQL await）→ `publish paused(reason=...)` → `break`（**不 abort**） |
| `[7/7]` 兜底落库（`queue.py:1184` 既有 `_persist_worker_result`）       | 完成时同步 `user_states.status=done`                                                                        |

### 7.3 `main.py`

| 位置                                | 改动                                                                                                  |
| --------------------------------- | --------------------------------------------------------------------------------------------------- |
| `POST /cancel`（`main.py:189-201`） | 保留（"放弃"用），语义不变                                                                                      |
| **新增** `POST /pause`              | `SET pause:{tid}=user_interrupt EX 3600`；写 `user_states.status=paused, pause_reason=user_interrupt` |
| **新增** `GET /api/my-info`         | 读 `user_states:{uid}`（Redis 优先，miss 回 MySQL）；返回项目/会话/状态/needs_resume                                |

### 7.4 `projects.py`（切换项目/会话时写入 current\_*）

| 位置                                | 改动                                                                                                                  |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| 列表/详情/新建会话（`projects.py:405-470`） | 切项目 → `user_states.current_project_id`；切会话 → `current_conversation_id`（含 `Trace.status=="processing"` 死代码修复，见 §9.1） |

### 7.5 `tracing.py`

| 位置                   | 改动                                            |
| -------------------- | --------------------------------------------- |
| `create_trace`（已修幂等） | 保持；`status` 写 `"running"`（与 `user_states` 一致） |

---

## 8. 前端改动点清单

### 8.1 `ChatView.vue`

| 位置                                   | 改动                                                                                                                                      |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `onMounted`                          | 先 `GET /api/my-info`：恢复 `currentProjectId/currentConvId`；若 `status==running/paused` → `maybeResume/resume`；若 `paused` → 渲染"已暂停，请输入指令继续" |
| `stop()`（`ChatView.vue:1235-1243`）   | 改调 `pauseChat(traceId)`（新 API）而非 `cancelChat`；**不立即 `generating=false`**，等收到 `paused` 事件再停 UI；保留"放弃"按钮走 `cancelChat`                    |
| `makeCallbacks` 新增 `onPaused`        | 收到 `paused` → `generating=false`；若 `reason=offline_timeout` 显示"上次任务已暂停…"；输入框保持可输入，发消息即 `resume`                                         |
| `send()`（续跑指令）                       | 若 `user_states.status==paused` → `openEs({resume:true, q: text})`（从 checkpoint 续跑）                                                      |
| `resume()`（`ChatView.vue:1207-1223`） | 增加：清 `pause:{tid}`（后端 resume 分支已清）；**不需要**心跳相关逻辑                                                                                        |
| 移除 sessionStorage 依赖                 | `activeGen`/`activeConv_*` 改为只读 `my-info`；`maybeResume` 改为读 `user_states`                                                               |

### 8.2 `ChatInput.vue`

- "停止"按钮 → 调 `stop()`（内部改 `/pause`）；保留"放弃"为 `cancelChat`+"aborted"。
- 暂停态下输入框常驻可输入（发消息即续跑）。

### 8.3 `api/chat.ts`

- 新增 `pauseChat(traceId)`、`myInfo()`。**（v4 无 heartbeat 函数）**。

---

## 9. 两个隐藏 bug（顺带修，不依赖整体方案）

### 9.1 `projects.py:413` 死代码

`/status` 端点查 `Trace.status == "processing"`，但全代码**没有任何地方写 `"processing"`**（`create_trace` 写 `"running"`）。结果：永远检测不到"正在生成"。  
**修复**：改为查 `user_states.status == "running"`，或 `traces.status IN ("running")`。

### 9.2 `proxy.py:815` `terminal_status` 默认 `"done"`

若流未收到终止事件、也未被断连检测命中（如 `after` 回放追到流尾自然结束），会**误记成 done**。  
**修复**：默认 `"running"`，仅在收到 `done/aborted/error/paused` 时改写。

---

## 10. 验证清单（逐场景对应注入点）

- [ ] **① 手动停止**：生成中点"停止" → DB `user_states.status=paused, pause_reason=user_interrupt`；前端显示"已暂停，请输入指令继续"；发新指令 → `resume=true` 从 checkpoint 续跑成功。
- [ ] **① 不丢成果**：暂停瞬间 `conversations.checkpoint_data` 非空（MySQL 已落）。
- [ ] **② 刷新续跑**：生成中点 F5 → 新页 `my-info` 返回 `paused`（或 running 残留）→ 自动 `resume` → status 翻 `running` → token 继续累积 → 最终 `done`。
- [ ] **② 不双 Worker**：resume 先 `cancel` 老 Worker，确认仅一个新 Worker 在跑。
- [ ] **③ 离开暂停**：关标签页 → `user_states.status=paused, pause_reason=offline_timeout`；Worker 落 checkpoint；再开页渲染"已暂停于 XX 阶段"。
- [ ] **断连竞态消除**：原"\_on_disconnect 立即 cancel"已移除，刷新/离开不再随机 abort。
- [ ] **两个 bug 修复**：`/status` 能检出 running；`terminal_status` 不再误记 done。
- [ ] **项目/会话恢复**：`my-info` 返回正确 `current_project_id/current_conversation_id`，打开即定位。

---

## 11. 实施顺序建议（分 commit，逐个可测）

1. **commit A（后端基础）**：`user_states` 表 + `GET /api/my-info` + `POST /pause` + `tracing`/`projects` 状态写入 + §9 两 bug 修复。（可单测 my-info 返回、/pause 落地）
2. **commit B（后端暂停原语 + 断连即暂停）**：`queue.py` 加 `is_paused/set_pause` + Worker 阶段边界暂停 + `_on_disconnect` 改写（cancel→pause）+ resume 分支翻状态 + `terminal_status` 默认修复。（单测 ① ③）
3. **commit C（前端入口）**：`onMounted` my-info 恢复 + `stop→pause` + `onPaused` 渲染 + 续跑指令 + 去 sessionStorage。（联测 ①②③）

---

> 文档状态：**v4（无心跳版，断连即暂停 + 重连续跑翻状态）**，未实施。确认方向后按 §11 三 commit 推进。
