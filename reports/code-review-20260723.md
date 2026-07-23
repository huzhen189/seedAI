# SeedAI 全局代码审查报告

> 审查日期：2026-07-23 ｜ 审查人：Senior Developer（高级开发工程师）
> 方法：4 个子系统并行审计（business / ai_service-core+intent / ai_service-skills+tools+qc / frontend）+ 对头部高危点逐行复核验证（proxy / rules / tools / chroma / queue / orchestrator / context / ChatView / chat.ts / MessageBubble / db / reset_all / qc / providers）。
> 目标：按需求逐行核对实现、找 bug、找不足、查异常/边界处理。

---

## 一、总体结论

近期重构的**方向基本落地**：8 个 agent、2 大意图方向、断点续联、幽灵进程清理、MySQL 探活（`pool_pre_ping=True`/`pool_recycle=1800`）都正确实现了。但本轮改动**引入了一个会让所有聊天请求 500 的致命 bug**，以及若干真实 bug 与需求缺口。审计初稿里有一部分**误报**，我已逐条复核剔除（见第七节）。

**一句话：先修 P0（1 行 `import time`），否则 `/api/chat` 必然 500、AI 永远收不到请求。**

---

## 二、🔴 致命（P0，必须立刻修）

### 1. `backend/business/app/proxy.py` 缺少 `import time` → 每个聊天请求必崩
- **位置**：`proxy.py:679` `t_start_chat = time.time()`（publisher 生成器**第一行**），以及 `:865` `_elapsed = (time.time() - t_start_chat) * 1000`。
- **证据**：全文件 import 区（`proxy.py:20-49`）只有 `from datetime import datetime`，**没有任何 `import time`**。全仓扫描确认：使用 `time.time()` 却缺 import 的文件**仅此一个**（ai_service 各文件均正常 import）。
- **影响**：`publisher()` 是 `StreamingResponse` 的异步生成器，第一行就 `NameError: name 'time' is not defined` → 请求在**把它 POST 给 7102 之前**就抛异常 → 业务端 500、AI 核心永远收不到请求。
- **与你现象的吻合**：你报过"你好到了业务端但 AI 没收到"——正是这个 bug 的特征（路由命中、鉴权过、消息拼好，但流还没开始就崩）。**这极可能就是你反复遇到的聊天 500 根因。**
- **修复**：在 `proxy.py` 顶部加 `import time`。（1 行，零风险。）

---

## 三、🟠 高（P1，真实 bug，建议本次一起修）

### 2. 多意图（split）SSE 永不发 `done` → 前端永久卡在"生成中"
- **位置**：`core/queue.py:606-714`（split 分支）+ `core/orchestrator.py:11,259`。
- **证据**：`Orchestrator.execute` 显式**丢弃 `done`**（`orchestrator.py:259` `if ev_name in ("intent","done"): continue`，docstring 第 11 行明说"编排器不 emit done"）。Worker 在 `queue.py:649-651` 把子任务流里的 `done` 捕获进 `done_event`，但 split 分支 orchestrator **根本不 yield done** → `done_event` 恒为 `None` → `queue.py:710` `if done_event is not None:` 不成立 → **不发布终止事件**。`MemoryBackend.subscribe`（`queue.py:253` `while True: await q.get()`）无限阻塞。
- **影响**：任何触发多意图拆分的复杂请求，前端收到 orchestration/subtask 事件后**永远等不到 done**，spinner 不消失、无法继续对话。
- **修复**：在 `orchestrator.execute` 末尾（merge 之后）`yield ev("done", {})`；或 Worker 在 split 分支兜底发布 `done`。

### 3. `INTENT_SKILL_MAP` 缺 2 个 agent（8 个 agent 只有 6 个可达）
- **位置**：`intent/tools.py:13-29`。
- **证据**：映射只含 `agent_chat / agent_search / agent_design / agent_requirement / agent_build / agent_review`，**缺 `agent_doc` 和 `agent_generate_site`**。
- **影响**：`agent_doc`（文档生成）与 `agent_generate_site`（整站生成）通过正常意图路由**永远无法被选中**；`("build","site")`/`("build","page")` 等都映射到 `agent_build`。若 `agent_build` 与 `agent_generate_site` 是不同实现，整站生成能力实际走的是 build 副本。
- **修复**：补齐 `("build","site"): "agent_generate_site"` 等映射；并确认 `agent_build` 与 `agent_generate_site` 是否真为两个独立 agent（见 P2 #10 孤儿文件）。

### 4. `_index_project_code` 死分支（代码永不索引进 Chroma）
- **位置**：`core/queue.py:130` `if skill_name != "generate_site": return`；调用点 `:814` 传的是 `skill_name`（实际为 `agent_build`/`agent_generate_site`）。
- **证据**：守卫写死旧名 `"generate_site"`，而 INTENT_SKILL_MAP 实际下发的是 `agent_build`/`agent_generate_site` → 条件**永远为真 → 直接 return**，建站后的代码块从不被索引到 `project_code` 集合。
- **修复**：守卫改为 `skill_name not in ("agent_build", "agent_generate_site")`。

