# 第三步 · 运行时基础设施重新设计（历史依据）

> **状态：历史依据。自 2026-08-01 起已被《SeedAI全链路重构最终实施规范.md》替代；传输、交互、稳定性、配额、BYOK 和灾备以最终规范为准。**
>
> **定位**：第一、二步定义了「做什么（Tool/Skill/DB）」与「怎么决策（路由/意图/执行流程）」。**第三步定义「系统怎么稳定地把它跑起来并交给用户交互」**——即传输层、人机交互、稳定性、多租户计量与计费、用户自定义模型接入（BYOK）。
>
> **全新项目口径**：不迁就旧 `proxy.py`(1791 行) / `queue.py`(1588 行) 上帝文件，传输层、网关、计量、BYOK 全部按生产级标准重写。
>
> **命名对齐**（沿用 step1 §0 硬规则 + step2）：
> - 阶段名沿用 `S0..S9`（step2 §7.3）。
> - 状态容器 `TurnContext`（step2 §7.1）。
> - 交互干预沿用 `InterventionEvent`（step2 §1.6），本步将其**扩展显式化为六类操作**（§3）。
> - Redis 键沿用 `ai:session:` / `ai:cancel:` / `ai:gate:approval:` / `ai:ratelimit:user:*`；SSE 缓冲新增 `ai:stream:`。
> - 计量表沿用 `usage_ledger`（step1 §3.2）/ `metrics_*`。
> - 凭证隔离沿用 step1 §5 前缀 `ai:` 隔离原则。

---

## 0. 总体分层（三层传输 + 网关 + 计量 + 密钥）

```
┌──────────────┐   POST /api/chat  (fetch + body)         ┌─────────────────┐
│  前端 (Vue3)  │ ───────────────────────────────────────▶ │  Edge / Ingress │
│  SSE 消费侧   │                                          │ (TLS, 禁缓冲,   │
│  EventSource/ │ ◀──────── SSE stream (typed events) ──── │  超时配置)      │
│  fetch Reader │                                          └────────┬────────┘
└──────────────┘                                                    │ 转发 (sticky by stream_id)
                                                                      ▼
┌─────────────────────────── Stream Broker（无状态, 按 stream_id 路由） ───────────────────────────┐
│  · 复用/扇出：同 stream_id 已在进行 → 挂新订阅者, 不重启 provider 调用（防 double-spend）            │
│  · 断线续传：ai:stream:{stream_id} 滚动缓冲 window，Last-Event-ID 重放                            │
│  · 取消传播：收到 ai:cancel:{turn_id} / 客户端断连 → 向上游 provider 发 abort（停计费）            │
│  · 完成后保留热态 60~120s，应对刷新/切设备                                               │
└───────────────────────────────────────────┬─────────────────────────────────────────────────────┘
                                              │ 调 Pipeline（step2 §7.2，S0..S9）
                                              ▼
┌──────── Gateway（配额/计量/路由） ────────┐   ┌──── Model Worker（持 credential） ────┐
│ · token 额度预检 + 事后对账                │   │ · 平台凭证 或  tenant 自带 key(BYOK)  │
│ · rpm / 并发 session / 单 session 预算     │   │ · 熔断/降级                          │
│ · 429 结构化错误 + Retry-After            │   │ · 流式归一化为通用 event 格式          │
└──────────────────────────────────────────┘   └──────────────────────────────────────┘
```

> **为何独立 Stream Broker 而非旧 `proxy.py` 直连 LLM**：旧架构把 SSE、取消、队列、Provider 调全都揉在一个文件里，断线/取消/多设备任何一个改动都牵连全局。本步把「传输」与「Agent 逻辑」解耦——Pipeline（S0–S9）只产生事件流，`Stream Broker` 负责把它可靠地送达、续传、取消。

---

## 1. SSE 流传输层（断线重连 + 前端内容展示）

### 1.1 `stream_id` 与 `event_id` 设计

- **`stream_id` 确定性派生**：`{conversation_id}:{turn_id}:{epoch}`，其中 `epoch` 仅在两种场景自增（决策 B）：
  1. **用户编辑 prompt**（在生成前/暂停态修改输入重发）；
  2. **系统重置态**（`/reset_sir` 等清状态操作）。
  - **不**因每次 SIR delta 合并、Task 进度等常态状态变化而 +1——否则 epoch 永远变、续传键永远失效。**状态变化后 epoch+1 防陈旧续传**，仅指上述两类"语义上应作废旧流"的事件。
- **`event_id` 严格自增整数**（每 stream 从 1 起），**不使用 provider 的 chunk id**（各厂商格式不一，自管编号）。
- SSE 头带 `id: {event_id}`，浏览器原生重连携带 `Last-Event-ID`。

### 1.2 事件协议（强类型，端到端一致）

> 原则（调研共识）：**绝不在 `data:` 里重载控制信号**，每种语义一个 `event:` 类型，前端逐类显式处理，模糊协议 = 抖动的 UI。

| event             | 含义                                        | 前端动作                                   |
| ----------------- | ------------------------------------------- | ------------------------------------------ |
| `token`           | 正文增量 delta                              | 追加渲染（小 chunk 高频，禁攒批 200ms 才刷） |
| `tool`            | 工具调用 IO（输入/输出/状态）                | 实时填入 Activity Panel 的步骤卡            |
| `state_diff`      | SIR / 任务 DAG 状态变更                      | 更新进度树、回滚指示                         |
| `stage`           | **十阶段流水线进度标记（S0~S9 enter/leave）** | 点亮「阶段进度条」对应节点、显示当前阶段说明 |
| `approval`        | HIGH 工具待确认（§2）                        | 弹出审批 UI，暂停流                         |
| `heartbeat`       | `: comment` 保活                            | 浏览器/EventSource 忽略，仅撑连接           |
| `usage`           | 本 turn 真实 token 用量（尾部兜底）          | 写入用量展示、触发对账（§5.2）              |
| `capability_notice` | 能力降级告知（§9.3）：`{feature, tier, limitation, upgrade_hint}` | Activity Panel 实时挂"演示态"提示条，收尾汇总进「能力说明」段 |
| `error`           | 结构化错误                                   | 切错误 UI（3-part：what/why/next）         |
| `done`            | 本轮正常结束                                 | 收尾、持久化、解锁输入                       |
| `reconnect`       | 服务端优雅关闭时通知客户端换实例              | 客户端重连到其他实例                         |

### 1.3 断线重连（解决 phantom compute / abandoned reruns）

- **机制 A（短窗续传，主路径）**：`Stream Broker` 在 `ai:stream:{stream_id}`（Redis LIST，TTL 120s）缓冲最近滚动窗口的事件。`Last-Event-ID` 重连 → 从断点重放缺失部分，**LLM 不重跑**；若 broker 有完整响应用压缩（直接给整段 message 而非逐 token 重放）。
- **机制 A'（长离线/数月返回兜底，决策 A 扩展）**：Redis 120s 缓冲只覆盖"秒级/分钟级"断网重连。**用户断网数天或离开数月**场景，靠**持久化落地**而非内存缓冲：
  - 每 turn 的输出随生成**实时落库**——正文写 `messages.content`、产物树写 `messages.content_path`、状态写 `messages.sir_snapshot`，生成中即写 partial，完成后标记 `done`（step1 §3.1）。用户数月后回来，前端直接拉会话历史 + `content_path` 指向的磁盘/COS 静态产物重建 UI，**无需重放流、LLM 不重跑**。
  - 暂停/挂起态（`ai:pause:{turn_id}`）**额外持久化到 DB**（新增 `paused_turns` 行，TTL 长/无过期），用户数月后回来前端显示"该任务曾暂停，是否继续？"，点击即从断点 `ai:sir:snap:{cid}` 热快照 + 已完成 Task 续跑（复用 §3 否定/补充机制）。
  - 已 `done` 的 turn 与产物**永远可读**（静态文件在本地目录/COS 长期保留，与 Redis TTL 解耦）。
  - 结论：**短期重连用 Redis 滚动缓冲；中长期恢复用 DB + 磁盘/COS 持久态**，两层互补，杜绝"离开久了会话/产物全丢"。
- **机制 B（幂等重试）**：客户端带 `turn_id` 重试；服务器若该 `turn_id` 已完成则直接返回缓存输出（`usage`+`done`），不二次生成（避免刷新导致双倍 output token 计费）。
- **取消即停计费**：断连 / 用户主动停 → `ai:cancel:{turn_id}=1`（沿用 step1 §5.3），Broker 向上游 provider 发 `abort`；**最后一个订阅者断开且未正常结束才真实取消**（防多设备误杀）。
- **重试策略**：客户端指数退避（上限 2s），最多 3 次；超阈提示「连接中断，请重试」而非静默转圈。

### 1.4 前端内容展示（Activity Panel 与对话线程分离）

> 调研强共识（Zylos/Ably 2026）：长任务必须有**独立审计/活动面**，不能只塞进线性聊天；否则用户看不到 Agent 在做什么 → 不敢中断 → 信任崩塌。

- **对话线程**：只显示「用户消息 / 最终回复 / 关键决策点」。
- **Activity Panel（折叠默认）**：步骤级进度，展开见工具调用输入/输出，再展开见原始请求响应。映射 step2 的 `tasks` DAG 与 `tool_calls`。
- **工具 IO 流式**：`tool` 事件随发生实时渲染（非等 Agent 完整响应），这是**最高价值 UX 提升**。
- **进度可见性优先于中断**：用户只能中断「看得见在跑」的 Agent（对应 §3 二次输入）。
- **3-part Error Surface**：错误必须给 `what`（具体失败）/ `why`（Agent 能判定的原因）/ `next`（具体下一步，非"重试"）三件套，降低任务放弃率。

### 1.4.1 阶段进度条（Stage Rail）：执行与显示的「一一对应」

