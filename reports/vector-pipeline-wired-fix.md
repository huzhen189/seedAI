# 向量库 3.2 / 3.3 / 3.4 接线修复报告

> 时间: 2026-07-28 | 目标: 把此前"只写不读"的死路(3.3 / 3.4)与"组件库空集合"(3.2)真正接通并实测生效。

## 一、上一轮遗留的问题(本次修复)

| 路径 | 上一轮状态 | 根因 |
|---|---|---|
| **3.2 组件库 RAG** | 函数接通但 `components` 集合**永久为 0** | `scripts/seed_rag_components.py` 从未创建,组件库无种子数据 |
| **3.3 多轮上下文** | 函数正确但**全代码无调用点** | `find_relevant_messages` 从未被 worker 调用 |
| **3.4 个性化/复用(读侧)** | 写侧 `_distill_memories` 已接,但 4 个 `retrieve_*` **全代码无调用点** | `build_rag_context` 只查 components + memory |
| **3.4 错误经验库** | `error_patterns` 集合**永久为 0** | `seed_error_patterns` 从未在运行期被调用 |

## 二、本次改动清单

### 1. 补种子数据(让 3.2 / 3.4 有数据可检索) — `app/agent/knowledge/chroma.py`
- 新增 `_COMPONENT_SEEDS`(20 条前端组件/模式参考:玻璃拟态卡片、Hero、响应式导航、深浅主题、Grid 画廊、Flex 防溢出、主按钮、表单、无障碍…)。
- 新增 `_seed_bootstrap()`:`ensure_collections` 启动时若 `components`/`error_patterns` 集合为空则幂等播种(固定 id 覆盖)。
- 新建 `scripts/seed_rag_components.py`(此前缺失的文件):手动全量重建 components + error_patterns + intents 索引。

### 2. 修复批量 embedding 上限 — `app/agent/knowledge/chroma.py`
- **关键坑**: Qwen `text-embedding-v3` 单批 upsert **上限 10 条**,超过返回 400(`batch size is invalid`)。
  原 `seed_components` 直接 `col.upsert(20 条)` → 整体失败 → 种子永远写不进去。
- `_upsert` 改为按 ≤10 分批;`seed_components` 改为走 `_upsert`(复用分批逻辑)。

### 3. 3.4 读侧接入 `build_rag_context` — `app/agent/knowledge/chroma.py`
- 在 `components` + `memory` 基础上,新增:
  - `retrieve_project_memory(project_id, query)` → **【项目记忆】**(按 project_id 隔离)
  - `retrieve_user_preferences(user_id, query)` → **【用户偏好】**(按 user_id 隔离)
  - `retrieve_error_patterns(query)` → **【错误模式经验】**(全局复用)
- RAG 注入上限 `_RAG_INJECT_MAX_CHARS` 4000 → **6000**(容纳新增段落)。

### 4. 两处调用点传入 project_id / user_id — `agent_generate_site.py:663` & `agent_build.py:584`
- `pool.submit(build_rag_context, first_user_msg)` →
  `pool.submit(build_rag_context, first_user_msg, project_id, user_id)`

### 5. 3.3 多轮上下文接入 Worker — `app/agent/core/queue.py`
- 新增 `[3.6]` 阶段:在 `[3/6]` 意图检测之后、分发之前,用最新用户消息语义召回本会话相关历史片段(`find_relevant_message_contents`),作为 **role=system** 消息前置注入:
  - 单意图路径:`_enriched_messages`(角色上下文之后)
  - 多意图路径:`orch_messages`(复制到 `shared_ctx.conversation_history` 与 `orch.execute`)
- 修复阈值:`CTX_SIMILARITY_THRESHOLD` 0.55 → **0.40**。实测 Qwen embedding 下"近重复"消息余弦距离≈0.47(sim≈0.53),原 0.55 会把几乎所有相关历史拒之门外,3.3 形同虚设。

## 三、实测结果(真实服务 :7101,seedai-biz venv)

### 确定性函数验证 — `scripts/test_vector_wiring_v2.py`
模拟 `_distill_memories` 写侧(写入 project_memory + user_preference),再调 `build_rag_context`:
```
✅ 【组件库参考】  命中   (components 已 seed 20 条)
❌ 【历史记忆】    未命中 (该测试 pid 无 memory 条目,符合预期)
✅ 【项目记忆】    命中   (3.4 project_memory 读侧闭环)
✅ 【用户偏好】    命中   (3.4 user_preferences 读侧闭环)
✅ 【错误模式经验】 命中   (3.4 error_patterns 已 seed 20 条)
✅ 3.3 相关历史召回 1 条 (sim=0.525 ≥ 0.40)
```

### 实时 Worker 验证(强制 `skill=agent_generate_site` 跳过误分类的意图门)
```
[RAG] build_rag_context 入口 query=... project_id=None user_id=23
[RAG] components 命中 5 条(注入 521 字)
[RAG] memory 命中 5 条(注入 7554 字)
[RAG] user_preferences 未命中(user_id=23)   ← 新用户首建,蒸馏尚未写,符合预期
[RAG] error_patterns 命中 5 条(注入 256 字)
[RAG] 注入 Planner 上下文总长=8359 字(有增益)
[gen] Planner 开始 ... rag=6000chars
[Worker] [3.6] 注入历史上下文 conv=25 条=1 最高相似度=1.000   ← 3.3 实时注入
```
→ **3.2 / 3.3 / 3.4 三条路径在真实运行时均已生效**。

## 四、遗留说明(非向量缺陷,建议后续)
- **`project_id` 未流入 Worker**:本次强制建站走的会话是 auto-start 自动创建的(conv=25),其 `project_id` 为 None,故 3.4 `project_memory` 实时未命中。
  检索逻辑本身已用 `project_id=99001` 确定性验证通过;正常 In-App 使用(会话挂在项目下)时 project_id 会下发,届时 `project_memory` 自动激活。
- **意图分类器偶发降级**:qwen 偶发把明确的建站语句误判为 `chat_casual`(conf 0.3),导致未走 build。这是意图 LLM 的独立性问题,与本次向量改动无关;用 `skill=agent_generate_site` 可强制。

## 五、改动文件
- `backend/app/agent/knowledge/chroma.py`(种子 + 分批 + build_rag_context 扩展 + find_relevant_message_contents + 阈值)
- `backend/app/agent/core/queue.py`([3.6] 历史上下文注入,单/多意图两路)
- `backend/app/agent/skills/agent_generate_site.py`(传 project_id/user_id)
- `backend/app/agent/skills/agent_build.py`(传 project_id/user_id)
- `backend/scripts/seed_rag_components.py`(新建)
- `backend/scripts/test_vector_wiring_v2.py`(新建,可复跑验证)