### 5. `rules.py` 引用未定义 `learn_kw` → 规则模块静默失效
- **位置**：`intent/rules.py:98-104`。
- **证据**：`learn_kw` 从未定义（只有 `build_kw`/`chat_kw`），`run_rules` 走到第 98 行即 `NameError`。但 `pipeline.py:94-104` 用 `try/except` 包住了 `run_rules`，异常被吞 → `rule_result` 保持空 `RuleResult()`，**关键词意图分类（build_kw/chat_kw）整模块不生效**，退化为纯语义 LLM。
- **影响**：不崩溃，但一大块零延迟规则逻辑是死代码；"建网站/搜索/翻译"等本可秒判的意图现在全靠 LLM，慢且易错。
- **修复**：要么定义 `learn_kw`（若确有 learn 意图），要么把第 98-104 行的 `learn` 分支删掉（schema 里已无 learn，只有 chat/build/unsupported）。

### 6. `context.py` `_CORRECTION_MAP` 发出非法 `level1`
- **位置**：`intent/context.py:176-184`（`代码生成→code`、`文档→doc`、`翻译→translate`、`设计/搜索/教程讲解→learn`）。
- **证据**：`VALID_LEVEL1` 只有 `chat|build|unsupported`，但修正表给出了 `code/doc/translate/learn`，均为非法值。这些值经 `pipeline.py:196` `final_l1 = ctx_correction.get("level1", final_l1)` 进入最终意图 → `run_tools` 查 `INTENT_SKILL_MAP` 查不到 → 降级 `explain`（chat），且下发给前端的 intent 事件 `level1` 非法。
- **修复**：把修正表的 level1 收敛到 `chat`/`build`（如"代码生成"→`build/fix`，"文档"→需新增 doc 意图或在 build 下处理，"翻译"→`chat/translate` 但 level1 必须是 chat）。

### 7. `MessageBubble.vue` QC `partial` 时 `qc.dimensions[d]` 未判空 → 气泡崩溃
- **位置**：`frontend/src/components/MessageBubble.vue:162-166`。
- **证据**：`qc.dimensions[d].mean` / `qc.dimensions[d].scores` 直接取值，无 `?.`。而 UI 本身就显示 `qc.partial` 徽标（意为部分裁判失败/超时），此时 `qc.dimensions` 里**缺失**某些 `QC_DIMENSIONS` → `qc.dimensions[d]` 为 `undefined` → `.mean` 抛 `TypeError` → 整个气泡组件崩溃。对比 `AdminView.vue` 回放表用了 `?.`（更安全），说明这是已知坑但聊天气泡漏了。
- **修复**：`qc.dimensions?.[d]?.mean ?? 0`，并对 `scores` 做同样保护。

---

## 四、🟡 中（P2，需求缺口 / 隐患）

### 8. 应用内 `reset_db` 不清 Chroma（与脚本路径不一致）
- **位置**：`backend/business/app/db.py:190-233` 的 `reset_db`；对比 `scripts/reset_all.py:58-74`（**脚本确实清空了所有 Chroma 集合**）。
- **证据**：`reset_db()` 只 DROP MySQL 表 + `FLUSHDB` Redis + 重建 + 种子用户，**完全不碰 Chroma**；且 `knowledge/chroma.py` 根本没有 `reset()` 函数（全文件已读，确认无）。
- **影响**：你定的规则"重置须同步清 Chroma"——**手动跑 `scripts/reset_all.py` 是对的，但前端/管理后台触发的应用内重置不会清 Chroma**，两边行为不一致。
- **修复**：在 `chroma.py` 加 `reset()`（删全部集合），并在 `reset_db()` 调用它（与脚本逻辑对齐）。

### 9. 前端未处理 `refined` 事件
- **位置**：`frontend/src/api/chat.ts` 无 `addEventListener('refined', …)`；需求事件流是 `node/intent/token/qc/done/refined`。
- **影响**：done-hook 链的"精炼"结果事件被丢弃，前端无展示。功能上不崩，但漏了需求列的事件。

### 10. 12→8 重构残留孤儿 skill 文件未删
- **位置**：`backend/ai_service/app/skills/` 下 `explain.py / search_agent.py / design_agent.py / generate_doc.py / requirement_agent.py / generate_site.py / write_code.py / fix_agent.py / review_agent.py / builder_agent.py / rag_retrieve_skill.py`。
- **证据**：`skills/__init__.py` 只导入 8 个新 agent；上述旧文件仍在仓库、与新 agent 重复定义同名 handler，`builder_agent.py` 还会注册第 9 个非需求 agent。若误 import 即 `NameError`/注册冲突。
- **修复**：确认无引用后删除；`agent_build` 与 `agent_generate_site` 二选一（配合 #3）。

