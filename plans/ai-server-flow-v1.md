# ai_service 流程改进方案 v1（选项决策自治 + 选择解析 + 端到端日志）

> 状态：✅ 已实施（2026-07-22 合入 `backend/ai_service` + `frontend`；待用户手动重启 7102 加载新代码）
> 范围：仅 `backend/ai_service` 的意图管道、Worker、选项事件、日志；前端 `ChatView.vue` 配套兜底。
> 待办：本地 git 提交（不 push）+ 联调验证（见 §6）。

---

## 0. 问题定位（来自实测）

**复现场景**：用户问 X → 系统低置信 → 出 `options`（候选如 `requirement_agent` / `explain`）→ 用户回 `"B"` → 系统把 `"B"` 当新 query 重新分类 → 又低置信 → 又出 `options` → 死循环。

**根因（4 点）**：
1. `intent/pipeline.py` `_aggregate` Step 5：`confidence < 0.5` 即 `decision="options"`，**阻塞**等待用户选择（`core/queue.py:433` `continue`）。
2. **无"待选项"状态记忆**：下一轮 `classify_v2` 从零跑，完全不知道上一轮出过选项。`"B"` 被当垃圾输入。
3. 前端 `ChatView.vue:131-174` **只在点 radio 时**调 `resendWithSkill(choices[i])` 带 `skill=` 重发；用户**打字 `"B"`** 走普通 `send` → 不带 `skill` → 后端无法识别。
4. `intent/context.py` 只看最后 assistant 文本做意图修正，不识别"上轮是选项菜单 + 本轮是选择"。

**用户两条核心诉求**：
- A. 工具路由**不该让用户判断**，系统自己决定；若不确定，去**调整工具的语义说明**让 AI 能决断。
- B. 既然出了选项，用户回 `"B"` 必须被正确识别（当前完全没判断出来）。
- C. 从 ai_server 收到用户输入的第一步 → 返回用户结果，每一步都要**清晰明了的日志**。

---

## 1. 设计原则

- **工具路由不阻塞用户**：AI 直接选 top-1 候选执行，不抛多选项让用户判。
- **确需歧义时**：优化 skill 语义描述（`INTENT_SKILL_MAP` + 各 skill 的 `description`/`intent_tags`）提升分类置信度，让 AI 能决断，而非抛给用户。
- **保留"可覆盖"**：若仍出选项（如需求方案多选这种**业务级**决策，用户确实要选），必须能正确解析 `"B"`/`"2"`/`"选B"`/`"第二个"`。

---

## 2. 方案

### 2.1 决策自治：默认直接路由（消除阻塞式 options）
改 `intent/pipeline.py` `_aggregate`：
- 删除 `decision="options"` 阻塞分支（或仅作极低置信兜底，且不再 `continue` 等待）。
- 始终 `decision="route"`，`selected_skill = tools.skills[0].name`（已是 top 候选）。
- 若候选 > 1 且置信偏低，在 `plan` 里附加 `alternatives=[...]`（**非阻塞提示**），供前端展示"已选 X，也可说'用 Y'切换"。
- `intent/tools.py`：低置信（<0.5）仍返回多候选用于 `alternatives` 提示，但**不再触发 options 决策**。

### 2.2 语义澄清：让 AI 能自己决断（回应"调整工具的语义说明"）
- 审计 `intent/tools.py:13-34` 的 `INTENT_SKILL_MAP` 与 `app/skills/*`、`app/tools/*` 的 `description` / `intent_tags`：
  - `requirement_agent` vs `explain` 易混 → 明确边界（需求分析/出方案 vs 概念讲解/答疑）。
  - 给 `intent/semantic.py` 的 LLM 分类提示词补充裁决规则："意图模糊时优先选最具体 / 最上游的 skill"。
- 目标：正常输入 `confidence ≥ 0.5`，走直接路由，不触发选项。

