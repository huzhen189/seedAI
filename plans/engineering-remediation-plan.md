# SeedAI 工程级整改方案（新旧对比 / Before-After）

> 版本：v1.0　|　日期：2026-07-22　|　范围：后端(business + ai_service) / 前端(Vue3 SPA) / 基础设施(docker-compose·运维·可观测)
> 目标：把「能跑的 demo」升级为「能闲聊 + 生成/修改 HTML·CSS·JS 的智能站点，**有工程级运营与错误处理、抗高并发、可手动扩容**」的生产系统。
> 状态：本方案为**审查结论 + 整改设计**，本会话仅产出方案，**未改动任何代码**。逐项实施需用户确认后另开任务。

---

## 0. 项目定位与「预期设计 vs 当前实现」偏差总览

### 0.1 定位（用户原话）
> 一个能闲聊，能按用户输入生成和修改 HTML/CSS/JS 的智能网站；有工程级别的运营和错误处理；能抗住高并发；能手动扩容。

### 0.2 架构基线（来自 `docs/01-项目总览.md`）
| 层 | 进程 | 职责 | 暴露 |
|---|---|---|---|
| 前端 | Vue3 + Vite SPA | 聊天 UI / 预览 / 管理后台 | `:7100`（本地）|
| 业务 | FastAPI `business` | 鉴权 / SSE 代理 / MySQL / Redis / 统计 / 管理 | `:7101`（唯一对外）|
| AI 核心 | FastAPI `ai_service` | 意图管道 / Skill Worker / 生成 HTML·CSS·JS | `:7102`（仅内网）|
| 依赖 | MySQL(云) / Redis(库3) / Chroma(:8000) | 持久化 / 缓存队列 / 向量 | 内网 |

SSE 链路：前端 `EventSource /api/chat` → business 代理 → ai_service `/generate` → Redis `queue:generate` + Stream `gen:stream:<trace_id>` → Worker 消费 → 逐帧透传。

### 0.3 「设计承诺 vs 代码现实」偏差
设计文档画得很漂亮（无状态可水平扩展、手动扩容 `--scale`、优雅停机 lifespan、liveness/readiness 分离、跨用户隔离、写失败兜底对账）。但**多份实现与文档承诺不符**，核心偏差集中在三处：

1. **并发模型是伪异步**：async 函数里直接调同步 `chat.invoke()`，事件循环被单个生成任务独占 → 单进程并发天花板 ≈ 1，「抗高并发」名存实亡。
2. **隔离/鉴权有洞**：`/api/cancel` 无鉴权、跨用户会话读未校验 owner、预览 iframe 沙箱逃逸 → 「工程级安全」未达标。
3. **运维契约未落地**：`/health` 不查依赖、`reconciler` 是空壳丢数据、`docker-compose` 宿主端口硬绑定 → 「手动扩容 / 错误兜底」跑不通。

### 0.4 风险分级统计
| 级别 | 后端 | 前端 | 基础设施 | 合计 |
|---|---|---|---|---|
| **P0（上线前必改）** | 2 | 1 | 2 | **5** |
| **P1（重要，尽快）** | 6 | 4 | 3 | **13** |
| **P2（优化，排期）** | 5 | 3 | 2 | **10** |

---

## 1. P0 整改（上线前必改）

> 任一条不修，都是「生产事故预备役」：要么并发直接被打挂，要么用户数据/登录态被偷，要么扩容命令执行即报错。

### P0-1 〔后端·并发天花板〕同步 LLM/Embedding 调用阻塞事件循环

**现状（Before）**
- `skills/builder_agent.py:67` / `explain.py:94` / `generate_site.py:94` / `requirement_agent.py:92` / `write_code.py:37` 在 `async def` 内调用 `resp = chat.invoke([...])`（langchain 同步 LLM）。
- `knowledge/chroma.py` 的 embedding 函数（`_ef()` / `get_or_create_collection`）也是同步 Chroma 客户端调用，在协程里跑。
- 后果：`chat.invoke` 是 CPU/网络同步阻塞，调用期间**整个事件循环被这一个生成任务独占**。单 worker 进程实际并发能力 ≈ 1；高并发时请求排队、SSE 首帧延迟飙升、Worker 堆积、OOM 风险陡增。「抗高并发」在当前代码下不可能成立。