> **核心回答**：Agent 执行的每一步**必须**与前端显示一一对应。除 `tool`(工具级) 与 `token`(正文级) 外，新增 **`stage` 事件**让 10 阶段（S0~S9）全程可见——每个阶段进入/离开各发一次，前端据此点亮「阶段进度条」。**用户任何时刻（含刷新/重连）都能看到"刚走到哪、在做什么"**。

- **三层可见性（粒度由粗到细，互不重叠，均来自 SSE 事件）**：
  1. **阶段进度条（Stage Rail，顶层）**：横向 10 节点 `S0 网关 → S1 理解 → S2 分类 → S3 合并 → S4 路由 → S5 校验 → S6 执行 → S7 装配 → S8 校验 → S9 归档`。每节点对应一个 `stage` 事件 `{stage, status:enter|leave, label, detail}`。当前阶段高亮+转圈，已完成打勾，未到置灰。**路由/校验等毫秒级阶段也发事件**——虽快，但保证任何时刻刷新都能看到"刚走到哪"。
  2. **Activity Panel（中层，任务/工具级）**：S6 执行期内，每个 Task + 每次工具调用发 `tool` 事件，实时填入步骤卡（输入/输出/状态）。对应 step2 的 `tasks` DAG 与 `tool_calls`。多意图时按 DAG 顺序展开各 Task。
  3. **对话线程（底层，正文级）**：`token` 增量渲染最终回复；关键决策点（澄清提问、`approval`、`capability_notice`）以消息/卡片插入。

- **映射关系（严格一一对应，无重载）**

  | 执行单元 | 触发事件 | 前端落点 |
  | --- | --- | --- |
  | 进入/离开某 Stage (S0~S9) | `stage` | 阶段进度条节点 |
  | 某 Task 起步/完成 | `state_diff`(DAG 节点状态) | Activity Panel 任务卡 |
  | 某工具调用 IO | `tool` | Activity Panel 步骤卡 |
  | 正文生成 | `token` | 对话线程 |
  | HIGH 工具待确认 | `approval` | 审批卡（进度条该阶段挂起） |
  | 降级告知 | `capability_notice` | Activity Panel 演示态提示条 |

- **示例时序（单意图 build_site）**：
  `stage(S0,enter)` → `stage(S0,leave)` → `stage(S1,enter)` → … → `stage(S4,enter,"路由: build_site 0.92")` → `stage(S4,leave)` → `stage(S5,enter)` → (缺槽则发澄清消息, **进度条停在 S5 等用户**) → `stage(S6,enter)` → `tool(site_design…)` → `tool(fs_write…)` → `stage(S6,leave)` → `stage(S7)` → `stage(S8,enter,"S8 校验通过")` → `stage(S8,leave)` → `stage(S9)` → `done`。

- **设计纪律**：① 绝不在 `token`/`tool` 里塞阶段信号（§1.2 原则）；② 每个 Stage 函数**必须在入口 emit `stage(enter)`、出口 emit `stage(leave)`**，由 `Pipeline` 装饰器统一包裹（避免漏发、保证对称）；③ 长 LLM 阶段（S2/S4/S6）`detail` 动态更新（如 `"S4 向量召回中…"` → `"S4 命中 build_site"`），让用户知道"在转圈但不是卡死"；④ 多意图时 Stage Rail 只显示主流程阶段，子 Task 进度落在 Activity Panel（按 DAG 缩进），避免进度条爆炸。

### 1.5 背压与心跳

- **后端背压**：`asyncio.Queue(maxsize=N)`，Producer（LLM 流）过快则阻塞，防 OOM；每 yield 后 `await request.is_disconnected()` 检测，断则 bail。
- **前端渲染**：小 chunk 高频追加；可对词级渲染加微延迟做视觉节奏（可选）。
- **心跳**：`Stream Broker` 每 **15s** 发 `:heartbeat\n\n` 注释，撑过 LB/nginx 60–120s 空闲超时（尤其"思考型"长首 token 场景）。
- **TTFT 监控**：记录 p95 TTFT 并告警，目标 chat < 700ms、snappy < 300ms。

### 1.6 客户端断网队列与乐观 UI 契约（SSE 客户端行为闭合）

> §1.3 解决了服务端断线重连，但**用户断网时这边的输入/UI 该怎样**未闭合。以下约定前端必须实现，否则会出现"发了消息没反应 / 重连后重复发 / UI 假死"。

1. **乐观渲染（Optimistic UI）**：用户提交消息 → 前端**立即**在对话线程渲染该气泡（状态 `pending`，灰色/半透明 + "发送中…"），**不等** `done` 事件；收到 `token`/`done` 后切换为 `sent` 并接管流式正文。断网期间这条消息标 `queued`（本地），不丢。
2. **离线输入队列（Offline Queue）**：
   - 检测 `navigator.onLine === false` 或 SSE `onerror` 连续超时 → 进入**离线模式**：用户在离线期间继续发的消息进**本地队列**（`localStorage`/`IndexedDB` 持久，避免刷新丢），每条带本地 `client_msg_id`（UUID）。
   - 重连成功（§1.3 `reconnect`/`Last-Event-ID` 恢复流）后，前端**逐条 flush 队列**到 `/api/chat`，附 `client_msg_id` 做幂等（服务端 §1.3 机制 B：同 `turn_id` 已完成直接返缓存，不重生成）。
   - 队列未 flush 完前，UI 顶部常驻"X 条消息待同步"提示条，不静默。
3. **重复发送防护**：`client_msg_id` 全局唯一；服务端 `ai:tool:idek` 同款幂等键逻辑延伸到 turn 级（`ai:turn:client:{client_msg_id}` 5 min TTL），同一 `client_msg_id` 二次到达直接返首次结果——杜绝"断网焦虑狂点发送"产生 N 份重复对话/重复计费。
4. **乐观状态回滚**：若某条 `queued` 消息最终 flush 失败（服务端拒绝/鉴权失效）→ 该气泡转 `failed`（红色 + "发送失败，点击重试"），不自动消失；用户手动点重试才重发，避免静默丢消息。
5. **长时间离线（数小时+）**：超过本地队列 TTL（默认 24h）或用户主动"稍后同步"→ 队列消息标记为 `draft-saved`，恢复时提示"您有 N 条草稿待发送"，由用户决定是否发出（防"离线乱点一堆后来又不想发"被一口气全发出去）。
6. **Activity Panel 离线态**：离线时 Activity Panel 冻结最后已知状态并显示"已离线，等待重连"，不显示假进度；重连后从 `reconnect`/快照增量补齐，不做"从零转圈"。

> 一句话：**发即显（乐观）、断即存（队列）、重即幂等（不重发不重计费）、败即留（可重试）、久即草稿（用户裁决）。** 把 SSE 客户端在弱网/断网下的行为也钉成契约，而非"看浏览器脸色"。

---

## 2. 需要用户操作的暂停（Pause / Approval Gate）

### 2.1 两类暂停

| 类型            | 触发                         | 实现                                            | 流状态              |
| --------------- | ---------------------------- | ----------------------------------------------- | ------------------- |
| **计划内 Approval Gate** | 命中 `risk=high` 工具（step1 §1.2，如 `site_delete`/`project/deploy`） | 原子 `SETNX ai:gate:approval:{req_id} pending`（step1 §5.3），推 `approval` 事件 | 推审批 UI，**停留等待** |
| **用户主动 Hold**       | 用户点「暂停」/中途打断（§3 中断类） | `ai:pause:{turn_id}=1`（决策 C **持久化**，断线不丢），并落 `paused_turns` 表（DB 长存）；Worker 在 checkpoint 轮询命中即 `TurnPaused`，保留 `done` Task | 流挂起，UI 显暂停态；数月后回来仍可恢复 |

### 2.2 暂停→用户操作→续跑/取消 时序（Approval Gate）

```
S6 执行命中 HIGH 工具
  → 原子 SETNX ai:gate:approval:{req_id} pending
  → SSE 推 event: approval  {req_id, tool, args_excerpt, rationale}
  → 前端弹审批卡（带「已做/将做/下一步」上下文, 非干巴巴 approve/deny）
  → 用户 approve / reject / modify
  → 前端回 POST /api/gate/{req_id}  {decision}
  → Lua: if GET==pending then SET approved|rejected  （防重复推进）
  → Worker 轮询到 approved → 执行工具；rejected → 抛 TurnCancelled，当前 Task cancelled
  → 超时(1800s)未决 → fail-safe 默认 rejected
```

### 2.3 持久审批上下文（断线不丢）

- `ai:gate:approval:{req_id}` TTL 1800s 落 Redis；**断线重连后前端重新拉 `/api/gate/pending` 恢复待审 UI**（pending approval 存于通道历史，不绑单一连接）。
- 用户可在另一设备/刷新后继续审批——满足 HITL 的组织侧交接（多设备）。
- **用户主动 Hold 同样持久化**（决策 C）：`ai:pause:{turn_id}` 之外，新增 `paused_turns` 表（step1 `seed_ai` 库）落 DB 长存——即便 Redis 过期/用户离开数月，前端重连后读 `paused_turns` 仍显示"该任务曾暂停，是否继续"，点继续即从 `ai:sir:snap:{cid}` 热快照 + 已完成 Task 续跑。

---

## 3. 用户二次输入对上一次结果的影响（六类操作）

> **本步扩展 step2 §1.6 的四类干预**（驳回中止/纠正方向/补充信息/元指令）为**显式六类操作**，并明确每类对上一次结果（SIR 快照、已产出 Task、`content_path`、会话）的影响。这是用户最关心的「中断/反驳/纠正/补充」落点。

### 3.1 六类操作分类

| 操作     | 用户典型表述                          | 归 step2 干预类 | 性质       |
| -------- | ------------------------------------- | --------------- | ---------- |
| **否定 negate**   | "这不对" / "不是这样" / "理解错了"     | 纠正方向        | 不采信上次 |
| **中断 interrupt** | "停" / "等等" / 主动点暂停            | 驳回中止        | 终止当前流 |
| **补充 supplement** | "再加个关于X的板块" / "用蓝底"         | 补充信息        | 增量合并   |
| **赞同 approve**   | "可以" / "继续" / 审批卡 approve       | 元指令→推进     | 放行       |
| **废弃 discard**   | "不用了" / "把刚才那个删了"            | 驳回中止(升级)  | 丢弃本轮产出 |
| **修正 revise**    | "把标题改红色" / "第二段重写"          | 纠正方向(定向)  | 定向 patch |

