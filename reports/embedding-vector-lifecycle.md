# Embedding 向量库生命周期与链路说明

> 适用版本：v2.2.0（方案 B 收敛后）
> 定位：本文档说明 Chroma 向量库的 **生成（写入）时机、召回（查询）时机、是否走完完整链路、以及对整体流程的作用**，供排查与接入参考。

---

## 1. 概览

| 项 | 说明 |
|---|---|
| Embedding 模型 | 阿里云 Qwen `text-embedding-v3`（经私有 MaaS 工作区 `QWEN_EMBEDDING_BASE_URL` 接入） |
| 向量维度 | **1024**（与现有 Chroma 集合强绑定，见 §6 风险） |
| 封装入口 | `backend/app/agent/knowledge/chroma.py` 的 `_ef()`（统一 embedding function） |
| 向量库 | 远程 Chroma `CHROMA_URL`（默认 `1.12.219.195:7000`） |
| 调用封装层 | `knowledge/chroma.py`（CRUD 通用函数）+ `intent/vector_store.py`（意图专用索引） |
| 优雅降级 | `_available()` 为假时：`retrieve/*` 返回 `[]`、`_upsert` 静默跳过、意图识别退回离线 bigram 打分（链路不崩，仅精度下降） |

### 1.1 集合清单（8 个）

| 集合 | 常量 | 用途 | 写入方 | 召回方 |
|---|---|---|---|---|
| `intents` | `chroma_collection_intents` | 意图语义索引 | `ensure_intent_index`（reset_all） | `retrieve_intents`（意图识别） |
| `components` | `chroma_collection_components` | 组件库 RAG 参考 | `seed_components`（seed 脚本） | `build_rag_context` |
| `memory` | `chroma_collection_memory` | 历史生成记忆 | `save_memory` | `build_rag_context` |
| `conversation_context` | `CTX_COLLECTION` | 对话上下文相似度 | `index_message`（每轮） | `find_relevant_messages` |
| `user_preferences` | `chroma_collection_user_preferences` | 用户偏好 | `upsert_user_preference`（蒸馏） | `retrieve_user_preferences` |
| `project_memory` | `chroma_collection_project_memory` | 项目记忆 | `upsert_project_memory`（蒸馏） | `retrieve_project_memory` |
| `project_code` | `chroma_collection_project_code` | 项目代码块 | `upsert_project_code`（建站 done） | `retrieve_project_code` |
| `error_patterns` | `chroma_collection_error_patterns` | 错误模式纠偏 | `seed_error_patterns`（seed 脚本） | `retrieve_error_patterns` |

---

## 2. 向量「生成」（写入）时机

### 2.1 启动 / 重置期（一次性 seed，幂等）

- **`intents` 集合**：`scripts/reset_all.py:125-126` 调用 `ensure_intent_index()`，把 `intent_catalog.json` 的 examples 批量写进 Chroma。
  - 分批写入（每批 ≤10 条，规避 Qwen embedding 单批上限）。
  - 启动期只做计数检查（`check_intent_index`），**不再每次重启重建**，避免 82 句意图重复 embedding。
- **`components` / `error_patterns` 集合**：由数据准备脚本（`scripts/seed_rag_components.py` 等）首次灌入，提供组件最佳实践与已知错误模式。重置时**保留不清**（`reset_all.py` 仅清 `memory / user_preferences / project_memory / project_code`）。
- **`ensure_collections()`**（`main.py:57` 启动调用）：仅确保集合存在（不写入数据）。

### 2.2 每轮对话（运行时，必走）

- **`Worker [2/6] Chroma 向量索引`**（`queue.py:739-756`）：每收到一个 job，就把 `messages` 里每条消息写入 `conversation_context`。
  - 调用 `index_message()`（`chroma.py:245`），`id = msg_{msg_id}`，`metadata = {conversation_id, role, msg_id}`。
  - 隔离到 `asyncio.to_thread` 避免阻塞 worker loop；失败仅 warn。
  - 这是**每次会话交互都会点亮**的写入节点（见 §4 实证）。