**整改（After）**
- LLM 调用统一卸载到线程池：`resp = await asyncio.to_thread(chat.invoke, [{"role":"system",...}, *msgs])`；或直接换 `await chat.ainvoke(...)`（若 LangChain 版本支持）。
- Chroma 同步调用（建库/检索/embedding）同样 `await asyncio.to_thread(...)` 卸载，或切换 `chromadb` 的异步客户端。
- 在 `core/queue.py` Worker 的消费协程里，单次 `run_skill` 不应再有任何同步长阻塞；耗时 IO 全部 `to_thread`。
- 附带：Worker 池大小与 `asyncio` 语义对齐（多 Worker 进程 + 进程内非阻塞协程）。

**预期收益**
- 单 worker 可并发处理多个生成任务，事件循环不再被独占。
- 资源利用率↑、P95 首帧延迟↓、OOM 概率↓。
- 为 P0-5「手动扩容」提供真实的并发底座。

**关联目标**：高并发、手动扩容。

---

### P0-2 〔前端·安全〕预览 iframe 沙箱逃逸（XSS 盗登录态）

**现状（Before）**
- `RightPanel.vue:195` 与 `PreviewPane.vue:18`：`sandbox="allow-scripts allow-same-origin allow-forms"`。
- `allow-scripts` 与 `allow-same-origin` **同时存在 = 沙箱完全失效**（W3C 规范：两者同现时浏览器会忽略沙箱限制）。生成的 HTML/JS 因此可以：
  - 访问父页同源的 `document.cookie` / `localStorage`（含 JWT）；
  - 调用父页 DOM、发起同源请求（带着用户 Cookie）；
  - 即使用户只「预览」了一段不可信的 AI 生成代码，登录态也可能被窃取。

**整改（After）**
- 去掉 `allow-same-origin`，仅保留 `allow-scripts`（如确需表单交互则 `allow-scripts allow-forms`）。
- 预览内容通过 `srcdoc` 注入；父页与 iframe 通信改用 `postMessage`（而非依赖同源 DOM）。
- 后端同步加固：登录 Cookie 设为 `HttpOnly` + `SameSite=Strict`（已在 `docs/01` 决策，本次补齐落地），即使前端沙箱偶发失守，JS 也读不到 token。

**预期收益**
- 阻断「生成内容 → 窃取登录态 → XSS 横向移动」完整攻击链。
- 满足「工程级错误处理 / 安全」基线。

**关联目标**：工程级安全。

---

### P0-3 〔后端·鉴权〕`/api/cancel` 未鉴权 + 无归属校验

**现状（Before）**
- `proxy.py:959-971`：`@router.post("/cancel")` 的 `cancel()` **无任何 `Depends(get_current_user)`**。
- 任意匿名请求带 `{"trace_id": "..."}` 即可转发到 AI 的 `/cancel` 中断生成。
- 攻击者枚举 `trace_id`（或抓包）即可中断**任意用户**的对话生成（拒绝服务）。这是已登记技术债 **M3**。

**整改（After）**
- `cancel()` 加 `current_user: User = Depends(get_current_user)`。
- 由 `trace_id` 解析 owner（`Message.trace_id` ↔ `user_id` 已落库），校验归属；越权返回 `403 Forbidden`。
- AI 侧 `/cancel` 仍由 business 转发，但 business 已先完成鉴权 + 归属校验。

**预期收益**
- 关闭越权取消链路，消除针对生成服务的 DoS 面。

**关联目标**：工程级安全、M3 技术债清偿。

---

### P0-4 〔后端·数据可靠性〕reconciler 空壳丢数据

**现状（Before）**
- `reconciler.py`：`_retry_one(payload)` **永远 `return True`**（注释明写「M0 占位：成功返回 True」）。
- 从 Redis `queue:error` 取出失败写操作后，直接当成功丢弃，**无真实回写 MySQL、无 DLQ（死信队列）、无告警**。
- 写 MySQL 失败（网络抖动 / 主从延迟）时，业务侧以为落库成功，实际**消息 / artifact 静默丢失**。