### 11. `resume()` 未传 `resume: true`
- **位置**：`frontend/src/views/ChatView.vue:915-929` 的 `resume()` → `startChat(...)` 无 `resume:true`；后端 `proxy.py:642,652` 的 checkpoint 恢复都 `if resume:` 才触发。
- **影响**：刷新后重连走的是 `trace_id + stream_exists` 回放（基础重连可用），但**服务端 checkpoint_data 注入（`resume_mode=correct/resume`）不会触发**，断点续联的"从断点继续生成"能力可能降级为"从头重播"。建议按你之前定的断点续联需求实测确认。

### 12. QC 混合判定（降本）未落地
- **位置**：`backend/ai_service/app/qc.py:51` `_LLM_DIMS = ("correctness","completeness","readability")`（**定义了从未使用**）；`:63` 裁判 prompt 要求 6 维全出；`:151-157` 只对 safety/compliance 做**上限钳制**（非确定性替代）。
- **证据**：docstring 第 8 行写"compliance/safety/efficiency 走确定性地板，仅 correctness/completeness/readability 走 LLM 三裁判"，但代码里 6 维全部交给 LLM（3 裁判 × 6 维 = 3 次 LLM 调用，每次都评 6 维），`_LLM_DIMS` 是死代码，未实现"降本"。efficiency 也无独立确定性规则（只有 safety 上限）。
- **影响**：成本未优化（3× 调用），与文档/注释描述不符。

### 13. `queue.py` 递归守卫是空操作
- **位置**：`core/queue.py:471-472,469` `qc_result=None` 注释写"全局递归保护"，`if recursion_count >= 20: 终止`，但 `recursion_count` 取自 job、**从未自增或重新入队**。
- **影响**：当前单 Worker 循环架构本就无递归，守卫无害但是误导性死代码（标了 MAX_RECURSION=20 却永不触发）。建议要么实现真正的重入队计数，要么删掉并加注释说明当前架构无需。

### 14. `providers.get_chat_model` KeyError 风险 + `max_tokens` 写死
- **位置**：`backend/ai_service/app/providers.py:126` `p = PROVIDERS[model_id]`；`:133` `max_tokens=4096`。
- **证据**：未知 `model_id` 直接下标 → `KeyError`（虽实践中 model_id 来自校验过的列表，但缺防御）；`max_tokens=4096` 对整站 HTML 生成可能截断长产物。
- **修复**：改为 `PROVIDERS.get(model_id)` + 抛 `ModelUnavailableError`；站点生成类调用用更大上限。

### 15. 主题切换缺失（项目标准要求）
- **位置**：全前端 `grep theme|dark|light|system|data-theme|toggleTheme` 仅命中 `style.css`（CSS 变量）、`MarkdownView.vue`、`webllm/*`（误命中）、`RightPanel.vue`、`useWebLLM.ts`——**无可用 light/dark/system 切换 UI**。
- **影响**：项目标准"每个站点必须有主题切换"。若属于需求范围，则缺失。请按你的规范确认是否要补。

---

## 五、🟢 低 / 异味（建议顺手清理）

- **`proxy.py:534` token 经 URL `?token=`**：当前聊天走 Cookie 无 Bearer，但 `q`/其他参数在 URL 里会出现在 referer/日志。可接受，但 PII 参数勿进 URL。
- **`proxy.py:574` 记用户消息全文（≤500 字）到日志**：PII 落入日志，生产建议脱敏。
- **宽 `except` 吞错**：`queue.py:493`（Chroma 索引）、`analytics.py` 各处、`repair.py:98` 等，QC/索引失败只 warn 不面向用户。可接受的降级，但建议关键失败有可观测指标。
- **`config.py` `jwt_secret` 默认 `"dev-secret-change-me"`**：未设时应拒绝启动（防伪造 token）。
- **`core/git_site.py:255`** `os.close(tempfile.mkstemp(...)[0])`：建完即关的临时文件句柄，属泄漏/困惑，建议用 `tempfile.NamedTemporaryFile`。
- **`proxy.py:709`** 错误帧 `yield str`，而 `:821` `yield bytes`：SSE 帧类型不统一（虽 Starlette 能处理 str，但建议统一 bytes）。
- **`frontend/src/api/chat.ts:98-100,96-97,107`** `unsupported/block/confirm/options/alternatives/paused` 监听器未 `es.close()`：这些事件后若服务端不再发 `done` 且连接关闭，EventSource 会**自动重连重发**导致重复生成。当前 `unsupported` 等场景服务端都会补 `done`，风险低，但建议这些半终态事件也显式关闭或在收到后由调用方接管。

