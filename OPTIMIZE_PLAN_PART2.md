# 优化方案 PART2（2026-07-27）

> 用户 6 问（意图分类 × 角色对齐 / 取消续传生命周期 / Project 多 Constraints / qwen Token Plan 配置 / 前端 Planner / 优化方案）。
> 全部基于源码事实探查（非推测），证据均带 `文件:行号`。改动遵循铁律：仅本地 commit、不 push、不打 tag、改码即改文档。
> **状态（2026-07-27）**：用户已确认「改 P0/P1/P2」，§5 / §6(A–F) / §8 / §9 全部落地并完成 `py_compile` + 前端 `vue-tsc` 校验。
> 关联：`OPTIMIZE_PLAN.md` §4 已落地的 4 角色（product/design/dev/qa）重构。

---

## 0. 现状速览（探查结论）

| # | 主题 | 结论 |
|---|------|------|
| 1 | 意图 level1/level2 | 3 L1(chat/build/manage) + 15 意图 + 9 skill，L1/L2 为「目录查表」；skill→role 映射在 `handoff.py` 第二真源；`manage` 缺中文标签 |
| 2 | 取消/续传/幂等/状态机 | 有 cancel 标志 + 断连自动取消 + 回放 + checkpoint resume，但存在 **8 处真实缺口**（G1–G8） |
| 3 | Project 多 Constraints | 无 constraint 列，寄生于 `system_prompt`；`--forbid:` 解析为 `list[str]` 硬拦截；产物/状态全项目共享（设计意图） |
| 4 | qwen Token Plan | 聊天配置正确（`sk-sp-` + token-plan 主机）；但**聊天与嵌入共用 `qwen_base_url`，嵌入 key `sk-ws-` 属另一工作区 → 改 base_url 后嵌入会 401** |
| 5 | 前端 Planner | 多意图有 `SubTaskTrack`、单意图有 `ThoughtTrail`、建站/需求有 `await_confirm` 门；但通用/多意图**无发送前预览**、**角色 SOP 未可视化** |

---

## §5 意图 level1/level2 × 4 角色对齐 ✅ 已落地 (2026-07-27)

### 现状
- `intent_catalog.json`(v1.2.0)：15 意图 / 3 L1（chat/build/manage）/ 9 skill。L1/L2 是「目录查表」非推导（`cascade.py:262-263` 直接从意图字典取）。
- skill→role 映射 `handoff.py:ROLE_FOR_SKILL`（第二真源），与意图目录平行（`handoff.py:38-45`）。
- 缺陷：`router.py:LEVEL1_LABELS` **缺 `manage` 中文标签**（回退原始串 "manage"）。

### 问题
1. **双真源漂移风险**：改意图目录忘改 `handoff.py` 会静默错配角色。
2. 分类法仍是「技能中心」，未显式体现新 4 角色 SOP（产品→设计→开发→评审）。
3. `chat_design` 走 `agent_design`→design 角色，但 design 角色当前**无上游 PRD 注入钩子**（`build_upstream_context` 按 role 顺序，design 需要 product 的 PRD）。

### 建议方案（单一真源 + SOP 对齐）
- **单一真源**：在 `intent_catalog.json` 每个意图加 `role` 字段（`product`/`design`/`dev`/`qa`/`null`）；`handoff.ROLE_FOR_SKILL` 改为「从 catalog 派生」（`catalog.skill_for` 已是单一映射，加 role 字段即可消除第二真源）。
- **补标签**：`router.py:LEVEL1_LABELS` 加 `manage="管理操作"`。
- **L2 与 SOP 对齐**（建议映射）：

  | 角色 | 意图 L2 |
  |------|---------|
  | product | `build_requirement` |
  | design | `chat_design` |
  | dev | `build_site` / `build_page` / `build_modify` / `build_game` / `build_doc` |
  | qa | `build_fix` / `build_review` |
  | null（跨角色，不进 SOP） | `chat_*`(casual/explain/compare/translate/search) / `manage_delete` |