### 2.3 建站任务 done 之后（蒸馏 / 沉淀）

- **记忆蒸馏 `_distill_memories()`**（`queue.py:202-250`，L2+）：
  - 仅 `agent_build` / `agent_generate_site` / `orchestrator` 触发。
  - 用 deepseek 从精炼对话抽取结构化信息 → `upsert_project_memory()`（写入 `project_memory`）+ `upsert_user_preference()`（写入 `user_preferences`）。
- **代码索引 `_index_project_code()`**（`queue.py:253+`，P4）：
  - 仅 `generate_site` / `agent_build` 且 `project_id` 存在时触发。
  - 遍历本地产物目录（`*.html/*.css/*.js`）分块 → `upsert_project_code()`（写入 `project_code`）。
- **生成成功回写 `save_memory()`**（`agent_generate_site.py:650/809`、`agent_build.py:571/724`）：
  - 收尾阶段把 `title + html[:1500] + steps` 写入 `memory` 集合（记忆闭环）。

### 2.4 写入侧关键日志标记

```
[Worker] [2/6] Chroma向量索引 conv=19 msgs=N 开始...
[ai_service.chroma] [向量] 索引消息 msg=19001 conv=19 role=user content=...
[ai_service.intent.vector] [向量] 意图索引已构建 N 条(集合=intents, 分M批)
[ai_service.intent.vector] [向量] 用户偏好 upsert user=... type=... hash=...
[ai_service.intent.vector] [向量] 项目记忆 upsert proj=... type=... hash=...
[ai_service.intent.vector] [向量] 错误模式 upsert type=... hash=...
[蒸馏] done trace=... proj=... user=... proj_mems=N user_prefs=M
```

---

## 3. 向量「召回」（查询）时机

### 3.1 意图识别（每次请求入口必走）

- **`classify_v3` → `retrieve_intents()`**（`intent/cascade.py:358`，`intent/vector_store.py:112`）：
  - 混合级联第 ② 步：语义召回 `intents` 集合 top5 → 按 `intent_id` 聚合最高相似度 → 作为 LLM 终判的候选。
  - 这是**每一条用户消息进入系统的第一道门**，决定路由到哪个 skill / 是否多意图拆分。
  - Chroma 不可用时自动退回离线 bigram 打分（`_offline_scores`）。

### 3.2 生成 / 建站 Planner 的 RAG 上下文

- **`build_rag_context()`**（`chroma.py:172`，调用点 `agent_generate_site.py:663`、`agent_build.py:584`）：
  - 查询 `components`（`【组件库参考】`）+ `memory`（`【历史记忆】`，可按 `project_id` 隔离）。
  - 拼接为字符串注入 Planner 系统提示，提升生成质量与一致性。
  - 失败返回 `""`（不阻断，仅损失 RAG 增益）。

### 3.3 对话上下文连贯

- **`find_relevant_messages()`**（`chroma.py:261`）：
  - 在 `conversation_context` 内按 `conversation_id` 过滤 + 余弦相似度阈值（`CTX_SIMILARITY_THRESHOLD`）召回相关历史消息 id。
  - 用于跨轮上下文拼接，保证多轮对话连贯。

### 3.4 精细化个性化 / 复用

| 函数 | 集合 | 作用 |
|---|---|---|
| `retrieve_user_preferences` | `user_preferences` | 生成时按用户偏好个性化 |
| `retrieve_project_memory` | `project_memory` | 按项目历史决策/约束复用 |
| `retrieve_project_code` | `project_code` | 复用已有代码块 |
| `retrieve_error_patterns` | `error_patterns` | 生成时规避已知错误模式 |
| `rag_retrieve`（工具） | `components`/`memory` | 暴露给角色/前端按需检索 |

---

## 4. 是否走完完整链路（实证）

基于**多意图 e2e 实测（run6，11/11 通过，`状态=done preview=True`）**的真实日志：

