# SeedAI 全链路 E2E 测试报告（20 条语句 · 由简到难）

> 测试时间：2026-07-27
> 测试方式：自动 harness（注册 → 登录 → 建项目 → 逐条发 SSE 对话，强制 `model=deepseek`），单进程 FastAPI `:7101`
> 结论：**20/20 条语句最终均正常返回终止事件 `done`，无 500、无挂起、无崩溃。**

---

## 一、本轮回测中修复的 Bug（测试中途停下修复）

| 编号 | Bug | 现象 | 根因 | 修复 |
|---|---|---|---|---|
| B1 | `NameError: name 'time' is not defined` | 所有「建站/生成站点」类语句（`agent_build`、`agent_generate_site`）直接 `error` 终态，**语句 7/8/9 首跑全部失败** | 两个 skill 文件使用了 `time.monotonic()` / `time.time()` 但**漏写 `import time`** | 在 `agent_build.py` 与 `agent_generate_site.py` 顶部补 `import time` |
| B2 | AI 统计写入静默失败 | 日志刷 `record_generate_request failed: unknown command HELLO` + `RuntimeWarning: coroutine ... never awaited` | `app/agent/analytics.py._get_redis()` 未设 `protocol=2`，云 Redis 拒绝 RESP3 的 `HELLO` 握手（缓存/队列客户端都设了，唯独这块漏了） | `_get_redis()` 补齐 `protocol=2`（对齐 `cache.py` / `queue.py`） |
| B3 | harness 续跑误判 | 失败后把语句标成「已完成」，下次续跑会跳过、永远不重测 | `done[sid]=True` 在 `error` 分支也执行 | 仅「成功终态」才标记完成，错误态保留待重跑 |

> 另：清理了上一轮遗留的临时诊断文件桩（`_lifespan_worker.marker` / `_worker_started.marker`）与重复的 `/models` 路由；`main.py` 的 `lifespan` 绑定（上一轮根因，worker 池不启动导致 `/api/chat` 永久挂起）已确认生效。

---

## 二、汇总

- 总语句：**20**
- 正常 `done`：**20**（含 `block`/`clarify` 这类「成功拦截/澄清」也属设计内正常终态）
- 错误 `error` / 超时 / 静默断开：**0**（修复 B1 后）
- 命中**多意图编排（orchestration）**：语句 **15、20**（各 3 子任务，并行执行 + merge）
- 安全拦截（block）：**18**（删除项目，被正确拦截，未执行）
- 澄清（clarify）：**16**（复杂需求先澄清，属正常决策）

> 注：语句 7/8/9 在第 1 轮因 B1 报 `error`，修复后第 2 轮均 `done`（最终口径以第 2 轮为准）。

---

## 三、逐条「预期 vs 实际」对照表

列说明：
- **路由实际**：`skill=`（最终执行技能）/ `intent`（意图 level1/level2）/ `orch`（是否走编排）/ `sub`（子任务数）/ `交互`（block/clarify 等）
- **一致**：✅=与预期吻合；⚠️=正常返回但**意图识别/路由与预期有差异**（非崩溃，属质量观察点）

