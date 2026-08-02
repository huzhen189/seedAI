# 向量库关键路径实测报告（2026-07-28）

## 0. 测试方式与结论速览

用户要求验证向量库 4 条关键路径是否「走完完整流程 / 是否成功提取 / 是否产生正向作用」。
实测方式：

1. **起服务**：单进程 v2.0 已启动（`uvicorn app.main:app --port 7101`，seedai-biz venv），
   启动日志确认 `embedding 提供方=Qwen text-embedding-v3`（1024 维，**非**本地兜底）、
   `已确保 9 个集合存在`、`意图向量索引检测就绪(集合=intents)`。
2. **直接函数验证**：`scripts/test_vector_pipeline.py` 调用与运行中服务**完全相同**的模块
   （同一 Chroma 客户端、同一 embedding），模拟多条新语句。
3. **真实链路验证**：`scripts/_live_chat_probe.py` 走通一次真实 HTTP/SSE
   （注册→建项目→建会话→`/api/chat`），从服务日志 `test_service.log` 抓取实时标记。

> 关键结论先行：**3.1 与 3.2 已接通并产生正向作用；3.3 与 3.4 函数正确，但运行时根本没被调用（写-only 死路）。**

| 路径 | 函数可用 | 运行时接通 | 正向作用 |
|------|---------|-----------|---------|
| 3.1 意图召回 `retrieve_intents` | ✅ | ✅ 每请求必走 | ✅ |
| 3.2 建站 RAG `build_rag_context` | ✅ | ✅ Planner 前调用 | ⚠️ memory 有效 / components 空失效 |
| 3.3 上下文连贯 `find_relevant_messages` | ✅ | ❌ **全代码无调用点** | ❌ 死路 |
| 3.4 个性化/复用 `retrieve_user_preferences` 等 | ✅ | ❌ **全代码无调用点** | ❌ 只写不读 |

---

## 1. 集合现状（实测文档数）

| 集合 | 文档数 | 说明 |
|------|-------|------|
| `intents` | 82 | 已 seed，混合级联第②步数据源 |
| `memory` | 7 | 历史建站产物（含「深蓝·摄影作品集」） |
| `conversation_context` | 21 | 真实会话消息（worker [2/6] 写入） |
| `components` | **0** | ⚠️ 空，未 seed |
| `user_preferences` / `project_memory` / `project_code` / `error_patterns` / `cache_gen` | **0** | 空 |

---

## 2. 3.1 意图识别入口 `retrieve_intents` —— ✅ 接通且起正向作用

**调用链**：`classify_v3 → _classify_segment → retrieve_intents`（intent/vector_store.py:358）。
每一条用户消息进入系统第一道门，决定路由 / 是否多意图拆分。

**模拟新语句实测（均为 Chroma 语义召回，非离线 bigram 降级）**：

| 语句 | top1 意图 | 相似度 |
|------|----------|-------|
| 帮我做一个个人摄影作品集网站，深色风格 | build_site | 0.352 |
| 生成一个企业官网，科技行业，蓝紫色调 | build_site | 0.447 |
| 深圳今天天气怎么样 | chat_casual | 0.422 |
| 你好，你是谁 | chat_casual | 0.675 |

**真实服务日志印证**：
```
[向量-意图召回] ✅ Chroma 语义召回 query=你好，你是谁 top1=chat_casual score=0.675
[级联][3b5cd…] 召回向量 top_k=5 条: [('chat_casual', 0.675), ('build_game', 0.08), …]
```
SSE 实时返回 `decision=route, skill=agent_chat` → 路由正确。

**判定**：完整链路走完，路由正确 → **正向作用 ✅**。

---

## 3. 3.2 建站 Planner RAG `build_rag_context` —— ✅ 接通，但 components 空导致组件库增强失效

**调用链**：`agent_generate_site.py:663` / `agent_build.py:584`（`ThreadPoolExecutor.submit(build_rag_context, first_user_msg)`），
在 Planner 拼系统提示前检索 `components` + `memory`。

**实测**：查询「个人摄影作品集网站 深色风格 全屏大图 瀑布流布局」
- `memory` 命中 5 条（注入 4000 字，top=「深蓝·摄影作品集」含完整 HTML）→ **正向作用 ✅**（复用历史产物）
- `components` 未命中（空集合）→ **组件库参考增强失效 ⚠️**