**整改（After）**
- 实现真实回写：按 `payload["kind"]`（如 `upsert_message` / `append_artifact` / `upsert_user`）路由到对应 Repository 的 `upsert`。
- 回写失败入 DLQ（`queue:error:dlq`）并触发告警（接入 `analytics` 失败计数 + 管理后台「系统分析」可见）。
- reconciler 进程崩溃重启保持幂等（按 payload 唯一键 upsert，不重复写）。

**预期收益**
- 写失败可恢复，核心数据（对话 / 产物）不丢。
- 兑现「工程级错误处理」承诺，对账器从演示骨架变为真正兜底。

**关联目标**：工程级错误处理、技术债 H1（proxy 上游错误落悬空记录）协同修复。

---

### P0-5 〔基础设施·扩容〕docker-compose 宿主端口硬绑定 + `/health` 不查依赖

**现状（Before）**
- `docker-compose.yml:70-71`：`ports: - "7101:7101"` 把宿主机 7101 硬编码绑定到**单个** business 容器。
  - 执行 `docker compose up --scale business=3` → 报「端口已被占用」，**手动扩容直接失败**。
- `main.py:65-66`：`/health` 仅 `return {"status": "ok"}`，**不查 MySQL / Redis / 队列可达性**。
  - 即使 MySQL 挂了，编排器 / 负载均衡仍判定实例健康、继续转发流量 → 雪崩。

**整改（After）**
- 去宿主机端口硬绑定：
  - business / ai_service 改用 `expose: - "7101"`（仅集群内可见），前端与网关走内部网络；
  - 若本地需直连，仅给首个实例留 `ports`，扩容实例不绑宿主端口（配合反向代理 / 服务发现）。
- 探活拆分：
  - `GET /healthz`（liveness）：仅自活（进程在、能响应）→ 编排器决定是否重启。
  - `GET /ready`（readiness）：`MySQL ping` + `Redis ping` + `队列可达` 全绿才返回 200，否则 503 → 编排器摘流（不杀进程，依赖恢复后自动回归）。
- （可选）`ai_service` 同样加 `/ready` 查 Redis + 模型可用性。

**预期收益**
- `docker compose up --scale business=N` 真正可横向扩容，打通「手动扩容」路径。
- 真实健康探活，依赖故障时不误转发，避免雪崩。

**关联目标**：手动扩容、运维可观测。

---

## 2. P1 整改（重要，尽快）

### P1-1 〔后端〕SSE 生成无总时长上限（技术债 M2）
- **Before**：`/generate` 长链路无总时限，异常卡住的生成永远占用 Worker + 连接。
- **After**：Worker 层加 `max_generation_seconds`（如 300s）硬上限；超时强制发 `done`+`abort` 事件并释放资源；前端 `onAlternatives`/生成态收到 `abort` 展示「已超时，请重试」。

### P1-2 〔后端〕DB 会话长占
- **Before**：长生成协程内长时间持有异步 DB session，连接池被占满时其他请求拿不到连接。
- **After**：仅在「真正读写那一下」开 session，写完立即 `close()`/`async with` 归还；长链路中间不持有 session。

### P1-3 〔后端〕跨用户会话越权读
- **Before**：`proxy.py` 按 `conversation_id` 取消息 / 产物时未校验 owner，A 用户可传 B 的 conversation_id 读到 B 的内容。
- **After**：所有 `conversation` / `project` / `artifact` 读写路径统一加 `owner_id == current_user.id` 校验；越权 403。

### P1-4 〔后端〕register 并发 500（技术债 M1）
- **Before**：并发注册同用户名 → 唯一约束冲突抛 500。
- **After**：捕获 `IntegrityError` 返回 `409 Conflict`（用户名已存在）；加事务 + 幂等。

### P1-5 〔后端〕队列无 ack 丢任务
- **Before**：Worker 从 `queue:generate` 取任务后若崩溃，任务丢失（无 ack / 无可见性超时）。
- **After**：Redis Stream / List 消费加 `ack` + 可见性超时（`BLOCK` + `XCLAIM` 或 `RPOPLPUSH` 模式）；Worker 崩溃任务可重投。