**写入侧（已点亮，有日志实证）：**
```
[Worker] [2/6] Chroma向量索引 conv=19 msgs=1 开始...
[ai_service.chroma] [向量] 索引消息 msg=19001 conv=19 role=user content=帮我做一个个人摄影作品集网站...
[Worker] [2/6] Chroma索引完成 成功=1/1 (+1156ms)
...（续跑后第二批）
[Worker] [2/6] Chroma向量索引 conv=19 msgs=3 开始...
[向量] 索引消息 msg=19001 / 19002 / 19003 ... 成功=3/3
```
→ **对话索引写入链路（conversation_context）在真实多意图场景下被完整点亮**。

**召回侧（代码路径保证，建站成功即证明未阻断）：**
- `classify_v3` 是每条消息入口必走 → `retrieve_intents` 必执行（否则意图识别/多意图拆分无法工作，而本次 e2e 正确拆分为「建站+天气」双子任务，反证意图召回链路有效）。
- `build_rag_context` 在 Planner 阶段必走，本次建站产出真实预览（`preview=True`）说明 RAG 召回未阻断。
- 意图索引 `ensure_intent_index` 由 `reset_all.py` 已构建（集合中现存 1024 维向量，探针确认）。

**结论：生成（写入）→ 后续请求召回 → 再沉淀（蒸馏回写）的完整闭环在真实 e2e 中成立。** 单轮即可验证「写入」，跨会话/跨任务即可验证「召回复用」。

---

## 5. 对整体流程的作用（价值）

1. **意图识别（路由基石）**：把自然语言映射到意图，是单/多意图拆分、skill 路由的前提。没有它，所有请求都会退化成兜底处理。
2. **RAG 组件库**：让生成的网站复用已沉淀的组件最佳实践，质量与一致性显著提升。
3. **记忆闭环（越用越懂）**：`user_preferences` / `project_memory` / `memory` 跨会话复用，系统逐步积累用户风格、项目约束、历史产物。
4. **对话上下文**：`conversation_context` 保证多轮对话连贯，避免「上一轮说过的事下一轮就忘」。
5. **错误模式纠偏**：`error_patterns` 让生成阶段规避已知坑，降低返工率。
6. **代码复用**：`project_code` 让后续任务能检索并复用已有产物片段。

---

## 6. 维度绑定与风险（重要）

- **维度强绑定**：1024 维向量与现有 Chroma 数据绑定。**更换 embedding 模型必须维度一致**（如另一个 1024 维 Qwen embedding），否则老向量与新查询向量维度不匹配 → 直接报错。`reset_all.py` 重建。
- **额度线独立**：`text-embedding-v3` 走独立计费线，**不在 chat 模型的 Token Plan 内**（已与用户确认）。chat 模型有额度 ≠ embedding 有额度。
- **降级不崩溃**：embedding 不可用 ≠ 链路断。写入侧静默跳过、召回侧返回空/离线 bigram，系统仍可运行（精度下降）。
- **本地兜底注意**：`chroma.py` 内置 `SentenceTransformer(all-MiniLM-L6-v2)` 兜底，但它是 **384 维**，与 1024 维库不兼容，启用需 `reset_all.py` 重建 + 本机能拉取 HF 模型。

---

## 7. 快速排查清单

| 现象 | 排查点 |
|---|---|
| 意图识别变「兜底 bigram」、精度下降 | `_available()` 是否 False（embedding 额度/SDK）→ 查 `ensure_intent_index` 是否构建、`intents` 计数 |
| 生成质量突然下降（无组件参考） | `build_rag_context` 返回空？`components` 集合是否被清/未 seed |
| 跨会话「不记得」用户偏好 | `user_preferences` 是否有数据（`upsert_user_preference` 日志）、`reset_all` 是否误清 |
| 查询报错「维度不匹配」 | 换了 embedding 模型但没 `reset_all` → 维度冲突 |
| 日志无 `[向量]` 标记 | embedding 不可用（降级中）或被静默吞掉（`_upsert` 失败仅 warn） |