### 2.3 选择解析：让 `"B"` 被正确识别（修当前 bug，双保险）
**(a) 后端状态化解析（主修复，最稳）**
- 新增 `pending_options` 存储：决定出选项时，以 `conversation_id` 为 key 存候选列表（Redis `gen:opt:<conv>` 带 TTL；无 redis 则内存 dict）。
- 在 `classify_v2` **最前面**插入 **选择解析器** `resolve_selection(messages, conversation_id)`：
  - 取最后一条 user 输入；模式匹配选择 token：
    - 单字母 `^[A-Ha-h]$`
    - 数字 `^[1-9]$`
    - `选[项]?[A-H1-9]`、`用[第]?[一二三…]`、`第二个` 等
  - 命中且存在 `pending_options` → 返回 `PipelineResult(decision="route", selected_skill=candidates[idx], from_selection=True)`，**短路**后续分类。
- 这样无论前端点按钮还是打字 `"B"`，后端都能正确路由。

**(b) 前端兜底（体验）**
- `ChatView.vue`：当 `optionsData` 激活时，若用户输入匹配选择 token，拦截普通 `send`，改调 `resendWithSkill(choices[idx])`。双保险。

### 2.4 端到端清晰日志（回应"每一步打印清晰"）
统一 `trace_id` 贯穿 + 阶段化步骤日志：
- `main.py /generate`：`[入口] 收请求 trace=.. conv=.. model=.. msgs=N skill=auto/指定 input="…"`
- `core/queue.py` worker：现有 `[1/6]~[6/6]` 保留，补充每步**耗时(ms)** 与 `trace`。
- `intent/pipeline.py`：`[管道 1/5]~[5/5]` 已较清晰，补 `selected_skill`/`confidence`/`decision` 一行快照。
- `core/runner.py` `run_skill`：进入/退出各 skill 打印 `[技能 <name>] 开始/结束 耗时`。
- **日志增强**：在 `logging_config.py` 增加 `TraceIdFilter`，用 `contextvars` 注入 `trace=..` 到每条记录；保证 grep 一个 trace 能看到全链路。
- 关键事件必打：收到输入 → 意图分类结果 → 决策(skill) → 选项(若有) → 执行 → 完成/QC。

---

## 3. 影响面与风险

- `decision="options"` 现有消费方：`queue.py:433`、`ChatView.vue:131-174`、`AdminView.vue:362` 统计。改为非阻塞后需同步：
  - 前端：`options` 事件仍可用作 `alternatives` 提示；**仅保留 `requirement_agent` 的业务方案选择**（那是真实需用户决策的场景）。
  - 统计：`options` 计数改为 `route(带 alternatives)`，或保留选项事件但语义调整。
- `skills/requirement_agent.py:110` 的"多方案选项"是**业务决策**，与工具路由选项不同，保留并同样套用 2.3 的选择解析。

---

## 4. 实施步骤（建议顺序）

1. **日志骨架**（`logging_config` TraceIdFilter + 各阶段耗时）—— 先有可观测性再改逻辑。
2. **pipeline/tools 决策改造**（默认路由 + alternatives）。
3. **选择解析器**（`resolve_selection` + `pending_options` 存储）。
4. **语义澄清**（`description`/`intent_tags` 审计 + 分类提示词裁决规则）。
5. **前端双保险**（选项激活时拦截选择 token）。
6. **联调**：
   - "帮我做网站" → 应直接路由 `requirement_agent`（不弹选项）。
   - 低置信输入 → 验证 `alternatives` 提示出现。
   - 打字 `"B"` → 验证能选中（无论选项来自工具路由还是需求方案）。

---

## 5. 待确认（推荐项已标注）

- **Q1**：是否彻底去掉"工具路由"的阻塞选项 UI，只保留"已选 X，可切换"的轻提示？（**推荐：是**）
- **Q2**：`requirement_agent` 的"方案多选"是否保留为真正的用户决策 UI？（**推荐：保留**，属业务决策，且套用 2.3 解析 `"B"`）
- **Q3**：日志是否全量开启 TraceIdFilter 注入（每条带 `trace=..`）？（**推荐：是**，仅 ai_service 日志，不影响业务端）

