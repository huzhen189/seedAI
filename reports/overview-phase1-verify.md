# Phase 1 工具可见化：重置 + 5 条 Live SSE 验证报告

## 一、做了什么
按指令「重置数据，你先测试 5 条数据」：
1. **重置全部数据**（`scripts/reset_all.py FORCE=1`）：DROP 12 张业务表、Redis FLUSHDB、Chroma 清 6 个运行集合（保留 components/intents/error_patterns 配置集）、意图索引重建、重建超管 **huzhen / huzhen189**。
2. **编写 5 条 SSE 探针**（`probes/probe_phase1_5cases.py`）：每条独立对话防跨轮意图污染，专门统计 `reasoning / tool_call / tool_result` 三类 Phase 1 事件是否经 `/api/chat` SSE 实时透出。
3. **实跑验证**：启动 7101 单进程后端，跑完 5 条用例，汇总计数与事件样本。

## 二、重大发现：Phase 1 原本完全失效（已修复）
- **症状**：首轮 5 条全部 `reasoning=0 tool_call=0 tool_result=0 done=True`。
- **根因**：`backend/app/agent/core/runner.py` 第 122 行构建作用域 ID 时把 `_ambient`（一个 `collections.deque`）放进了 `hash()`，而 **deque 不可哈希** → `TypeError: unhashable type: 'collections.deque'`。该异常发生在 `ToolEventBus.enter()` 之前，导致工具事件作用域从未注册，所有 `emit_*` 静默降级 → **三事件从不透出，且整条对话走到异常兜底文案**。
- **修复**：`_scope_id = (abs(hash((trace_id, sub_task_id or "0"))) ^ id(_ambient)) % (10**9)`，去掉不可哈希的 deque，用 `id()` 保唯一。
- **提交**：`713cec5`（本地，未 push / 未打 tag）。

## 三、验证结论（修复后）
| # | 类别 | 输入 | reasoning | tool_call | tool_result | 结论 |
|---|---|---|---|---|---|---|
| 1 | 闲聊 | 你好, 介绍一下你自己 | 1 | web_search×1 | 1 | ✅ |
| 2 | 实时搜索 | 2026 网页设计趋势 | 0 | 0 | 0 | ❌（路由到未埋点 skill）|
| 3 | 技术问答 | HTML 语义化标签 | 1 | web_search×1 | 1 | ✅ |
| 4 | 对比问答 | Grid vs Flexbox | 1 | web_search×1 | 1 | ✅ |
| 5 | 设计（对照）| 深色模式配色方案 | 0 | 0 | 0 | 对照（agent_design 未埋点，不计分）|

**埋点路径通过率 3/4**。Phase 1 机制本身确认生效——真实 `web_search` 工具调用与结果经 SSE 实时透出，WorkBuddy 式「思考→调用→观察」循环已落地。

**样本**（用例 1）：
```
reasoning : 检测到实时信息需求, 正在联网搜索最新资料…
tool_call : name=web_search id=tc_3 args={'query': '...', 'top_k': 3}
tool_result: name=web_search ok=True summary=返回 3 条结果
```

## 四、暴露的缺口（Phase 1 后续工作）
当前**只有 `agent_chat.explain_skill`（web_search）和 `agent_doc`（cos_upload）接入了 Phase 1 三事件**；`agent_search`、`agent_design`、`agent_build`、`agent_modify` 等 skill 的工具调用仍**不可见**。用例 2 路由到 `agent_search`（经核查完全无埋点）所以 0 透出——这是埋点覆盖问题，不是透出机制故障。

## 五、注意事项
- ⚠️ **本环境回收站不可用**（safe-delete 硬拦截），导致 `reset_all.py` 清 `artifacts/` 陈旧产物与 `*.log` 被拦，`artifacts/anon`、`artifacts/sites` 未清。请在有回收站的环境手动清理，或通过 git bash `rm -rf` 删除（非危险区，属运行产物）。不影响本次 SSE 验证。
- 测试账号固定：**huzhen / huzhen189**（超管，reset 重建）。

## 六、产物
- 探针脚本：`probes/probe_phase1_5cases.py`
- 运行日志：`reports/probe_run2.log`
- 详细报告：`reports/probe-phase1-20260730-015318.md`