### P1-6 〔前端〕SSE 无心跳 / 超时 / 抖动丢半成品
- **Before**：`EventSource` 无心跳保活，弱网断连后前端不知道、半成品消息滞留。
- **After**：后端每 ~15s 发 `: heartbeat` 注释帧；前端 `es` 无消息超时有 `onerror` 自动按 `trace_id` 续传（复用 P0/v0.5 断点恢复）。

### P1-7 〔前端〕切项目 / 路由未关 SSE 泄漏
- **Before**：切会话 / 切路由时旧 `EventSource` 未 `close()`，连接泄漏累积。
- **After**：`onUnmounted` + 路由守卫统一 `abortController.abort()` 关闭在途 SSE。

### P1-8 〔前端〕`doSend` 无 try/catch 致 conversationId="null"
- **Before**：`doSend` 异常未捕获 → `conversationId` 被写成字符串 `"null"`，后续请求全错。
- **After**：包 `try/catch`，失败重试 / 提示，绝不写入脏 ID。

### P1-9 〔前端〕所有 resend 未关旧流
- **Before**：`resendWithSkill` / 重发时旧 `EventSource` 未关闭，双流叠加重复渲染。
- **After**：重发前先 `abort()` 旧流，保证单流。

### P1-10 〔基础设施〕business 侧 TraceIdFilter 未贯通
- **Before**：仅 ai_service 落了 `TraceIdFilter`，business 日志无 `[trace=..]`，全链路排障断点。
- **After**：business 接入 `contextvars` + `TraceIdFilter`，在 `/api/chat` 入口 `set_trace`，与 ai_service 对齐，端到端可追。

### P1-11 〔基础设施〕密钥硬编码 / 未外置
- **Before**：部分密钥 / URL 字面量散落代码（如 embedding key、超管账号硬编码）。
- **After**：统一走 `.env` + 配置中心 / Secret Manager；代码内零字面密钥；超管改为环境变量 `SEED_SUPER_ADMIN` 注入（已在 db.py，补齐其余）。

### P1-12 〔基础设施〕MemoryBackend 多副本分片
- **Before**：`pending_options` 等内存状态随多 Worker 分片，A 实例登记的选项 B 实例读不到。
- **After**：多 worker 共享状态统一走 Redis（如 `pending_options` 迁 Redis）；或明确单副本约束并文档化。

### P1-13 〔基础设施〕无 Prometheus 指标
- **Before**：无 `/metrics`，扩容 / 排障靠肉眼看日志。
- **After**：暴露 `generation_total / generation_duration_p95 / queue_depth / error_rate / active_connections` 等，管理后台「系统分析」复用同一数据源。

---

## 3. P2 整改（优化，排期）

| 编号 | 位置 | 现状 | 整改后 |
|---|---|---|---|
| P2-1 | 后端 | `@app.on_event` 弃用（D1） | 改 `lifespan` 上下文管理，优雅启停 |
| P2-2 | 后端 | 每请求 `httpx.AsyncClient()` 新建 | 进程级共享 client（连接池），`lifespan` 内创建/关闭 |
| P2-3 | 后端 | `KEYS *` 全量扫描 Redis | 改 `SCAN` 游标，避免阻塞主线程 |
| P2-4 | 前端 | WebLLM 静默下载 ~600MB | 默认关闭 / 用户显式开启 + 下载进度提示 |
| P2-5 | 前端 | alternatives 完成后未清 | 生成结束 `clearAlternatives()`，避免旧候选滞留 |
| P2-6 | 前端 | 本地闲聊不持久化 | 按用户选择持久化或明示「仅本次会话」 |
| P2-7 | 后端 | 鉴权三分叉（security / proxy._resolve_user / …） | 统一到单一 `get_current_user` 依赖（清偿 H2） |
| P2-8 | 后端 | `create_task` 未 await / 异常 handler 死循环 | 清理未等待任务与死循环分支 |
| P2-9 | 前端 | dev 代理无 SSE flush | vite 代理显式 `flushInterval` / `ws:false` 已做，补 SSE 不分块缓冲 |
| P2-10 | 基础设施 | 「用全名」提示不匹配 alternatives | 文案与后端 `parse_selection` 对齐，避免误导 |

---

## 4. 实施顺序（建议）