---

## 6. 实施记录（2026-07-22）

### 6.1 已落地改动
| 文件 | 改动 |
|---|---|
| `app/logging_config.py` | 新增 `TraceIdFilter` + `set_trace()` + `trace_context()`；formatter 注入 `[trace=..]`；file/console handler 均挂 filter。（Step 1） |
| `app/main.py` | `/generate` 与 `stream()` 内 `set_trace(trace_id)`；入口→出口全链路完成日志。 |
| `app/intent/pipeline.py` | `[0/5]` 选择解析短路：命中 `pending_options` / 显式"用 X" → 直接 route，不重跑 LLM；`_aggregate` Step5 删除 `decision="options"` 阻塞 → `decision="route"` + `plan` 附 `alternatives`（非阻塞）。 |
| `app/intent/selection.py` | **新增**。 `parse_selection`（A-H/1-9/中文数字/选X/用X/切换X/第X个）+ `set/get/clear_pending_options`（内存 dict 兜底，TTL 1800s）+ `resolve_selection`（显式覆盖优先，其次待选项映射）。 |
| `app/intent/semantic.py` | LLM 分类提示词加 **裁决规则**：模糊时自行选最具体 skill（build 默认 site / learn 默认 explain），不把选择推给用户；confidence 给 ≥0.6。（Step 4 语义澄清） |
| `app/core/queue.py` | Worker `[5/6]`：把阻塞式 options 改为**非阻塞 alternatives 事件**（系统已选 top1 并继续）；publish 后 `set_pending_options(conv, [top1, *alts])`（存完整有序候选，使"B"→第2候选）；补每步耗时日志。 |
| `frontend/src/types.ts` | 新增 `AlternativesEvent { selected, skills, hint? }`。 |
| `frontend/src/api/chat.ts` | `ChatCallbacks` 加 `onAlternatives?`；SSE 加 `alternatives` 事件监听。 |
| `frontend/src/views/ChatView.vue` | ① `alternativesData` ref + `parseSelectionToken`（对齐后端正则）+ `clearAlternatives`；② `onAlternatives` 回调接事件；③ `send()` 顶部拦截：若 `alternativesData` 激活且输入为选择 token（如"B"）→ `resendWithSkill(对应skill)`；④ `resetGenState` 清 `alternativesData`；⑤ 模板非阻塞候选提示条（可点击/输入切换）；⑥ 对应 CSS。 |

### 6.2 关键设计修正（实施中发现）
- **"B" 索引歧义**：最初 `pending_options` 只存 `alts`（不含 top1），导致 "B"(idx1) 越界。改为存**完整有序候选 `[top1, *alts]`**，使 "A"=top1(无操作)、"B"=alts[0]、… 与用户心智一致。已在 `selection.py` 加 `idx==0` 无操作保护。
- **`requirement_agent` 业务方案多选**：保留其 `options` 弹窗（q-2 确认项），不纳入本次管道级修复；其选项为业务方案（非 skill 名），前端 radio UI 仍为主路径。

### 6.3 验证
- `py_compile` 全部通过；`parse_selection`/`resolve_selection` 单测： `"B"→1→explain`、`"A"→0→无操作`、`"用 explain"→override→explain`、`"帮我写2个函数"→None`（不被劫持）。
- 前端未跑构建（环境缺 node_modules），待用户 `npm install && npm run dev` 自验。

### 6.4 联调清单（待用户重启 7102 后）
- [ ] "帮我做网站" → 直接路由 `requirement_agent`（draft 状态走需求分析），不弹阻塞选项。
- [ ] 低置信输入 → 出现 `alternatives` 提示条（已选 X · 可切换 Y）。
- [ ] 在提示条激活时打字 `"B"` → 正确切换；点 chip 也能切换。
- [ ] 日志：grep 同一 `trace=` 可见 入口→管道[0/5]→Worker[1/6~6/6]→技能→完成 全链路。