**两点隐患**：
1. `components` 集合当前 **0 文档** → 需运行 `scripts/seed_rag_components.py` 填充才能激活「组件库参考」。
2. 两处调用点都只传 `first_user_msg`、**没传 `project_id`**（函数支持但调用方未用），
   因此 `memory` 检索是**跨项目全量**，未按项目隔离。

**判定**：链路接通；正向作用**部分成立**（memory 有效、components 失效）。

---

## 4. 3.3 对话上下文连贯 `find_relevant_messages` —— ⚠️ 函数可用，运行时未接通（死路）

**函数实测正确**：
- 索引 3 条消息（conv=999001）后查询「摄影作品集网站配色」→ 命中 msg 990001（sim 0.669 ≥ 阈值 0.55）
- 查询「明天股票会涨吗」→ 0 命中（阈值过滤正确，日志 `未命中(全部低于阈值 0.55)`）

**但全代码 grep 确认 `find_relevant_messages(` 无任何调用点。**
`conversation_context` 集合只被 **写入**（worker [2/6] `index_message`），从未被**读取**。

**真实服务印证**（本次实时请求 conv=21）：
```
[Worker] [2/6] Chroma向量索引 conv=21 msgs=1 开始...
[向量] 索引消息 msg=21001 conv=21 role=user content=你好，你是谁
[Worker] [2/6] Chroma索引完成 成功=1/1
```
写侧确实跑通；但后续 [3/6] 上下文检测只用 DB 历史消息 + summary + 前端 ctx_hint，**从不查询 `conversation_context`**。

**判定**：写入链路走完、召回链路没走 → **多轮对话向量连贯当前未生效（死路）**。

---

## 5. 3.4 精细化个性化/复用 —— ⚠️ 写读闭环成立，但 retrieve 未接通（只写不读）

**写入侧（实测会执行）**：
- `worker._distill_memories` 写 `user_preferences` / `project_memory`
- `worker._index_project_code` 写 `project_code`
- `seed_error_patterns` / `upsert_error_pattern` 写 `error_patterns`

**读取侧（实测函数正确，但运行时无调用）**：
```
[向量-个性化] 检索 user_preferences user=999001 query=配色风格偏好 命中=1 条
[向量-个性化] 检索 project_memory proj=999001 query=首页布局 命中=1 条
[向量-错误模式] 检索 error_patterns query=flex 溢出 命中=1 条
```
写→读闭环本身成立。**然而 `retrieve_user_preferences` / `retrieve_project_memory` /
`retrieve_project_code` / `retrieve_error_patterns` 全代码无任何调用点。**
`build_rag_context` 只查 `components` + `memory`，不查上述 4 个集合。

**判定**：个性化/复用数据在持续沉淀，但**从未反馈进 Planner 提示** → 当前**不产生正向作用（写-only 死路）**。

---

## 6. 若要让 3.3 / 3.4 真正起正向作用（建议接线点）

1. **3.3**：在 `queue.py` worker [3/6] 上下文检测处调用 `find_relevant_messages`，
   将命中的历史消息 id 取出内容注入上下文（跨轮连贯）。
2. **3.4**：在 `build_rag_context`（或 Planner 提示组装处）增加
   `retrieve_user_preferences` / `retrieve_project_memory` / `retrieve_error_patterns`
   的调用，把个性化偏好、项目记忆、错误模式注入提示。
3. **3.2**：运行 `scripts/seed_rag_components.py` 填充 `components`；并让两处调用点传入 `project_id` 实现隔离。

> 以上为接线建议，本次仅做验证，未改动运行时 wiring（遵循「先验证、后动手」）。

---

## 7. 复现命令

```bash
# 启动服务（seedai-biz venv）
cd backend
../.workbuddy/binaries/python/envs/seedai-biz/Scripts/python.exe -m uvicorn app.main:app --port 7101

# 离线函数验证（直连 live Chroma）
python scripts/test_vector_pipeline.py

# 实时链路验证（真实 HTTP/SSE，已跑通）
python scripts/_live_chat_probe.py
```

服务当前仍在 `127.0.0.1:7101` 运行，可直接用浏览器/前端访问。