- **验证**：9 skill 全可映射到 4 role 之一或 null；`build_upstream_context` 依 role 注入上游交付物，design 现能拿到 product 的 PRD。

### 改动文件
- `backend/app/agent/intent/intent_catalog.json`
- `backend/app/agent/roles/handoff.py`（`ROLE_FOR_SKILL` → 派生）
- `backend/app/agent/core/router.py`（`LEVEL1_LABELS` 补 manage）

---

## §6 取消 / 续传生命周期 + 状态机 + 工具幂等（重点） ✅ 已落地 (2026-07-27)

### 诊断（8 处真实缺口，均源码取证）
- **G1** 取消为协作式、阶段/分块边界级，无法抢占进行中的 LLM 推理（`agent_build.py:509-510` 等；无 token 内中断）。
- **G2** 子任务状态**无 `cancelled`/`aborted`**；取消的子任务记为 `failed("用户取消")`（`models.py:54-60`；`orchestrator.py:284-289`）。
- **G3** 无子任务状态机 / 合法转换表，状态为散点字符串赋值（`orchestrator.py:127,291`）。
- **G4** 编排层取消**不级联**：仅当前流式子任务停止，兄弟/下游子任务继续（`orchestrator.py:121-173` 层循环无 `is_cancelled` 检查）。
- **G5** `resumeFromPlan` 复用旧 traceId + `resume:true` → `stream_exists=True` → 只回放旧流、不重跑 skill（`ChatView.vue:233-255` vs `proxy.py:781-787`）。
- **G6** 工具**无任何幂等键**；checkpoint resume 重跑会从 checkpoint 阶段重跑整段 skill，重复执行带副作用 tool（RAG/写盘/COS/外部 API）（`tool_registry.py:137-147`；全仓 `idempot` 零匹配）。
- **G7** 取消后前端仅得「已取消」文本，无「哪步取消 / 已完成 / 未完成」结构化清单（`agent_build.py:510` 空 payload；`ChatView.vue:853-864`）。
- **G8** `agent_build.py:630-638`「取消即存 checkpoint+paused」逻辑位于 `return`（629）之后，**不可达死代码** → planner 阶段取消无 checkpoint 保护。

### 设计目标
按「任务执行计划生命周期」建模：
`pending → running → (done | failed | cancelled | blocked | skipped) ↔ paused`

### 建议方案
**A. SubTask 状态机**（`core/models.py`）
- 新增 `SUB_CANCELLED="cancelled"`、`SUB_ABORTED="aborted"`、`SUB_PAUSED="paused"`。
- 加 `SUB_TRANSITIONS: dict[str, set[str]]` 守卫 + `SubTask.transition(to)` 方法（非法转换记日志/抛）。

**B. 编排层级联取消**（`core/orchestrator.py`）
- 在层循环与兄弟 task 间插入 `is_cancelled` 检查；收到取消时：进行中→`cancelled`，未开始→`skipped`，已 done 保留。
- 收尾 emit 结构化 `cancel_summary`：`{cancelled:[id], completed:[id], skipped:[id]}`。

**C. 工具幂等**（`registry/tool_registry.py`）
- `invoke(name, *args, idempotency_key=None, **kwargs)`；key=`{trace_id}:{name}:{hash(args)}`；Redis 缓存结果短 TTL（如 600s）；命中且成功则直接返回，避免 resume 重复副作用。
- 重放（同 tid）本来不重跑 tool，无影响；此键专门保护 resume 重跑。

**D. 前端取消摘要**（`chat.ts` + `ChatView.vue` + `SubTaskTrack.vue`）
- `aborted` 事件带 payload（或新增 `cancel_summary` 事件）；前端渲染「取消摘要」卡：列出取消的步骤、已完成、未完成。

**E. 修 G5**（`app/proxy.py`）：`resume=true` 且 `stream_exists(tid)` 时，删除旧流（或强制新 tid）后再入队，确保 checkpoint 重跑生效。

**F. 修 G8**（`agent_build.py`）：把「取消即存 checkpoint+paused」逻辑移到 `return` 之前的可达分支。