### 3.2 每类对"上一次结果"的影响矩阵

| 操作        | SIR（状态）                              | 已 done 的 Task / 产物                     | content_path 文件         | 会话/消息                |
| ----------- | ---------------------------------------- | ------------------------------------------ | ------------------------- | ------------------------ |
| **否定**    | 回滚到 `ai:sir:snap:{cid}` 上一轮热快照（step1 §5.1）；本轮 SIR 丢弃 | 受影响 Task 标 `cancelled`，重走          | 未发布则保留草稿，发布态降级为草稿 | 本轮 message 标 `needs_rework` |
| **中断**    | 冻结（不改写）                           | done 保留、running 标 `cancelled`、pending 丢弃 | 不动                      | 本 turn 不写最终 message（仅 partial，标 abandoned） |
| **补充**    | `DST.apply_delta`（step2 §3）增量合并新槽位 | 续跑后续 Task；受影响的 Task 重跑           | 追加新文件（新 version）   | 新 message 关联上下文     |
| **赞同**    | 无变更                                   | 推进被挂起的 Task                           | 无变更                    | 审批记录入 `tool_calls.approval_log` |
| **废弃**    | 无变更（或清本轮临时态）                  | 本轮全部 Task 标 `discarded`               | **未发布文件保留可恢复，已发布走回收站（step1 §3.3）** | 本轮 message 标 `discarded` 不计入正式历史 |
| **修正**    | 局部更新对应槽位                         | 仅定点 Task 重跑（如"改色"→只重渲染 CSS Task） | 覆盖该文件新 version       | 追加修正 message          |

### 3.3 实现机制

- **入口统一**：所有二次输入在 **S0 入口**先经 `InterventionClassifier`（step2 §1.6，走 `intent_lite`，不耗租户模型）判为「新意图」还是「六类干预之一」。是干预 → 生成 `CorrectionEvent` 注入当前/新建 `TurnContext`，**不新建完整 Turn**（除非否定后转为新意图）。
- **回滚热路径复用 A1 决策**：否定/修正依赖 step1 已定的 `ai:sir:snap:{cid}`（Redis LIST，热，LTRIM 10）+ `messages.sir_snapshot`（冷，行内联）。回滚 = 取上一轮快照覆盖当前 ctx.sir，无需重算。
- **安全退出**：Worker 在 checkpoint（每 Task 起步前 / 每次工具调用前 / 每步 ReAct Thought 前）轮询 `ai:cancel:`+`ai:pause:`，命中抛 `TurnCancelled`/`TurnPaused`（**非异常降级**，已 done 保留）。
- **与 Approval Gate 边界**：Approval（`ai:gate:approval`）是执行**前**计划内确认；干预（`ai:cancel` + `CorrectionEvent`）是执行**中/后**计划外打断，优先级高于一切。

---

## 4. 系统稳定性

### 4.1 连接模型：按"活跃连接"而非"RPS"容量规划

- 流式服务器按**并发活跃连接** sizing（30s 均值生成 @100 RPS ≈ 3000 并发连接）；uvicorn worker 数 / 反向代理连接上限相应调高；长异步运行时（uvicorn/starlette）优于线程每请求模型。
- LB 超时、`proxy_buffering off`、`gzip off` 三项缺一不可（否则首 token 从 300ms 飙到数秒）。

### 4.2 断连即 abort 上游 + 后写独立会话

- **客户端断连 → 立即 `abort` 上游 provider 调用**（停止计费，防 phantom compute）。
- **后处理写库**（`usage_ledger` / `session_audits` / `messages`）用**全新 DB session + `asyncio.shield`**，即便调用方被取消也要完成——否则遗留 "idle in transaction" 行锁耗尽连接池（已知反模式）。
- 禁止在 request-scoped session 里做 post-stream 写。

### 4.3 优雅关闭与管理系统优雅停止服务

**A. 零停机部署（SIGTERM，已有）**：收到 `SIGTERM` → `server.close()` 停止接新流 → 向活跃流发 `event: reconnect`（通知换实例）→ 等 ≤25s 让流完成 → 强关剩余 → `exit`。配合 LB draining 实现活跃生成中零停机发版。

**B. 管理系统优雅停止服务（admin 主动停机 / 维护）**：管理后台点「优雅停止」或运维发管理 API `POST /admin/system/drain`，进入 **drain 模式**（对齐老系统优雅停机能力，文档此前未系统写）：

1. **停止接收新请求**：网关（S0）立即拒绝新 Turn（返回 `503` + `Retry-After`），LB 摘流；已在跑的 Turn 不受影响。
2. **在飞任务收尾**：活跃 SSE 生成中的 Turn 允许其自然完成（≤ 优雅超时 T，默认 60s）；超 T 仍未完成 → 触发 §2 暂停持久化（`ai:pause` + `paused_turns`），用户回来可续跑，**不丢进度**。
3. **队列 flush（不丢事件）**：drain 期间 `persist_worker` 持续消费直到 `ai:stream:persist` 清空；`reconciler` 跑完 `ai:stream:error` 重试（§3.7.6）；确保 W2 批存与失败补偿全部落库后再退出（呼应 §3.7.7 实时统计视图同时归零）。
4. **后台 worker 收尾**：等待 `session_audits`/`agent_runs`/`memory_storage_log`/`flow_checks` 等 W1 后台写 + `log-reviewer` 完成当前批次。
5. **资源优雅释放**：按依赖逆序关闭 Chroma / MySQL / Redis 连接池、SSE 订阅、pub/sub 客户端。
6. **状态标记**：drain 全程写 `ai:system:draining=1`（带超时），管理后台实时可见；完成后 `exit(0)`。

- **约束**：drain 期间不接新业务，但**不破坏数据一致性**（所有写最终落库）；若进程被强杀（`SIGKILL`），重启后 §3.7.6 `reconciler` + §8 备份保证事件不丢。

### 4.4 熔断 / 降级

- **模型 Provider 熔断**：连续 N 次超时/5xx → 该 provider 进入 open 状态 T 秒，快速失败 + 落 `metrics_events(type=provider_circuit)`；可选切备用 provider。
- **嵌入降级**：`text-embedding-v3` 不可用时（step1 决策 1 云端），降级为本地哈希/缓存兜底（影响召回质量但不阻断）。
- **`intent_lite` 降级**：分类模型失败 → 退化为 L1 规则 + 默认 `low_conf` 进 S5（保可用，牺牲精度）。
- **S8 判定降级**：`intent_lite` 安全判定失败 → 默认放行的同时记 `output_guard_log(decision=error_fallback)`，不阻断主流程。

### 4.5 幂等（复用 step1 §1.5）

- 所有 MID/HIGH 工具实现 `idempotency_key`（由 `turn_id + tool + args_hash` 派生）；相同 key 重复调用返回首次结果，防网络重试导致双写/双删。

### 4.6 可观测性三件套（tracing + logging + metrics，A3 · 对齐老系统 `tracing.py`/`logging_config.py`/`analytics.py`）

> 老系统已有链路追踪 + 统一日志 + 埋点三件套，文档此前未系统写。新架构统一收敛到这三件，且与 §3.5 统计表同源、不重复建设。

**4.6.1 链路追踪（tracing）**
- 每个 Turn 生成 `trace_id`（= `TurnContext.trace_id`），**贯穿 S0–S9 全链路**；每个 Stage / Tool / LLM 调用挂 `trace_id` + `span_id`。
- 跨进程（planner / `persist_worker` / `reconciler`）经 Redis pub/sub 透传或随 payload 携带；查问题用 `trace_id` 拉全链路（结构化日志 + `flow_checks.state_excerpt`）。
- 阶段埋点复用 step2 `ai:stage:{id}:*`（每 Stage 自动打点，呼应 §1.4.1 阶段进度条）。

**4.6.2 结构化日志（logging）**
- 统一 `getLogger("app.<module>")`（呼应老纪律，**禁裸 `logging.warning` / `__name__`**）；输出 **JSON 结构化**（含 `trace_id`/`user_id`/`conv_id`/`level`）。
- **敏感脱敏**：token / key / PII 不上日志（呼应 step1 脱敏 + S8）；落 `backend/app/logs/<svc>.log`（按日滚动，呼应 step1 日志约定）。
- `flow_checks.log_ref` 指向具体日志位置（`app/logs/xxx.log:行号`），复查可精准溯源。

**4.6.3 指标与告警（metrics）**
- **埋点统一在 harness 层**（包装 LLM/工具 SDK 的唯一出口）：`latency_ttft` / `latency_total` / `token` 的 **p50 / p90 / p99** + 成功率 + 错误率。
- `/health` 暴露 `status / active_streams / total_tokens / failed / mem_mb / uptime`。
- 指标入 `metrics_events`（W2）→ 聚合 `metrics_daily`（`latency_p50/p90/p99` 列已建，§3.5）。
- **告警规则**：p99 超阈 / error_rate 超阈 / `ai:stats:persist:pending` 积压超阈（§3.7.7）/ `provider_circuit` 触发（§4.4）/ `undisclosed_mock_rate > 0`（§9.2 漏告知 Mock）。告警出口接 webhook / 管理后台通知。

**4.6.4 与统计系统衔接**：可观测指标与 §3.5 统计表**同源**（都来自 harness 埋点 + `metrics_events`），不另起一套；实时看板读 `ai:stats:*`（§3.7.7），历史分析读 MySQL，二者互补。

---

## 5. 多租户额度配置 + Token 额度检查

### 5.1 配额模型（四层原语，调研共识）