```
阶段 A（P0，阻断上线）──── 并行推进
  P0-1 后端卸载阻塞调用        → 并发底座
  P0-2 前端沙箱修正            → 安全
  P0-3 /api/cancel 鉴权       → 安全
  P0-4 reconciler 真实回写     → 数据可靠
  P0-5 compose 去端口硬绑+探活拆分 → 扩容通路
        ↓ 全部完成并自验
阶段 B（P1，健壮性）──── 按依赖排序
  P1-10/P1-11/P1-13 可观测先行（先能看见问题）
  P1-1/P1-2/P1-5 后端资源与队列
  P1-3/P1-4 隔离与并发注册
  P1-6~P1-9 前端 SSE 生命周期
  P1-12 共享状态迁 Redis
        ↓
阶段 C（P2，打磨）──── 排期清理
  P2-1~P2-10 技术债清偿
```

> 原则：**先打通「能并发 + 能扩容 + 不丢数据 + 不被打穿」，再做体验打磨**。P0 任一项未过，不允许上生产。

---

## 5. 手动扩容操作步骤草案（依赖 P0-5）

```bash
# 1) 确保 docker-compose.yml 已去 business/ai_service 宿主端口硬绑定（仅 expose）
# 2) 启动基础依赖
docker compose up -d mysql redis chroma

# 3) 扩容业务层（无端口冲突）
docker compose up -d --scale business=3 --scale ai-service=3

# 4) 前置反向代理 / 服务发现指向 business 集群（内部网络，不再绑宿主 7101）
# 5) 观察 readiness：逐个实例 GET /ready 应 200；MySQL/Redis 抖动时自动摘流
# 6) 缩容：docker compose up -d --scale business=1（优雅 drain 由 lifespan 保证）
```

> 前置条件：P0-1（非阻塞 Worker）+ P0-5（无端口硬绑 + readiness）+ P1-12（状态走 Redis）三者齐备，扩容才有意义。

---

## 6. 联调验证清单

**P0 验证**
- [ ] 压测：`locust` / `wrk` 并发 50 路 `/api/chat`，单 worker 首帧延迟不随并发线性恶化（证明事件循环不再被独占）。
- [ ] 安全：预览一段含 `document.cookie` 窃取脚本的 AI 生成 HTML，父页 Cookie 不被读取（沙箱生效）。
- [ ] 鉴权：匿名 `POST /api/cancel` → 401；A 用户取消 B 的 trace → 403。
- [ ] 数据：人为让 MySQL 写入失败，reconciler 日志出现真实回写 + DLQ，恢复后数据不丢。
- [ ] 扩容：`--scale business=3` 成功；逐个 `GET /ready` 探活正常；kill 一个 MySQL 连接后该实例 readiness=503 且流量被摘。

**P1 验证**
- [ ] 生成 > 上限时长自动 `abort`；弱网断连前端按 trace_id 续传无重复。
- [ ] 切会话 / 路由无遗留 `EventSource`（DevTools Network 连接数稳定）。
- [ ] 并发注册同用户名返回 409 而非 500。
- [ ] business 日志出现 `[trace=..]`，与 ai_service 串联。

**P2 验证**
- [ ] 启动/关闭日志来自 `lifespan`；无 `@app.on_event` 弃用警告。
- [ ] Redis `SCAN` 替代 `KEYS`；WebLLM 默认不下载。

---

## 7. 备注

- 本方案基于三方只读审查（后端 / 前端 / 基础设施）+ `docs/01-04` 设计基线汇总；所有 P0 的 `file:line` 已逐一 `grep`/`Read` 复核属实。
- **本会话未改动任何业务代码**（Phase 2 仅审查 + 出方案）。逐项实施需用户确认后另开任务，按「改代码即同步 git commit + 更新 docs/01~04」长期约定执行。
- 关联历史：Phase 1 的「ai_service 流程改进（决策自治 + 选择解析 + 端到端日志）」已落地并提交 `162f029`，待用户手动重启 7102 联调验证「回 B 被正确识别」与 alternatives 提示条。

---

## 8. 新功能：基于 Git 的站点版本控制 + COS（新功能，不计入 P0/P1/P2）

