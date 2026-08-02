# M8 — 闲聊路径接通真实模型（概览）

> 本文件为 SeedAI 全链路重构 M8 里程碑的交付说明。后端 `:7101` 仍运行，可用固定账号登录复查。

## 交付内容

### 1. 新链路统一 LLM 客户端 `app/llm/`
- `LLMClient` / `chat_completion`：OpenAI-compatible 异步客户端，**只服务 S0–S9 Pipeline**，不耦合 legacy `app/agent/*`。
- 供应商优先级：**qwen（默认，settings.qwen_*）→ deepseek（兜底，settings.deepseek_*）**。
- 每次调用带 30s 超时；某供应商失败自动故障转移到下一个；统一 `LLMError` 便于上层降级。
- `get_llm_client()` 单例缓存；`health()` 自检。

### 2. 闲聊路径从 stub 变真模型
- `app/domains/chat/service.py::ChatService.respond`：原 echo stub（"我已理解你的问题：…"）替换为真实 `chat_completion` 调用，带 SeedAI 建站助手 system prompt。
- **失败优雅降级**：模型不可用时返回友好文案，绝不中断 S0→S9 链路。

### 3. 端到端验证 `scripts/smoke_chat_llm.py`
login → auto-start（建项目+会话）→ `POST /api/chat`（纯闲聊）→ 读 SSE `done.reply`
- 纯闲聊「你觉得好的网站设计最关键的三个原则是什么」→ **4/4 通过**，回复为 qwen 真实生成（非 echo）。
- 含建站动词的消息（如"做一个餐厅官网"）按预期被意图路由判成 `site×create`，走 SiteWorkflow 生成站。
- 关键约定：助手文本在 `done` 事件的 `reply` 字段；`assistant` 角色消息只落库、不单独发 SSE 事件。

### 4. 回归
- M7 `scripts/smoke_project_ops.py` 仍 **28/28**，建站/发布/审批/生命周期无回退。

## 提交
- 本地 commit `2c8c5f3`（仅本地，未 push）：M8 LLM 客户端 + 闲聊路径接线 + 冒烟脚本。

## 现状与剩余（规范 M0–M11）
- ✅ **M0–M8**：后端核心已功能闭环并实跑验证（SiteWorkflow 生成 + 真实 LLM 闲聊 + publish/trash/restore/purge + 审批闸门 + S0–S9）。
- ⏭️ **M9**：前端五阶段 Rail / 单一 Reducer / 审批卡 / 离线队列 / 独立预览 Origin（前端 `:7100` 是否仍指向旧接口待核）。
- ⏭️ **M11c**：删旧链——`main.py:50-51` 仍 import `app.agent.skills`/`app.agent.tools`；`app/agent/*`、`proxy.py`、`app/projects.py` 待删（删除前需确认新链路无残留依赖）。
- 已知遗留（非本轮引入）：M2 骨架测试 `test_skeleton_pipeline_*` 仍失败（期望 `session=None` 全 NO_OP，与真实 Pipeline 冲突），随 M11c 一并处置。
- 风格说明：当前 SiteWorkflow 为确定性 premium 模板生成；完整 LLM 驱动生成（M6 Tool 平台 / BYOK / 16 原子 Tool）尚未接入新链路。

## 测试账号（供复查）
- 账号 `e2e20_seedai_test` / `testpass123`，后端 `http://127.0.0.1:7101`。