| 维度                | 落点（表/Redis）                          | 默认与收紧策略                         |
| ------------------- | ----------------------------------------- | -------------------------------------- |
| **Tier**            | `users.tier` ENUM(`free`/`pro`/`max`)     | 决定其余各项上限基线                   |
| **每日 token 预算** | `users.token_budget_daily` / `projects.token_budget_daily` | **决策 E**：`free`=用户 **5M/日**（项目不限或同 tier，见 §5.4）；`pro`/`max` 暂仍按 `free` 额度运行（**当前未开收费**，后续在 `config/quota.yaml` 分档覆写）；超阈返回 429+清晰文案 |
| **RPM**             | `ai:ratelimit:user:{uid}:rpm`（ZSET 滑动窗口，沿用 step1 §5.3） | 超阈排队或 429                        |
| **并发 session 数** | `ai:session:{conv_id}` 计数 + `users.max_concurrent_sessions`（**决策 F：统一默认 5**，tier 可调） | 防 session-proliferation 攻击          |
| **单 session 算力预算**（compute budget） | `ai:session:{conv_id}.token_budget`（**决策 F：统一默认**，如 2M token/会话，tier 可调） | 超预算优雅总结状态并终止，防 runaway loop |

### 5.2 网关预检 + 事后对账（防流式漏记）

- **预检（S0 入口）**：先查 Redis 滑动窗口 + 每日固定窗口（`ai:ratelimit:user:{uid}:cost_daily`，沿用 step1），超阈直接 429，**不进 Pipeline**。
- **事后对账**：每个 LLM 调用在 **harness 层**（包装 SDK 的唯一出口）打 `user_id`+`conversation_id`+`feature` 标签（lint 禁止直连 SDK）；用 **provider 响应里真实返回的 token 数**写 `usage_ledger`（step1 §3.2）。**流式尾部 `usage` 事件兜底**（首屏已生成但尾部未记时补全），避免 mid-stream 断连丢 token 计数（已知 5–15% 漏记）。

### 5.3 渐进收紧（progressive tightening）

- 接近阈值（如 80%）→ RPM 收紧 + 返回 `Retry-After` 提示；超过 → 硬 429。
- 单租户偏离 30 天基线 3× → `metrics_events(type=anomaly)` 告警（cost-to-revenue 监控）。
- 429 结构化错误：`{code:"quota_exceeded", scope:"daily_token", limit, used, retry_after}` → 前端不崩溃，显「今日 AI 额度已用完」。

### 5.4 表结构增补（在 step1 `seed_ai` 库上）

```sql
-- users 扩列（step1 §3.1 users 表 ALTER）
ALTER TABLE users
  ADD COLUMN tier ENUM('free','pro','max') NOT NULL DEFAULT 'free',
  ADD COLUMN token_budget_daily BIGINT UNSIGNED NOT NULL DEFAULT 5000000,   -- 决策 E: free=5M/日
  ADD COLUMN max_concurrent_sessions SMALLINT UNSIGNED NOT NULL DEFAULT 5;

-- projects 扩列（step1 §3.1 projects 表 ALTER）
ALTER TABLE projects
  ADD COLUMN token_budget_daily BIGINT UNSIGNED NOT NULL DEFAULT 5000000;   -- 决策 E: 当前未分档, 暂与 free 同额
```

> **决策 E 落实**：当前**未开收费**，三档 tier 暂共享 `free` 额度（5M/日），不强制 pro/max 差异化；后续真要分档时在 `config/quota.yaml` 覆写 `token_budget_daily` 与 `max_concurrent_sessions`。`usage_ledger`（step1 §3.2）已有 `token_input/token_output/cost_usd`，作为按 user/conversation/feature 的对账源。`metrics_daily`（step1 §3.5）按日聚合。配额**不建新聚合表**，复用既有统计域。

---

## 6. 用户自定义模型 AppKey（BYOK）

### 6.1 密钥隔离（信封加密，调研共识）

- 新增表 `user_model_keys`（在 `seed_ai` 库）：

```sql
user_model_keys (
  id BIGINT UNSIGNED PK AUTO_INCREMENT,
  user_id BIGINT UNSIGNED FK→users(id),
  provider VARCHAR(32) NOT NULL,         -- openai|anthropic|google|deepseek|qwen|hy3|openrouter
  api_key_enc VARCHAR(512) NOT NULL,     -- AES-256-GCM 密文, 格式 {iv}:{ct}:{tag}
  base_url_override VARCHAR(255) NULL,   -- 自建网关/兼容端点(可选)
  model_map JSON NULL,                   -- {standard:..., pro:..., ultra:...} 租户自映射档位
  is_valid BOOLEAN DEFAULT NULL,         -- 写入探针结果
  last_validated_at TIMESTAMP NULL,
  created_at TIMESTAMP,
  UNIQUE KEY uq (user_id, provider),
  KEY idx_user (user_id)
)
```

- **加密**：`PROVIDER_ENCRYPTION_KEY`（64 hex，缺失启动即 fail-closed）做 AES-256-GCM；IV 12B 随机（OS CSPRNG，不复用），auth tag 16B 防篡改。
- **tenant 解析**：`user_id` 从 **JWT/会话**（绝不从请求体），防 `X-Tenant-Id` 注入跨租户读 key。
- **明文窄作用域**：解密仅在 harness 调用当次内存存在，注入后**立即 delete**；不写日志/DB/span/响应体。
- **响应只返前缀**：API 返回 `sk-...(后4位)`，绝不返完整 key。

### 6.2 写入探针校验

- 保存 key 前 **probe**（拿 key 调一次最小请求验证可用），失败 `fail-fast` 报「密钥无效」；成功后 `is_valid=true`。

### 6.3 凭证解析 `resolve_model_credential(tier, user_id)`

```
if user_model_keys 含该 tier/provider 的有效 key:
    return (tenant_key, base_url_override or provider_default)
else:
    return PLATFORM_CREDENTIALS[tier]   # step2 §1.4 平台档位凭证
```

### 6.4 BYOK 适用边界（决策 D：不覆盖平台模型）

- **BYOK 仅应用于 S6 执行三档**（`exec_standard/pro/ultra`）——租户自带 key 走租户账号计费，平台不 markup。
- **平台固定模型（`intent_lite`/`intent_strong`/`text-embedding-v3`）一律走平台凭证**，保证 S4 意图判断 / S8 结果判定 / S2 理解 / 嵌入**与租户自费档彻底解耦、质量与成本恒定可复现**（呼应 step2 决策 5）。
- **不允许**租户 BYOK 覆盖 `intent_lite` 等平台模型——覆盖会导致租户判定质量/成本失控、且违反"低价租户判定不劣化"铁律。租户若要更强执行能力，只能在 S6 三档内自选/接 BYOK（生成质量差异只体现在 S6）。

### 6.5 计费归属

- BYOK 模式下 token 费**直接计租户 provider 账号**，平台可加可选 markup（写入 `usage_ledger.cost_usd` 仅作展示/对账，不从平台扣）。非 BYOK 模式计平台套餐额度（step2 §1.4 token-plan）。

---

## 7. 我额外补充的 production-grade 点（调研发现，你未提但必做）

1. **多设备同步（防 double-spend / drift）**：同 `conversation_id` 的流走**同一 channel**（`ai:stream:{stream_id}` 扇出）——笔记本+手机同时开同一会话，Broker 让第二个订阅者**挂到进行中的生成**而非重启 provider 调用。这是"phantom compute"的根治，不是可选项。
2. **上下文隔离 / Bulkhead（防 noisy neighbor）**：执行层按租户/项目分资源池（UVicorn worker 隔离 / 未来 K8s namespace + resource quota），单租户 token 风暴不拖垮全局；agent 可上传自定义 tool/prompt 的高危租户用 MicroVM 隔离。
3. **流式尾部 `usage` 兜底**（§5.2 已含）：mid-stream 断连最常导致尾包 token 漏记，显式 `usage` 事件 + `usage_ledger` 对账堵住 5–15% 漏记。
4. **Prompt caching 友好**：系统提示/长脚手架做前缀缓存键（provider 支持时），降首 token 成本与延迟——纳入 harness 层，不对业务暴露。
5. **3-part Error Surface**（§1.4 已含）：结构化错误 what/why/next，降任务放弃率。
6. **计费透明前端**：设置页明示 `exec_ultra=deepseek 按量`（step2 §1.4 已标），BYOK 状态下明示「本对话由你的 key 计费」。

---

## 8. 灾备与数据备份（生产级必做，调研补强）

> 前三步覆盖了"运行态"，但**灾难恢复（RTO/RPO）缺位 = 单点故障即全损**。以下为各存储的备份/恢复策略（MySQL + Chroma + COS + Redis 四件套），纳入运维 SOP。

### 8.1 备份对象与 RTO/RPO 目标

| 存储 | 数据性质 | 备份方式 | 频率 | RPO | RTO | 恢复手段 |
| --- | --- | --- | --- | --- | --- | --- |
| **MySQL (`seed_ai`)** | 业务+统计真相源 | 逻辑 `mysqldump`（全量+增量 binlog）到对象存储 | 全量每日 03:00；binlog 实时 | < 5 min（binlog 回放） | < 30 min | `mysql < dump` + `mysqlbinlog` 追平 |
| **Chroma（物理隔离集合）** | 用户/项目向量 | 集合导出 JSON（含 embeddings + metadata）到 COS | 每日 + 集合变更后异步快照 | < 24 h（或按集合版本） | < 1 h | 重建 Collection + `upsert` 导入 |
| **COS（`site-deploy` 生产桶 + `previews` 本地目录）** | 用户静态产物 | 桶版本控制 + 跨区复制（同云另一 AZ）；本地 `previews/` 每日 rsync 到备份卷 | 持续版本化；rsync 每日 | 版本化=0（可回旧版）；rsync RPO<24h | < 15 min（切版本/复制） | `coscli restore` / 副本切换 |
| **Redis（`ai:*` 热状态）** | 可重建热缓存 | RDB 快照（AOF 可选 `everysec`）持久化到本地卷 | RDB 每 15 min；AOF 每秒 | < 15 min | < 10 min | 重启加载 RDB（热态丢了也可从 MySQL 重建，见 §1.3 A'） |
| **`libs_index.json` + `shared/vendor/libs/`** | 本地库真相源 | 随代码仓库 Git 管理 + 发布时打包 | 随发版 | 0（Git 历史） | < 10 min | 重新拉取 / 回滚 commit |