> 标注：这是**新增能力**，不是缺陷修复。按用户 2026-07-22 确认的 5 项决策落地；详细流程图见配套 `ai_service` 对话思考/执行流程图。

### 8.1 现状 / 痛点（Before）
- 生成的 HTML/CSS/JS 只是一次性产物：存 `Artifact` 表 + COS 直链，**无版本历史**。
- 用户改坏了只能重生成，无法回滚到「上上个版本」，也看不到每次改动 diff。
- 大资源（图片/字体）与源码混在一起，复现 / 迁移 / 协作都麻烦。

### 8.2 整改 / 方案（After）
**真 Git（非 DB 快照模拟）**，每个 `project` 一个仓库，三层职责分离：

| 层 | 职责 | 落地 |
|---|---|---|
| 磁盘 working tree | agent 直接读写、可预览/构建 | `data/sites/<project_id>/` |
| DB `site_version` 表 | 版本元数据（commit_hash/message/trace_id/user_id/diff_stats），前端直查不碰 CLI | 新增表 |
| COS | ① 容灾备份 ② LFS 大资源 ③ 发布态 | `seedai-git/` · `seedai-assets/` · `seedai-sites/` |

**5 项确认决策**：
1. 提交粒度：**每轮 agent turn 自动 commit**（历史最细，message 带 `trace_id`）。
2. 分支：**允许实验分支**（`main` 发布态 + `tag` 里程碑 + `exp/<name>` 实验分支）。
3. COS 预览：**发布态直接走 COS 静态托管**（顺带治 P0-2 沙箱逃逸——预览读 COS 直链而非同源 srcdoc）。
4. LFS：**开启**（大资源走 Git LFS，object 落 COS）。
5. 本地保留：**全按需从 COS 恢复**（本地默认不存 working tree，访问时 `git clone <bundle>` 还原）。

**框架选型**：**subprocess 调系统 `git` CLI + `asyncio.to_thread` 包裹**（非 GitPython 直接同步调用）。理由：所有 git 库本质同步，必须 `to_thread` 才不阻塞事件循环（兑现 P0-1）；git CLI 最稳、特性全、零 Python 依赖、易调试；docker 仅 `apk/apt add git git-lfs`。LFS 接 COS 用**自定义 LFS transfer agent**（复用 `tools/cos_*` 客户端）把 object 落到 `seedai-assets/`。

### 8.3 实现落点
- `ai_service/app/core/git_site.py`：`ensure_repo`（本地缺失→拉 bundle→clone）、`commit`（add -A + commit，to_thread）、`checkout_restore`、`bundle_to_cos`、`diff`。
- Worker 在「生成成功」与「回滚」两处接该模块 + per-project Redis 锁（`git:lock:<project_id>`）。
- business 新增 `GET /api/projects/<id>/versions`、`POST .../rollback`（带 P0-3 鉴权 + owner 校验）。
- 前端新增「版本历史」面板（项目侧栏 / AdminView）：时间线 + 选两版本 diff（monaco-diff / diff2html）+ 一键回滚。
- DB 新增 `site_version` 表：`project_id, commit_hash, parent_hash, message, trace_id, author_user_id, files_changed, stats_json, cos_bundle_key, created_at`。

### 8.4 回滚流程
用户选版本 → ai_service 在锁内 `git checkout <hash> -- .` 还原 working tree → 重发 COS（更新预览/分享）→ 可选补 `revert to vN` commit 保持历史线性。

### 8.5 崩溃安全 / 运维
- 提交失败 = 无新 commit，文件仍在 working tree（git 原子性保证历史不 corrupt）；bundle 上传 best-effort + 重试（复用 P0-4 reconciler/DLQ）。
- 本地磁盘仅按需还原，仓库增长由 git 增量压缩 + COS bundle 兜底，无本地膨胀风险。

### 8.6 收益 / 关联
- **收益**：生成站点从「一次性产物」升级为「可工程化迭代的资产」——精确回滚、diff 审阅、实验分支、跨区容灾。
- **关联**：P0-1（to_thread 不阻塞事件循环）、P0-2（COS 托管预览治沙箱逃逸）、P0-3（rollback 接口鉴权 + owner 校验）、P0-4（bundle 上传复用 reconciler/DLQ）。