### 改动文件
- `backend/app/agent/core/models.py`、`orchestrator.py`、`registry/tool_registry.py`、`core/queue.py`、`app/proxy.py`、`app/agent/skills/agent_build.py`
- `frontend/src/api/chat.ts`、`views/ChatView.vue`、`components/SubTaskTrack.vue`

### 落地注记（2026-07-27）
- **A 状态机**：`models.py` 新增 `SUB_CANCELLED/SUB_ABORTED/SUB_PAUSED` 常量 + `SUB_TRANSITIONS` 转移表 + `SubTask.transition(to)`（非法转移记 warning 拒绝，不抛异常避免中断流）。
- **B 级联取消**：`orchestrator.py` 层循环进入新层前插 `is_cancelled` 检查（pending→skipped 级联）；收尾 emit `cancel_summary{cancelled,completed,skipped}`；`_run_one` 风险门控 HIGH→blocked、MEDIUM→skipped、取消→cancelled（原记 failed）。`RoleOrchestrator` 继承复用父类层循环/`_run_one`，默认路径自动覆盖。
- **C 工具幂等**：`tool_registry.invoke(name,*args,idempotency_key=None,trace_id=None,**kwargs)`；key=`ai:tool:idek:{trace_id}:{name}:{hash(args,kwargs)}`，命中且缓存成功直接返回；Redis 异常降级忽略。`get_redis()` 取连接（decode_responses=True）。
- **D 前端取消摘要**：`chat.ts` 注册 `cancel_summary` 事件 → `ChatView.onCancelSummary` 修正子任务泳道状态并渲染「取消摘要」卡。
- **E 修 G5（根因纠正）**：原诊断写 `proxy.py:781-787` 的 `resume` 分支回放 `replay_stream`，但实际代码已重构——`resume` 真实处理在 `publisher()` 内（所有请求生效），`await_confirm` 阶段已 `open_channel(tid)` 致 `stream_exists(tid)` 恒 True → 落入回放分支不重跑。修复：resume 分支先 `delete_channel(tid)` 再 `open_channel`+`enqueue` 强制重入队。`queue.py` 新增 `delete_channel`（基类桩 / MemoryBackend 清 history+progress / RedisBackend `r.delete` 异常降级）。
- **F 修 G8**：删除 `agent_build.py` 中位于 `return` 之后的不可达「取消即存 checkpoint+paused」死代码（9 行）。

---

## §7 Project 多 Constraints / 产物与状态共享

### 现状
- Project **无** constraint 列（`models.py:55-81`）；约束寄生于 `Project.system_prompt`(Text)。
- `--forbid:` 行解析为 `project_constraints: list[str]`（Tier2 硬拦截，`proxy.py:91-109` → `safety.py:65-90`）。
- 多条约束以 list 支持；但**项目级共享、不隔离**：`build_status` 单值，`Artifact` 仅按 `project_id`（无 constraint 列，`models.py:126-149`）。
- `api/models.py` 的 `constraints: dict` 字段**未使用**（遗留契约）。

### 语义澄清
约束 = **项目级护栏**（软注入 skill 提示 + 硬禁用词），天然全项目共享产物与状态——这是**设计意图**，不是 bug。多条约束以 list 合并生效。

### 建议
- **保持共享语义（推荐）**：约束即全局规则，产物/状态全项目共享，无需改。
- **仅当用户需要「按约束独立追踪产物」**：新增 `constraints` 表（id/project_id/type/content/status），`artifacts` 加 `constraint_id` 外键，每约束独立状态。属 P3 可选，改动大，先确认需求。
- 清理遗留：`api/models.py` 的 `constraints` 字段若不用则删，避免误导。

---

## §8 Qwen Token Plan 配置修（建议本轮即修，低风险） ✅ 已落地 (2026-07-27)