### 8.2 关键原则

1. **热态可丢，真相源不可丢**：Redis 断档不影响正确性（§1.3 已设计从 MySQL 重建 `ai:sir:snap`、从 `content_path` 重建产物 UI），故 RDB 仅"加速冷启动"而非强制。MySQL 与 COS 才是**不可重建真相源**，必须跨区/异地留存。
2. **统计系统优先保全**：`qc_scores`/`flow_checks`/`metrics_daily`/`usage_ledger`/`output_guard_log` 是合规与计费依据（step1 §3.5），备份优先级 **高于** 业务内容表——即便业务表可重建，统计记录丢失也无法挽回。
3. **隔离备份凭据**：备份写入对象存储用**独立最小权限子账号**（仅 `put` 备份前缀），与生产 `site-deploy` 桶凭据分离，防"被入侵即删库删备"。
4. **加密静止**：备份文件静态加密（KMS/平台密钥），落盘即密文；还原时仅运维通道解密。
5. **演练（Drill）**：每季度做一次**恢复演练**（从备份恢复一个隔离实例，校验数据完整 + 路由可达），记录 `RTO/RPO` 实测，不演练的备份=没备份。

### 8.3 误删/灾难分级响应

- **单项目误删（`project_purge` 真删）**：回收站 `trashed→purging` 有异步 job 窗口（step1 §3.3），job 未完成可从 `previews/` 与 binlog 救回；job 完成则依赖 8.1 的 MySQL dump + COS 版本 + Chroma 快照三方还原到删除前时间点。
- **库级损坏**：用最近全量 dump + binlog 追平；统计表单独优先恢复。
- **机房间断网**：COS 跨区副本接管读；MySQL 主从切换（若用云 RDS 高可用则自动）。

> 一句话：**真相源双备份（异地+版本化）、热态可重建、统计优先、凭据隔离、季度演练。** 灾备不是"有备份脚本"，是"演练过能恢复"。

---

## 9. 网页生成能力边界（原生支持 / 降级支持 / 硬红线）

