# SeedAI 产品记忆压缩方案（短期 → 长期）

> 目标：让 AI agent 在**每次任务完成后**，把短期对话记忆压缩成结构化「项目记忆」（长期），并定期 compaction 防止无限膨胀；新任务 step1 融合「最近 K 条 + 项目记忆 + 用户卡片」做语义判断。
> 关联：`docs/AI核心流程-现状与新方案对比.md` §八 Phase A（"上下文真喂进去"的落地依赖本方案）。

## 0. 现状盘点（实测代码）
- **短期原始**：MySQL `Message` 全量历史（`business/app/models.py`）。
- **短期压缩**：Redis 滚动摘要 `proxy.maybe_compress_summary` —— 每 6 条消息调 `deepseek-chat` 压成 ≤200 字，`summary:{conv_id}` 7 天 TTL，随对话流透传给 ai_service 作 `conversation_summary`。**这是"短期→更短短期"，不是长期。**
- **长期（非结构化）**：Chroma 向量索引全量消息（`knowledge/chroma.py` 的 `index_message` / `find_relevant_messages`），按语义相似度检索历史。**原始消息向量，从不蒸馏、从不合并、无限增长。**
- **缺口**：① 无结构化「项目记忆」持久化（跨会话的目标/决策/约束/偏好/产物）；② 无用户卡片；③ 无"短期蒸馏成长期 + 长期定期压缩"机制。

## 1. 三层记忆架构（目标态）
- **L0 短期原始**：`Message` 表（已有，不变）。
- **L1 短期压缩**：Redis 滚动摘要（已有，复用 + 强化为"每轮任务结束再压一次"）。
- **L2 长期结构化**：新增 `project_memories` 表（MySQL）+ Chroma（带 metadata 语义检索）。**这是核心新增。**
- **L3 用户卡片**：`User` 表扩展 `card_json` 字段（或独立 `user_cards` 表），存稳定偏好（行业/风格/技术栈/禁忌）。

## 2. 蒸馏（L0/L1 → L2，任务完成后触发）
- **触发点**：trace 收到 `done` 事件 / 一轮完整任务结束（`core/queue.py` 现有 done 钩子，与 §8 git 提交并列）。
- **动作** `ai_service/app/memory/distill.py`：
  - 取本轮 = L1 摘要 + 本轮消息 + 本轮产生的产物(artifact 路径/ requirement_doc)；
  - 调 LLM（小模型 deepseek-chat）抽取**结构化条目**：
    `{type: decision|constraint|preference|artifact|fact, content, importance(1-5), project_id, ts}`；
  - 写 `project_memories` 表 + upsert Chroma（metadata: `project_id, type, ts`）。
- **失败降级**：蒸馏异常仅 `logger.warning`，不阻断主生成（与 QC 一致）。

## 3. 压缩 / Compaction（L2 自清理，防膨胀）
- **触发**：单项目 `project_memories` 条数 > N（默认 50）**或** 条目 age > 30 天。
- **动作** `ai_service/app/memory/compact.py`：
  - LLM 读取该项目全部 memories → 合并重叠/过期条目 → 输出**精简集**（drop T2 易逝细节，保 T0 关键决策/约束）；
  - 用精简集整体覆盖该项目 memories（保留 `id` 最早那条作锚，其余 upsert/删除）。
- **预算守卫**：压缩后若仍 > 上限，按 `importance` 升序删，保高 importance。

## 4. 检索（新任务 step1 融合）
- `intent/context.py` 的 `run_context` 新增来源 **"project_memory"**：
  - 取最近 K=5 条原始消息（L0/L1）+ Chroma top-k 相关 `project_memories`（按当前输入向量）+ `user_cards`；
  - 拼成结构化 context 喂给 step1 语义判断（替代/增强现有"关键词兜底"）。
- 这样既吃满"最近 5 条 + 项目记忆 + 用户卡片"，又非阻塞。

## 5. 复用点 & 落地落点
- **复用** `proxy.maybe_compress_summary` 做 L1（不重写）。
- **复用** `knowledge/chroma.py` 做 L2 向量（新增 `index_memory` / `find_relevant_memories`，不改 `index_message`）。
- **新增** `ai_service/app/memory/`：`distill.py` / `store.py` / `compact.py`。
- **新增表** `project_memories`（含 `project_id, type, content, importance, ts`）；`init_db()` 已支持 `create_all` 自动迁移，无需手跑 migration。
- **挂点**：`core/queue.py` done 钩子调 `distill`；`intent/context.py` 检索加 `project_memory` 来源。

## 6. 成本 / 预算
- 蒸馏：每轮任务 **1 次** LLM（deepseek 小模型）→ 成本极低。
- compaction：低频（阈值触发，非每轮）→ 成本可控。
- 检索：Chroma top-k 本地向量，微秒级。

## 7. 风险 / 回滚
- 纯**新增**存储（L2/L3），不改动 `Message` / 现有 Chroma / 主生成链路。
- 蒸馏/压缩失败均降级 warn，不影响对话与建站。
- 回滚：不创建表即可（或 `DROP TABLE project_memories`），无副作用。

## 8. 与既有方案的衔接
- 本方案 = 用户新 AI 核心流程 **step1「语义判断」的上下文供给层**。step1 要的"最近5条 + 项目记忆 + 用户卡片"由本方案提供。
- 与 §8 git 版本、QC 自修复互不冲突，可并行落地。

## 9. 建议实施顺序
1. 建 `project_memories` 表 + `user_cards`（schema 迁移）。
2. `memory/distill.py` + done 钩子挂接（先打通"任务结束→写长期记忆"）。
3. `memory/store.py` Chroma 记忆索引。
4. `intent/context.py` 加 `project_memory` 检索来源。
5. `memory/compact.py` + 阈值触发。
6. 前端可在"项目设置"展示/编辑用户卡片与项目记忆。