### 问题
聊天与嵌入**共用 `qwen_base_url`**，但：
- 聊天 key `QWEN_API_KEY=sk-sp-...`（token-plan 人版，正确）；
- 嵌入 key `QWEN_EMBEDDING_KEY=sk-ws-...` 属 **ws 私有工作区**，host 应为 `ws-rao72of9tmiy6llq...`；
- 把 `QWEN_BASE_URL` 改成 token-plan 主机后，嵌入请求会带 `sk-ws-` key 打到 token-plan 主机 → **极可能 401**。

### 修复（3 处）
1. `shared/config.py` 加 `qwen_embedding_base_url: str`（默认 ws 主机）。
2. 根 `.env` 加 `QWEN_EMBEDDING_BASE_URL=https://ws-rao72of9tmiy6llq.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
3. `app/agent/intent/chroma.py:55` 与 `app/agent/tools/rag_retrieve.py:67` 的 `api_base=settings.qwen_base_url` → `settings.qwen_embedding_base_url`。

### 验证
重启 7101 后，embedding 请求应打到 ws 主机返回 200（日志核对 `app.log`）。

---

## §9 前端 Planner 展示增强 ✅ 已落地 (2026-07-27)

### 现状
- 多意图：`SubTaskTrack`（🧩 并行编排 + 策略芯片 + N 子任务泳道 + 实时流 + 合并成功/失败计数）。
- 单意图：`ThoughtTrail`（意图徽标 + 计划卡 + 阶段时间线）。
- 建站/需求：`await_confirm` 方案确认门（唯一「执行前预览」）。

### 缺口
1. 通用/多意图**无发送前计划预览**（计划随流出现）。
2. 计划与 **4 角色 SOP 未可视化关联**（用户看不到「产品→设计→开发→评审」链路）。
3. 取消后无结构化摘要（见 §6 D）。

### 增强
- **执行前 plan_preview**：意图识别后、执行前发 `plan_preview` 事件（所有意图），前端渲染「执行计划」卡，含 **SOP 角色链路 badge**（产品分析师→设计顾问→开发工程师→质量评审）。
- **SOP 阶段条**：`SubTaskTrack`/`ThoughtTrail` 顶部加 4 阶段进度条，高亮当前角色。
- 取消摘要卡（§6 D）。

### 改动文件
- `frontend/src/views/ChatView.vue`、`components/SubTaskTrack.vue`、`components/ThoughtTrail.vue`、`api/chat.ts`

---

## §10 优先级与排期

- **P0（正确性/数据风险，建议立即）**：§8 qwen 嵌入 401 修；§6 G5 resumeFromPlan 死跑；§6 G8 checkpoint 死代码。
- **P1（生命周期完整性）**：§6 A/B/C/D 状态机 + 级联取消 + 工具幂等 + 前端取消摘要。
- **P2（清晰度/体验）**：§5 意图×角色对齐；§9 Planner 角色可视化 + 执行前预览。
- **P3（可选）**：§7 结构化 Constraints（确认需求后做）。

---

## 附：用户 6 问直接结论

1. **意图 level1/level2 要改吗？** 要——但核心是消除「意图目录 / role 映射」双真源，给每个意图加 `role` 字段并让 `handoff.py` 从 catalog 派生；同时补 `manage` 中文标签。L1/L2 本身是用户意图维度，保留；新增的 `role` 维度才与 4 角色 SOP 对齐。
2. **取消 vs 续传生命周期？** 有骨架（cancel 标志 + 断连自动取消 + 回放 + checkpoint resume），但缺状态机、不级联、无工具幂等、前端无结构化摘要、resumeFromPlan 复用旧 tid 只回放不重跑。按 §6 补。
3. **Project 多 Constraints？** 支持多条（list 禁用词），且天然共享产物与项目状态（护栏语义）。若需「按约束独立工作流」才需建模为 subtask/conversation 或加 constraint 表（P3）。
4. **qwen Token Plan？** 聊天正确；嵌入与聊天共用 base_url 会 401，按 §8 拆分 `qwen_embedding_base_url`。
5. **前端 Planner 清晰吗？** 有，但通用/多意图无发送前预览、角色 SOP 未可视化；按 §9 增强。
6. **优化方案**：见上 §5–§10。