> 明确本平台**生成的产物形态**：**① Markdown 文档**（需求文档、说明、文案，存 DB `messages`/文件，前端渲染为富文本/预览）；**② 无服务端静态网站**（纯前端 HTML/CSS/JS，运行在浏览器，可用浏览器本地存储 `localStorage`/**IndexedDB 本地数据库**）。
>
> **核心策略（与旧版最大差异）**：**不做"不支持清单"，做"降级清单"。** 用户明确要求的可操作功能，一律优先**降级为静态按钮 / Mock 假数据 / 浏览器本地数据库**并**做出来可用**，同时**强制告知演示态边界 + 给出后续追加「HTML 本地数据库服务」的升级路径**。仅安全合规与超平台范畴的极少数项目为硬红线。

### 9.1 L0 原生支持（纯前端静态 / 文档范畴，无降级、无告知负担）

| 类别 | 具体能力 | 产物形态 | 说明 |
| --- | --- | --- | --- |
| **页面结构** | 单页 / 多页落地页、官网首页、产品介绍页、作品集、博客（静态生成）、简历页、活动页 | HTML + CSS（可 Tailwind/原子化或原生） | 多页 = 多个 `.html` 入口 + 共享 CSS/JS |
| **视觉表现** | 响应式布局、暗色/亮色主题、动画（CSS/轻量 JS）、轮播、Modal、Tab、折叠面板、滚动视差 | HTML/CSS/JS | 不依赖框架后端 |
| **前端交互** | 表单（前端校验+本地暂存）、Tabs/Accordion、简易计算器、单位换算、静态图表（Chart.js，**本地 /vendor/libs/chart.js**）、SVG 插画 | HTML/CSS/JS + **本地 /vendor/libs 库**（禁止外引 CDN） | JS 仅跑在浏览器 |
| **本地持久化** | `localStorage`/`sessionStorage`、**IndexedDB 本地数据库**（多表 CRUD/索引/事务，原生 IDB 即可，无需额外库）、JSON/CSV 导入导出 | HTML/JS | 数据存用户本机浏览器，**真实可用**（详见 §9.2.1） |
| **内容展示** | 图文混排、时间线、卡片、画廊（前端分页/懒加载）、地图嵌入（iframe 公共地图）、视频/音频嵌入（公共 URL） | HTML/CSS | 嵌入用第三方公共资源 |
| **文档类** | 需求文档、PRD、说明书、博客 Markdown、README、营销文案 | `.md` / 渲染后 HTML | 存 DB + 文件，前端预览 |
| **SEO 静态** | 每个 HTML 自带 meta/OG、sitemap.xml、robots.txt（静态） | 文件 | 纯静态即可 |
| **部署形态** | 本地预览目录（`site_publish`，nginx 托管）+ 生产桶上传（`site_deploy`，step1 §1.2） | 静态文件 | **无构建流水线要求**，原生 HTML 直接托管 |
| **数据可视化（静态）** | 用硬编码 JSON / 内嵌数据集渲染图表（Chart.js，**本地 /vendor/libs/chart.js**；3D 可用 three.js 本地库） | HTML/JS | 数据写死在前端，非实时拉取 |

### 9.2 降级支持（Graceful Degradation）——**默认策略：先做出来，再说清楚**

> **总原则（决策 G）**：用户明确要求的"可操作功能"，**不因为没有后端就拒绝**。一律走三级降级链：
> **L0 原生支持** → **L1 降级实现（静态按钮 / Mock 数据 / 浏览器本地库）** → **L2 硬红线（极少数，仅安全合规与超平台范畴）**。
> **只要落在 L1，必须：① 做出来可点可用；② 明确告知这是演示态；③ 给出后续可追加"HTML 本地数据库服务"的升级路径。**

#### 9.2.1 本地持久化三梯度（"HTML 本地数据库"能力阶梯）

| 梯度 | 载体 | 容量/生命周期 | 适用 | 默认档 |
| --- | --- | --- | --- | --- |
| **T0 内存态** | JS 变量 / 内嵌 JSON | 刷新即失 | 纯展示、演示列表、图表假数据 | 展示类默认 |
| **T1 localStorage** | `localStorage` / `sessionStorage` | ~5MB，同浏览器长存 | 表单暂存、主题偏好、购物车、Mock 登录态 | **交互类默认** |
| **T2 IndexedDB（本地数据库）** | IndexedDB（可选 Dexie CDN 封装） | 数十~数百 MB，结构化+索引+事务 | 多表 CRUD、Mock 后台管理、离线笔记/记账/客户表 | **用户要"能增删改查/像个系统"时自动升 T2** |

> T2 即用户所说的「**HTML 本地数据库**」。**当前版本已可生成 T2 代码**（IndexedDB 建库建表 + CRUD + 简易查询），只是数据**只存在用户本机浏览器**、不跨设备、不跨人共享。后续任务可把 T2 升级为"平台托管的本地数据服务"（见 9.2.4 升级路径）。

#### 9.2.2 降级映射表（原"不支持"→ 现"这样做"）

| 用户诉求 | 降级实现（L1，默认交付） | 保留的限制 | 告知话术要点 |
| --- | --- | --- | --- |
| **登录 / 注册 / 会员** | 生成完整登录/注册 UI + 前端校验 + **Mock 鉴权**（内置演示账号 或 注册即写 T1/T2），登录态存 `localStorage`，受保护页做前端跳转守卫 | 无真实鉴权，前端可绕过；**不用于真实敏感数据** | "登录为演示态：账号存在你的浏览器本地，未做服务端校验，不能用于真实用户体系" |
| **后台 / CMS 内容管理** | 生成**可用的 Mock 后台**：列表/新增/编辑/删除/分页/搜索，数据落 **T2 IndexedDB**，带"导出 JSON / 导入 JSON"按钮 | 数据仅本机；换设备/清缓存即丢 | "后台真实可操作，数据存浏览器本地数据库(IndexedDB)，可导出备份" |
| **表单提交 / 收件** | 前端校验 + 提交后**成功态反馈** + 记录落 T1/T2 + "导出 CSV"；可选预留 `FORM_ENDPOINT` 常量（用户自填第三方表单服务 URL 即真实生效） | 默认不真实外发 | "提交数据存本地并可导出；填入你的表单服务地址即可真实收件" |
| **购物车 / 下单** | 商品列表 + 加购 + 数量增减 + 小计 + 结算页 + **模拟订单号**，购物车与订单落 T1/T2 | 无真实支付、无真实库存 | "下单为演示流程，生成模拟订单号，不产生真实支付" |
| **支付** | 生成支付 UI（方式选择、金额确认、结果页），走**模拟支付流程**（延时 + 成功/失败态） | ❌ 不接真实支付渠道、不写密钥 | "支付为模拟演示，未接入任何真实收单渠道" |
| **搜索 / 筛选 / 排序** | 前端全量数据本地检索（内嵌 JSON 或 T2 索引） | 数据集为静态/本地 | "搜索基于本地数据，非实时全网/服务端检索" |
| **仪表盘 / 数据报表** | Chart.js/ECharts + **合成假数据**（结构真实、数值 mock），带"替换数据源"注释锚点 | 非实时 | "图表数据为示例数据，可替换为你的真实数据文件" |
| **实时聊天 / 客服** | 前端模拟对话（预设应答 / 本地回声），消息落 T1 | 无 WebSocket 服务端、非真人 | "聊天为前端演示，消息只在本机" |
| **评论 / 留言板** | 留言 CRUD 落 T2，带昵称/时间/删除 | 仅本机可见，非多人共享 | "留言存本地，别人打开看不到你的留言" |
| **数据导入导出** | ✅ **真实可用**：JSON/CSV 导出下载、文件导入解析（FileReader） | 无 | 无需降级说明 |
| **多语言 / 国际化** | ✅ 前端 i18n 字典切换 | 无 | — |
| **访问统计** | 生成埋点占位 + 本地计数；或预留第三方统计脚本插槽 | 本地计数不跨设备 | "统计为本地计数，接入第三方统计脚本可真实生效" |
| **SSR / 构建框架站点** | 默认产出**原生静态站**（等价效果）；用户坚持要框架源码 → 产出源码包，**部署由用户自理**（不走 `site_deploy`） | 平台不执行 `build` | "已用原生静态实现同等效果；如需框架源码包需你自行构建部署" |

#### 9.2.3 L2 硬红线（**仅此四类**，不可降级）

| 类别 | 原因 | 处理 |
| --- | --- | --- |
| **真实服务端可执行代码 + 平台托管运行** | 产物形态与托管形态不支持（静态托管，无运行时） | 可生成"源码包"交付给用户自托管，但**平台不部署、不运行**；不假装已上线 |
| **真实凭证 / 密钥落前端** | 安全红线 | 支付密钥、数据库口令、第三方 secret 一律不写进静态产物；改用占位常量 + 说明 |
| **真实 PII / 敏感数据处理** | 合规红线（呼应 step1 脱敏、S8 判定） | 不引导把身份证/手机号/病历等写进前端 JS；演示一律用假数据 |
| **域名 / ICP 备案 / HTTPS 证书代办** | 超出平台范畴 | 平台只管静态托管（本地 nginx + COS），其余给指引不代办 |

> 除以上四类外，**默认不得回答"做不了"**——必须给出 L1 降级方案。

#### 9.2.4 升级路径话术（每次降级交付都要附）

> "当前 XX 功能已按**演示态**实现（数据存在你的浏览器本地数据库）。
> 后续任务可以追加：**① 升级为 HTML 本地数据库服务**（IndexedDB 结构化多表 + 索引 + 事务 + 导入导出备份）；**② 接入第三方 BaaS/表单服务**（你提供地址与 key 即真实收发）；**③ 平台后续提供托管数据服务**后一键切换为真实持久化。
> 需要的话直接说'把 XX 升级成本地数据库版'即可。"

### 9.3 降级执行机制（与既有阶段对齐）

- **意图层（S4）**：`build_site` 命中后，Skill `policy.py` 注入系统约束改为**降级导向**——"你只能产出静态 HTML/CSS/JS；遇到需要后端的功能，**不得拒绝**，必须用 静态按钮 / Mock 数据 / localStorage / IndexedDB 实现可交互的演示态，并在 `degradations[]` 中登记"。
- **规划层（S5）**：Planner 输出 `degradations[]`（每项含 `feature` / `tier`(T0|T1|T2) / `limitation` / `upgrade_hint`），随计划一并展示给用户，用户可当场改主意（走 step3 §3 二次输入通道）。
- **产物层（强制三处告知）**：
  1. **对话回复**：结尾固定「能力说明」段，列 `degradations[]` + 9.2.4 升级路径话术；
  2. **页面内**：注入可关闭的 `demo-notice` banner（如"演示态：数据仅存本机浏览器"），并在相关 JS 上方加 `/* MOCK: 演示数据，替换点 */` 注释锚；
  3. **SSE 事件**：新增 `capability_notice` 事件（`{feature, tier, limitation, upgrade_hint}`），前端 Activity Panel 实时展示，不必等收尾。
- **校验层（S5/S8）**：`output_guard_log` 规则调整——
  - 原 `rule=no_backend` 由 **block 降为 warn**，且仅在"产物声称接了真实后端但实际没有"时触发（防止**假承诺**，而非阻止降级）；
  - 新增 `rule=undisclosed_mock`（**block**）：检出 Mock/假数据/本地存储但 `degradations[]` 为空或页面无 `demo-notice` → 拦截并要求补告知（**"可以假，不可以不说"**）；
  - 新增 `rule=secret_in_static`（**block**）：静态产物中检出疑似真实密钥/口令；
  - 新增 `rule=pii_in_static`（**block**）：检出疑似真实 PII。
- **数据留痕**：`degradations[]` 随 turn 落 `messages` 元数据（step1 `content_path` 同级 JSON 字段）+ 写 `flow_checks`，便于后续"升级成本地数据库版"时精准定位替换点。
- **前端模板库**：模板（step2 `applyTemplate`）同步补充**降级态模板**（Mock 后台、Mock 登录、IndexedDB CRUD 骨架），从源头保证降级实现质量一致。
- **用户误请求兜底（改写）**：用户说"给我做个带后台能登录的网站" → **不再拒绝**，直接产出「Mock 登录 + IndexedDB 后台 CRUD」可用站点，并在回复中用 3-part（what 做了什么 / why 为何是演示态 / next 如何升级）说明。只有落入 9.2.3 四类硬红线时，才走原"超边界"3-part 拒绝路径。

> 此策略写入 Skill 系统约束（step1 §2 `site_build.policy`）+ `intent_catalog` 的 `build_site` 能力说明：**"能降级就不拒绝，降级必告知，告知必给升级路径"**。

#### 9.4 本地预置组件库（/vendor/libs）清单与接入规范

> 平台**已离线预置一批常用前端库**（存于 `backend/shared/vendor/libs/`，由 `_download_libs.py` 拉取、`libs_index.json` 登记）。生成站点**按需引用所用库**（非全量引入），且一律**根绝对路径 `/vendor/libs/<name>/<file>` 引用**，预览（本地 nginx / 单进程 `/vendor/{path}` 路由）与生产（COS 桶 `site-deploy`）**均复用同一份，无需重复下载、无跨域、无离线白屏**。

**9.4.1 已预置库清单（覆盖绝大多数页面需求）**

| 类别 | 库（白名单名 → 全局变量） | 用途 | 本地引用示例 |
| --- | --- | --- | --- |
| **CSS 框架/原子化** | `tailwindcss`→tailwind（4.x 浏览器运行时，无构建） | 原子化样式，Vue/React 站点首选 | `/vendor/libs/tailwindcss/index.global.js` |
| | `bootstrap`(5.3)→bootstrap + `bulma`(1.0) | 组件化 CSS 框架，class 驱动 | `/vendor/libs/bootstrap/bootstrap.min.css` + `bootstrap.bundle.min.js` |
| | `animated`→animate.css / `fontawesome`→fa6 图标 | 动画类、图标字体（纯 CSS） | `/vendor/libs/animated/animate.min.css`、`/vendor/libs/fontawesome/all.min.css` |
| **JS 框架** | `vue`(3.4)→Vue / `react`+`react-dom`(18) / `alpinejs`(3)→Alpine / `jquery`(3.7) | 响应式/声明式/命令式交互 | `/vendor/libs/vue/vue.global.prod.js`、`/vendor/libs/react/react.production.min.js`、`/vendor/libs/react-dom/react-dom.production.min.js`、`/vendor/libs/alpinejs/cdn.min.js`、`/vendor/libs/jquery/jquery.min.js` |
| **动画/动效** | `gsap`(3.12)→gsap / `aos`(2.3)→AOS | 时间轴动画、滚动进场 | `/vendor/libs/gsap/gsap.min.js`、`/vendor/libs/aos/aos.js` + `aos.css` |
| **轮播/媒体** | `swiper`(11)→Swiper / `glightbox`(3)→GLightbox | 幻灯片、图片/视频灯箱 | `/vendor/libs/swiper/swiper-bundle.min.js` + `.css`、`/vendor/libs/glightbox/glightbox.min.js` + `.css` |
| **图表** | `chart.js`(4.4)→Chart | Canvas 折线/柱/饼图（§9.2 数据可视化 T0–T2 默认选它） | `/vendor/libs/chart.js/chart.umd.min.js` |
| **组件/工具** | `sweetalert2`(11)→Swal / `sortablejs`(1.15)→Sortable / `clipboard`(2.0)→ClipboardJS / `dayjs`(1.11)→dayjs / `highlight`(11)→hljs / `three`(0.15)→THREE / `htm`(3)→htm | 弹窗、拖拽排序、复制、日期、代码高亮、3D/WebGL、无构建 JSX | 对应 `/vendor/libs/<name>/<file>` |
| **自研设计系统** | `seed-premium`（玻璃拟态，已随页内联，非 `/vendor` 引用） | 平台默认观感，零依赖 | 由 `ensure_vendor()` 自动注入 `<head>`/`<body>` |

**9.4.2 覆盖度结论**

- ✅ **足够覆盖绝大多数网页生成**：CSS 框架、JS 框架、动画、轮播/灯箱、图表、常见组件/工具、3D 一应俱全；配 §9.2 的 Mock/本地数据库降级策略，常规官网/落地页/作品集/简历/仪表盘/简易后台/博客几乎全覆盖。
- ⚠️ **当前未预置、需要时补下载**（记入 `libs_index.json` 并随 `_download_libs.py` 落盘即可，无需改生成逻辑）：`dexie`（IndexedDB 友好封装，原生 IDB 也可用故非必需）、`echarts`（重型图表，可用 chart.js 替代）、`marked`/`dompurify`（md→html 渲染，文档类站点用）、`flatpickr`/`lodash`/`axios`(本地 fetch 替代)等。

**9.4.3 接入规范（生成时怎么用上这些本地库）**

1. **引用路径严格根绝对**：一律 `<script src="/vendor/libs/<name>/<file>">` / `<link href="/vendor/libs/<name>/<file>">`；**禁止** unpkg/jsdelivr/cdn.tailwindcss/cdnjs 等外部 CDN（沙箱/离线预览会白屏变灰块），这是白名单硬约束（见 9.4.4）。
2. **LLM 引导**：`LIBS_REFERENCE`（由 `libs_index.json` 自动生成）注入 SYS_CODER，列出每个库的精确路径与全局变量，模型只能从白名单选。
3. **三级解析链路（预览→生产一致）**：本地预览靠单进程 `/vendor/{path}` 路由直托管 `shared/vendor/`；生产 `site_deploy` 上传 COS 时，**必须把 `shared/vendor/libs/` 整目录一并复制进桶根**（保持 `site-deploy/vendor/libs/...`），否则根路径 404（见 9.4.4 风险 R1）。
4. **不内联大库**：`ensure_vendor()` 只内联轻量的 `seed-premium`（~16KB）。第三方库**走引用而非内联**，避免单页几十 KB→MB 膨胀；确需单文件离线（如交付源码包）再改用内联变体。
5. **按需引入，禁止全引/乱引**：`/vendor/libs/` 全量预置在**服务器与 COS**上（供任意页面取用），但**单个生成页只引用其实际用到的 1–3 个库**——例如纯展示页只引 Tailwind+FontAwesome，带图表页才加 Chart.js，绝不开头一股脑 `<script>` 引 25 个。判断规则：**页面 HTML/内联 JS 里实际 `new/调用/选择器` 用到的库才列入 `<head>` 引用**，未用到的不引入（见 9.4.4 `unused_vendor_ref` warn）。

**9.4.4 接入校验与安全（并入 S8 `output_guard_log`）**

| 规则 | 级别 | 触发 | 处理 |
| --- | --- | --- | --- |
| `block_external_cdn` | **block** | 产物检出 `unpkg`/`jsdelivr`/`cdn.tailwindcss`/`cdnjs`/`cdn.jsdelivr`/`code.jquery` 等外引 script/link | 拦截，强制改写为 `/vendor/libs/` 本地引用或删去；回写 `degradations[]` 告知 |
| `vendor_path_exists` | **warn** | 引用的 `/vendor/libs/<name>/<file>` 不在 `libs_index.json`（库未预置） | 告警，提示先 `_download_libs.py` 补齐或改用已预置库 |
| `unused_vendor_ref` | **warn** | `<head>` 引入的 `/vendor/libs/*` 在页面 HTML/内联 JS 中**完全未被实际调用** | 告警并提示剥离未用引用，避免无谓体积与"全引"陋习 |
| `missing_vendor_on_deploy`（**R1 部署遗漏**） | **block（部署阶段）** | `site_deploy` 上传 COS 前，扫描产物引用了 `/vendor/libs/*` 但 `site-deploy/vendor/` 未一同上传 | 部署脚本自动把 `shared/vendor/libs/` 同步进桶根再上传，缺则中止并提示 |

> 一句话规矩：**库用本地的、路径用根的、部署要带桶。** 既不依赖外网，又保证预览与生产一致，且不破坏 §9.2「可断网运行」的设计初衷。

#### 9.4.6 本地库版本管理 / 升级 / CVE 处理 / 完整性校验

> 预置库不是"下完就完"。它需要像依赖管理一样有**版本固定、可升级、可审计、防篡改**的生命周期。

1. **版本固定（`libs_index.json` 锁版）**：每个库登记 `version` + `source_url` + `sha256` + `added_at`。生成引用时绑定白名单版本，**不允许页面写 `?v=xxx` 或运行时拉其他版本**——保证预览/生产字节一致、可复现。
2. **升级流程（不破坏在产页面）**：
   - 升级某库 → 下载新版本到 `shared/vendor/libs/<name>/<newver>/`，**保留旧版本目录**；`libs_index.json` 切 `latest` 指向新版，但旧页面若硬编码旧路径仍可用（路径含版本则永远存在）。
   - 全量替换风险：新主版本（如 Tailwind 3→4）Breaking Change 多，默认**不自动升大版本**，需人工跑 `_download_libs.py --upgrade <name>` + 回归 `html_validate` + 抽样产物页面截图比对（`browser_capture`）确认无回归再切 `latest`。
3. **CVE / 安全响应**：
   - 维护 `libs_index.json` 的 `known_cve`（管理员填：某版本有 XSS/原型链污染等）→ 生成时若页面引用的版本命中 `known_cve`，S8 `output_guard_log` 记 `warn` 级 `vuln_lib_version` 并提示升级；高危 CVE 可设 `block_until_upgraded=true` 阻断部署（运营决策）。
   - 原则上本地静态引用**不引入运行时供应链攻击面**（不跑 npm install、不从 CDN 拉），但旧版本自身漏洞仍需关注（如旧 jQuery XSS）。
4. **完整性校验（防篡改 / 下载损坏）**：
   - 服务启动自检 + 定期 cron：逐个库文件算 `sha256` 比对 `libs_index.json` 登记值；不一致 → 标记 `integrity_failed`、告警、自动从 `source_url` 或 Git 历史重拉（落盘后重新校验）。
   - 部署 `site_deploy` 上传 COS 前，对本次引用的每个 vendor 文件再做一次 sha256 比对（与本地一致才传），防"本地被改但传了旧/坏文件"。
5. **空间与清理**：旧版本目录长期保留占用磁盘，设 **保留策略**（如每个库最多留 2 个历史大版本 + 当前 `latest`），超出由 `_download_libs.py --gc` 清理并同步更新 `libs_index.json` 引用计数（无页面引用旧版本才真删）。
6. **审计**：每次新增/升级/删除库动作落 `app/logs/libs_changelog.log`（who/what/version/sha256），与 §8.2 备份凭据隔离的运维通道一致。

> 一句话：**版固定、升不破产、CVE 可拦、sha 可校、旧版可留、动作可审。** 让本地库从"下载一堆文件"变成"受控的依赖资产"。

#### 9.4.5 用户中途要求"不存在的库 / 未预置的技术"如何处理

> 用户中途（对话补充、否定后改口、或首次就点名）要求用到某库或技术时，分两类：**(a) 真实存在但本平台未预置**；**(b) 根本不存在 / 已废弃 / 与无后台静态形态矛盾**。处理原则呼应 §9.2「可以假，不可以不说」+ step2 用户中途反驳/补充的 HITL 路由：**先判定、再分流、必告知、给替代**。

**9.4.5.1 初判（S4/S5 可用性 check）**

- 路由与 Planner 接到"指定库/技术"需求时，先做 **availability check**：
  1. 命中 `libs_index.json` → **已预置**，直接走 §9.4.3 按需引入；
  2. 查已知公开库清单命中、但不在预置白名单 → **case a**；
  3. 查不到 / 语义与"无后台静态"矛盾（如 SSR、后端框架、服务端运行时）→ **case b**。

**9.4.5.2 case a：真实存在但未预置 → 自动/确认补齐**

- 若库在 `_download_libs.py` **支持的下载源清单**内（如 `echarts`/`dexie`/`marked`/`dompurify`）：**默认自动拉取**（`_download_libs.py` 落盘 `shared/vendor/libs/<name>/` + 登记 `libs_index.json`），成功后后续页正常按需引用；**拉取失败**（外网不通/无该版本）→ 降级转入 case b 流程。补齐期间通过 SSE `capability_notice` 回「正在预置 X 库」，**不阻塞**主流程。
- 若库**不在自动下载清单**（需人工确认源码可信度，如某 niche UI kit）：**不自动下载**，走 HITL 确认——对话澄清「X 不在预置库，是否允许我从官方源拉取？」，用户确认后再拉（避免引入不可信代码）。

**9.4.5.3 case b：不存在 / 废弃 / 形态矛盾 → 降级替代 + 强制告知**

- **不存在的库**（杜撰名、拼写错、已废弃）：明确告知「X 不是已知库/未找到」，并给**最接近的已预置替代**（例：要 `d3` 力导向 → 用 `three`/`chart.js` 近似；要 `anime.js` → 用 `gsap`）。
- **形态矛盾的技术**（React SSR / Next.js / 任何后端框架 / 服务端运行时）：明确告知「本平台只生成无后台静态站点，X 属服务端技术无法生成」，转 §9.2 降级映射给**前端等价替代**（例：要"服务端实时数据" → 前端 `setInterval` + 硬编码/假数据；要"后端搜索" → 前端本地过滤）。
- 以上降级**必回写 `degradations[]`**（requested=原要求 / substitute=替代 / limitation+upgrade_hint），并走 §9.2 强制三处告知（对话说明 + 页面 `demo-notice` + SSE `capability_notice`）。

**9.4.5.4 S8 校验对齐**

- `vendor_path_exists`(warn)：case a 自动补下载成功应**豁免**该 warn；仅当引用了"未预置且未补齐"的库才告警。
- 新增 **`unknown_lib_ref`**（**block**）：页面 `<script src="/vendor/libs/<name>">` 中 `<name>` 既不在 `libs_index.json`、也不在已知公开库清单（case b 漏网，模型硬编了虚构库）→ 强制改为已预置替代或剥离，防白屏 + 防"假库"承诺。
- 口号延续 §9.2：**"不存在不可怕，假装存在才可恶"**。

---

## 10. 认证与授权（A1 · 补齐老系统 auth 能力，文档此前缺失）

> 老系统 `auth.py`/`security.py`/`artifacts_auth.py`/`admin.py`/`user_state.py` 已有完整认证授权能力，三份设计文档此前只散提 `users` 表与 admin，未系统写**认证流程 / 权限模型 / 产物鉴权**。本章补齐。

### 10.1 认证模型
- **JWT 双令牌**：`access_token`（短时效，如 30min）+ `refresh_token`（长时效，如 7d，可轮转）；登录密码用 argon2id/bcrypt 加盐。
- **滑动过期**：`access_token` 每次活跃请求静默续期（呼应 §5 额度滑动窗口），降频重复登录；refresh 失败→强制重新登录。
- **超管**：`huzhen/huzhen189` 仅由 `reset_all.py` 创建（呼应 step1 铁律），不经由正常注册；生产 `init_db` 不建超管账号。
- **token 吊销**：`jti` 黑名单走 Redis `ai:token:blacklist`（登出 / 改密即时失效）；BYOK 明文不落 DB（信封加密，§6）。

### 10.2 授权与权限（RBAC）
- **角色两级**：`user` / `admin`；`admin` 仅人工授权（不在自助注册链路）。admin 端点走独立前缀 `/admin/*` + 独立鉴权中间件。
- **资源归属校验**：所有 Project/Conversation/Message 读改写必须校验 `user_id` 匹配（防越权读他人项目）；越权→`403`。
- **产物 URL 鉴权（artifacts_auth）**：预览产物（本地/COS 静态页）的访问 URL 带**短令牌签名**（HMAC，时效 + 防遍历），未带/过期→`401`；不依赖登录态也能安全分享。

### 10.3 安全基线
- 登录失败频控（同账号连续错 N 次→临时锁 + 告警）；密码/密钥不进日志（呼应 §4.6.2 脱敏）。
- 管理操作（改意图/删项目/调配额）写 `admin_audit_log`（step1 已设计），谁何时改了啥可追溯。
- 与阶段对齐：S0 网关做鉴权（未登录→`401`，无权限→`403`，超配额→`429`）。

## 11. 测试与回归体系（A4 · 补齐老系统测试资产，文档此前缺失）

> 老系统 `scripts/run_tests.py`(200 条)/`e2e_test_v090.py`/`multi_intent_regression.py`(24 断言)/`_diag_*`/`_verify_*` 已是成熟测试资产，文档此前仅点名 `lint_intents.py`。本章把整体测试策略写清。

### 11.1 分层
- **单元**：repo / service / tool 纯逻辑（mock LLM/provider），快、隔离。
- **集成**：Pipeline 跑通一整轮（用 `intent_lite` 本地，不依赖云端大模型），验证 S0–S9 串联 + DB/Redis/Chroma 真实交互。
- **e2e**：登录 → `auto_start` → `GET /api/chat` 读 SSE 事件（验证路由完成 / 阶段进度 / 产物落库），用**固定可复现账号**。
- **回归**：`multi_intent_regression.py`（24 断言，多意图 DAG/部分失败）、`lint_intents.py`（IntentSchema 一致性门禁）、`_diag_*`/`_verify_*`（路由/SIR 等专项）。

### 11.2 CI 门禁
- 后端：`ruff` + `mypy`（门禁）；前端：`eslint` + `prettier` + `vitest`（仅 `app`/`shared`）。
- `lint_intents.py` 在 PR 阶段强制跑（复用 step2 §2.3 一致性校验），不一致直接 red。
- 覆盖率阈值（如核心路径 ≥70%）作为合入门禁。

### 11.3 可复现测试账号（呼应跨项目记忆：测试文档必须记录账号密码）
- 固定账号 `e2e20_seedai_test` / `testpass123`（可用 `E2E_USER`/`E2E_PW` 覆盖）；凭证落盘 `_e2e_20_creds.json`，报告生成器读取并写入文档「测试账号」段，便于用户登录复查。
- 历史残留随机 `e2e20_*` 账号（密码均 `testpass123`）清理用 pymysql 查 `users WHERE username LIKE 'e2e20_%'` 删除，超管 `huzhen` 不动。

### 11.4 运行策略
- `scripts/run_tests.py`：`--quick` 30 条 / 全量 200 条；打 `GET /api/chat`（默认 qwen）。
- 报告生成器读取凭证写「测试账号」段 + 产物链接，供用户登录复查生成的项目/对话。

## 12. 第三步拍板清单（A–H 已全部确认 ✅）

| # | 决策 | 结论 |
|---|------|------|
| **A** | 断线重连主路径 + 长离线兜底 | ✅ 采用 Redis 滚动缓冲续传（120s）；**断网数天/离开数月**靠 DB 落库（`messages.content`/`content_path`/`sir_snapshot`）+ 磁盘/COS 静态产物持久态 + `paused_turns` 长存 → 回来直接重建 UI、LLM 不重跑（§1.3 机制 A'） |
| **B** | `epoch` 触发条件 | 仅「编辑 prompt」+「系统重置态」+1；**不因常态 SIR/Tasks 变化 +1**（防续传键失效）（§1.1） |
| **C** | 用户主动 Hold 持久化 | ✅ 持久化到 `ai:pause:{turn_id}` + `paused_turns` 表（DB 长存），断线/数月后均可恢复（§2.1/§2.3） |
| **D** | BYOK 覆盖平台模型 | **不覆盖**：BYOK 仅 S6 exec 三档；`intent_lite/intent_strong/text-embedding-v3` 一律平台凭证（§6.4） |
| **E** | tier 额度 | **free=5M/日**；pro/max 暂未开收费，共享 free 额度，后续 `config/quota.yaml` 覆写（§5.1/§5.4） |
| **F** | 并发/session 预算 | **统一默认**（并发 5、单 session 算力预算统一，tier 可调）（§5.1） |
| **G** | 网页能力边界策略 | ✅ **降级优先，不做拒绝清单**：用户要求的可操作功能一律 L1 降级（静态按钮 / Mock 数据 / localStorage / **IndexedDB 本地数据库**）并交付可用；**强制三处告知**（对话「能力说明」段 + 页面 `demo-notice` banner + SSE `capability_notice` 事件）+ **必附升级路径**（追加 HTML 本地数据库服务 / 接第三方 BaaS / 未来平台托管数据服务）。硬红线仅四类：平台托管运行服务端代码、真实密钥落前端、真实 PII、域名备案证书代办。校验规则相应调整：`no_backend` 由 block 降 warn，新增 block 级 `undisclosed_mock`/`secret_in_static`/`pii_in_static`（**"可以假，不可以不说"**）（§9） |
| **H** | 本地组件库接入 | ✅ 已预置 25 个库（`/vendor/libs/`，含 Tailwind/Bootstrap/Bulma/Vue/React/Alpine/jQuery/GSAP/AOS/Swiper/GLightbox/Chart.js/SweetAlert2/three 等 + 自研 `seed-premium`，见 §9.4.1）**覆盖度足以支撑绝大多数网页生成**。接入规范：① 引用一律根绝对 `/vendor/libs/<name>/<file>`，**禁止外引 CDN**（白名单硬约束）；② `libs_index.json` 自动生成 LLM 白名单注入 SYS_CODER；③ `site_deploy` 上传 COS **必须连同 `shared/vendor/libs/` 复制进桶根**，否则预演一致但生产 404（R1）；④ S8 校验 `block_external_cdn`(block) / `vendor_path_exists`(warn) / `missing_vendor_on_deploy`(block，部署阶段)。未预置需补的（dexie/echarts/marked 等）经 `_download_libs.py` 落盘即生效，无需改生成逻辑（§9.4） |

> 三步设计（step1/step2/step3）现已**全部冻结**：A–H 决策 + 网页生成能力边界（§9，含 §9.4 本地库接入规范）均已定稿；§10–§11 为**本轮用户追加批准的 A 类/B 类缺口补齐**（认证授权 / 可观测三件套 / 测试回归 / reconciler 补偿 / 管理系统优雅停止 / 队列 Redis 统计视图 / 质检 6 维（用户拍板口径）/ roles→Skill 对比），非新拍板，是对老系统已验证能力的补录。

#### 12.x 本轮 A 类 / B 类补齐清单（用户 2026-07-31 晚间批准，非部署扩容类）

- **A1 认证与授权（§10）**：JWT 双令牌 + 滑动过期、超管仅 reset_all 创建、RBAC(user/admin)、资源归属校验防越权、产物 URL 短令牌签名鉴权(artifacts_auth)、token 吊销黑名单、admin 操作审计。补齐老系统 `auth/security/artifacts_auth/admin` 能力。
- **A3 可观测性三件套（§4.6，扩写）**：tracing(`trace_id` 贯穿 S0–S9) + 结构化日志(`getLogger("app.<module>")`、脱敏、按日滚动) + metrics(harness 层 p50/p90/p99 + `/health` + 告警规则)。补齐老系统 `tracing/logging_config/analytics`。
- **A4 测试与回归体系（§11）**：单元/集成/e2e/回归四层 + CI 门禁(ruff+mypy+eslint+vitest+lint_intents) + 可复现账号(`e2e20_seedai_test`/`testpass123`，凭证落盘报告写「测试账号」段) + 运行策略。补齐老系统 `run_tests`/`e2e`/`multi_intent_regression`/`_diag`/`_verify` 资产。
- **A5 写后失败补偿 reconciler（step1 §3.7.6）**：`ai:stream:error` 失败队列 + 退避重试 + 崩溃恢复 + 与 §8 灾备衔接。补齐老系统 `reconciler.py`。
- **管理系统优雅停止服务（§4.3 B 段）**：admin 主动 drain 模式——停新请求 / 在飞任务收尾或转暂停 / 队列 flush 不丢事件 / 后台 worker 收尾 / 逆序释放资源 / `ai:system:draining` 可见。
- **队列批存 Redis 实时统计视图（step1 §3.7.7）**：`ai:stats:*` 独立 Redis 结构（积压深度/失败深度/topic 计数/最近事件流/当日计数），统计系统实时可查**不建新 MySQL 表**。
- **B6 质检维度改回 6 维（step1 §3.5）**：`qc_scores` 维度采用用户最终拍板的 **relevance/completeness/accuracy/safety/efficiency/experience + overall**（不沿用老系统 `scoring.py` 的 7 维 correctness/completeness/readability/compliance/efficiency/craft/safety）；`metrics_daily` 同步 6 列。
- **B7 旧 roles→8 Skill 详细对比（step2 §2.4）**：6 RoleAgent + 强 Schema 交接物 逐角色映射 8 Skill + Planner(DAG) + 全局统计线，证明能力零丢失、边界模型升级。

> 下一步进入**第四步：代码结构落地**（config 三件套 `models.yaml`/`router.yaml`/`quota.yaml` → `intent_catalog.json` 完整 schema → `TurnContext`/`Pipeline`/10 Stage 骨架 → 传输层 `StreamBroker` + SSE 端点 → `reset_all.py` 适配 step1 表 + step3 扩列/`user_model_keys`/`paused_turns` + §3.1.1 枚举自检 → `lint_intents.py` + 配额/计量 harness）。