---

## 六、需求符合度矩阵

| 需求 | 状态 | 备注 |
|---|---|---|
| 8 agent / 2 意图方向（chat/build/unsupported） | ⚠️ 部分 | 架构在，但 `INTENT_SKILL_MAP` 漏 2 agent（#3），`context.py` 发非法 level1（#6），规则模块死（#5） |
| 意图 schema {level1,level2,label,level1_label,level2_label,confidence,industry} | ✅ | 字段齐 |
| SSE 事件 node/intent/token/qc/done/refined | ⚠️ | `refined` 前端未处理（#9）；多意图 `done` 缺失（#2） |
| 断点续联（刷新/失败重连） | ⚠️ | proxy 已允许空 messages（之前修的）；但 `resume()` 未传 `resume:true`（#11），多意图仍卡 |
| 重置须清 Chroma | ⚠️ | 脚本 `reset_all.py` ✅；应用内 `reset_db` ❌（#8） |
| QC 三裁判 + done 钩子链 | ⚠️ | QC 跑通；但混合判定未降本（#12）；`repair.py` 自纠错闭环未接线（见下） |
| 测试脚本可复用 + 完整文档 + 精细统计 + 可追踪日志 | ✅（脚本侧） | `run_tests.py`/`complex_test.py` 在 |
| WebLLM 弃用 | ✅ | `useWebLLM.ts` 置 `WEBLLM_DISABLED`，`webllm/*` 无执行路径 import（但 `ChatView` 留了 `contextHint` 悬空引用，见 #7 旁注） |
| MySQL 探活 / 无静默 SQLite 回退 | ✅ | `pool_pre_ping=True`/`pool_recycle=1800`（`db.py:32-33`） |
| 幽灵进程清理 | ✅ | `schedule_biz_restart` 杀全部 :7101 LISTENING（`db.py:257`） |
| 主题切换 | ❌ | 缺失（#15） |

> 旁注：`ChatView.vue:842-876` 用 `if (true) { /* ... */ }` 把 WebLLM 代码整段注释掉，但 `contextHint` 仅在该注释块内声明，而 `:909` `startChat({contextHint, …})` 仍在引用 → **运行时 `ReferenceError`，每次发送都会抛**。若你当前 dev 还能发消息，说明跑的是旧构建，**请重新构建验证**；建议直接删掉这个悬空引用（WebLLM 已弃用，本就不该传 `contextHint`）。这条我标为"高疑似"，因为它与"之前能发消息"的现象冲突，需要你重建后实测确认。

---

## 七、审计误报纠正（重要，避免你被假警报带偏）

复核后，初稿里以下几条**不成立**，已剔除：

1. **"多意图 SSE 永不终止（整体误报）"**：单意图（主路径）`run_skill` 会 yield `done`，Worker 捕获后正常发布——单意图没问题；**只有 split 分支**因 orchestrator 不 emit done 才挂（即本报告的 #2）。
2. **"unsupported→agent_chat 解释降级丢失"**：`queue.py:561-577` 对 `unsupported` **确实调用了 `agent_chat`** 并先发解释流、再补 `unsupported` 事件、最后 `done`——降级正常执行，事件也送达。初稿误判。
3. **"`import time` 缺失遍布多文件"**：全仓扫描确认**仅 `proxy.py` 缺**，ai_service 全部正常。初稿夸大。
4. **"`astream_with_fallback` 未真正 fallback 是 bug"**：这是**设计选择**——架构故意不自动换模型，而是发 `retry` 事件让前端弹框让用户选替代模型（`providers.py:142-144` docstring 写明）。非 bug。仅 `get_chat_model` 的 `KeyError` 风险（#14）值得修。
5. **"Chroma 是 6 个集合"**：实际代码里有 **7 个**（`components/memory` + 新增 `user_preferences/project_memory/project_code/error_patterns` + `conversation_context`）。数量差异不影响结论，但记录准确。

---

## 八、优先级修复清单（建议顺序）

1. **P0**：`proxy.py` 加 `import time`（解锁所有聊天）。
2. **P1**：多意图 `done` 缺失（#2）；`INTENT_SKILL_MAP` 补 agent（#3）；`_index_project_code` 守卫（#4）；`rules.py` `learn_kw`（#5）；`context.py` 非法 level1（#6）；`MessageBubble` QC 判空（#7）；`ChatView` `contextHint` 悬空引用（旁注）。
3. **P2**：应用内重置清 Chroma（#8）；`refined` 事件（#9）；删孤儿 skill（#10）；`resume` 标志（#11）；QC 混合判定（#12）；递归守卫（#13）；providers 防御（#14）；主题切换（#15）。

> 需要我现在就动手修 P0 + P1 这批吗？P0 是 1 行零风险修复，建议立刻做；P1 我可一并改完并跑类型/导入自检。你定。