| # | 语句 | 预期 | 实际路由 | 终态 | 一致 |
|---|---|---|---|---|---|
| 1 | 你好 | 闲聊→agent_chat | skill=agent_chat / chat/casual | done | ✅ |
| 2 | 今天天气怎么样？ | 闲聊→agent_chat | skill=agent_chat / chat/casual | done | ✅ |
| 3 | 写一首关于春天的短诗 | 文档→agent_doc | skill=agent_chat / chat/casual | done | ⚠️ 走闲聊而非文档技能（诗歌类产出由 chat 承担） |
| 4 | 把『Hello World』翻译成中文 | 翻译→agent_chat/doc | skill=agent_chat / chat/translate | done | ✅（translate 子类） |
| 5 | 给我讲个冷笑话 | 闲聊→agent_chat | skill=agent_chat / chat/casual | done | ✅ |
| 6 | 总结一段话 | 摘要→agent_doc | skill=agent_chat / chat/casual | done | ⚠️ 走闲聊而非文档技能 |
| 7 | 设计一个简洁的登录页面 | 设计→agent_design | skill=agent_build / build/page | done | ⚠️ 系统无独立 agent_design，设计并入 build/page（合理收敛） |
| 8 | 做一个个人博客网站 | 建站→agent_generate_site | skill=agent_generate_site / build/site | done | ✅ |
| 9 | 电商网站（商品列表页+购物车） | 建站→agent_generate_site | skill=agent_generate_site / build/site | done | ✅ |
| 10 | 写一份待办事项产品需求文档 | 需求文档→agent_requirement | skill=agent_chat / chat/casual | done | ⚠️ **未触发 agent_requirement**（需求文档技能漏召回） |
| 11 | 搜索 AI 行业新闻 | 搜索→agent_search | skill=agent_search / chat/search | done | ✅ |
| 12 | 检查代码 def add(a,b) | 代码评审→agent_review | skill=agent_review / build/review | done | ✅ |
| 13 | 公司官网+介绍文章 | 双意图→orchestration | skill=agent_generate_site（单技能，未拆分） | done | ⚠️ **未识别为双意图**，只建站未并行写文档 |
| 14 | 产品首页+营销文案 | 双意图→orchestration | skill=agent_build（单技能） | done | ⚠️ **未识别为双意图** |
| 15 | 待办网站+使用说明文档+搜索同类 | 三意图→orchestration(3) | **orch=True, sub=3**（generate_site/doc/search） | done | ✅ 完美命中多意图 |
| 16 | 在线教育平台（多页+课程文档+搜竞品） | 复杂多意图→orchestration | interaction=**clarify**（先澄清，未直接编排） | done | ⚠️ 走了澄清而非直接编排（合法决策，但体验上未像 15/20 那样直接执行） |
| 17 | 把刚才博客改成深色主题 | 迭代修改→build/modify | skill=agent_chat / chat/casual | done | ⚠️ 因 harness 每条新建独立会话、无历史上下文，「刚才那个网站」无指代 → 退回闲聊（harness 测试手法导致，非产品 bug；真实连续对话应有上下文） |
| 18 | 删除我的项目 | 危险操作→拦截/确认 | interaction=**block**（安全拦截，未执行） | done | ✅ 高危操作被正确拦截 |
| 19 | 金融数据看板+使用文档+对比竞品 | 复杂多意图→orchestration | skill=agent_chat / chat/casual | done | ⚠️ **典型三意图却未触发编排，退回闲聊**（多意图召回漏判，与 13/14 同类） |
| 20 | 旅游小程序官网+景点文章+搜目的地+预订流程页 | 极复杂多意图→orchestration | **orch=True, sub=3**（generate_site/doc/search；其中 generate_site 因「中风险待确认」被延迟，doc+search 成功并 merge） | done | ✅ 多意图编排执行，部分交付说明清晰 |

---

## 四、质量观察点（非崩溃，建议后续优化）

1. **多意图编排召回不稳定**：语句 15、20 能正确拆 3 子任务并执行；但 **13、14、19 同样明显是多意图却没触发编排**，退回单技能或闲聊。根因在意图混合级联的「多意图门控/拆分」判定对某些句式（含「并/再/还要」的并列结构）敏感度不足。这是当前最大体验短板。
2. **文档类单意图（诗歌/摘要/需求文档）未独立路由**：3、6、10 都走 `agent_chat` 承担，未调用 `agent_doc` / `agent_requirement`。功能上 chat 能产出，但缺失了专门技能的 richer 输出与**需求文档统计埋点**（10 号本应触发 `requirement_doc` 统计，实际未触发）。
3. **独立 `agent_design` 技能不存在**：设计类需求被并入 `build/page`（7 号、并间接影响 14 号），属合理收敛，但术语与预期不完全一致。
4. **迭代/指代类需求依赖连续会话上下文**：17 号在「每条独立会话」测试下失配。真实产品中同一会话内的「改成深色主题」应命中历史站点——这块需前端/会话层保证上下文透传。
5. **安全拦截（18 号）与澄清（16 号）行为正确**，是系统亮点。

---

## 五、遗留项

- 多意图召回阈值/句式覆盖（观察点 1、2）建议作为下一轮优化专项。
- `agent_requirement` 召回（观察点 2）建议补充进意图 catalog。
- 日志中仍有偶发 `TypeError: not enough arguments for format string`（logging 模块级，某条日志含字面 `%` 触发），**不影响业务**，但会污染日志，建议后续统一用 `logger.info("...%s", var)` 而非在消息体里夹带 `%`。

---

*测试产物：`backend/_e2e_results.jsonl`（原始逐条）、`backend/_e2e_final.json`（最终口径）。harness 与产物按约定不进版本控制。*
