# SeedAI vs 业内方案对比分析

> 目标：评估 SeedAI 当前/计划架构与 5 个主流 Agent 框架的差异，给出优劣判定和改进建议。

## 一、主流方案概览

| 框架 | 团队 | 核心理念 | 编排方式 | 适合场景 | 不适合场景 |
|------|------|----------|----------|----------|-----------|
| **LangGraph** | LangChain | 图状态机 | Supervisor→Worker 有向图 | 复杂分支、循环、人工审核 | 简单串联对话 |
| **AutoGen** | Microsoft | 对话协作 | Agent 间消息传递+GroupChat | 研究实验、代码执行 | 需要严格状态管控的场景 |
| **CrewAI** | 独立 | 角色模拟 | Manager→Agent 层级委托 | 快速原型、团队协作模拟 | 生产级可靠性要求 |
| **OpenAI Agents SDK** | OpenAI | 极简委托 | Handoff(直接转交) | 内部工具、快速上手 | 多模型切换、复杂编排 |
| **Claude Agent SDK** | Anthropic | 计算机使用 | Orchestrator→子Agent | 长时运行、代码/研发 Agent | 非 Claude 生态 |
| **SeedAI** | 我们 | 意图管道+Router+Skill | 5 模块分类→Router→Skill 执行 | 对话式 AI 建站 | 通用 Agent 编排 |

## 二、架构深度对比

### 2.1 编排模型

| 维度 | LangGraph | AutoGen | CrewAI | SeedAI |
|------|-----------|---------|--------|--------|
| 控制流 | 显式 DAG | 隐式对话 | Manager 委托 | 意图→Router→Skill |
| 并行支持 | ✅ 原生 | ✅ 会话级 | ✅ 层级并行 | ⚠️ splitter+多 Agent |
| 条件分支 | ✅ 一流 | ⚠️ 需自定义 | ✅ Manager 判断 | ✅ 意图分类 |
| 循环/重试 | ✅ 内置 recursion_limit | ⚠️ 需手动实现 | ⚠️ 需手动实现 | ✅ generate_site Reflexion≤3轮 |
| 人工介入 | ✅ interrupt_before | ✅ human_input_mode | ⚠️ 有限 | ✅ confirm+checkpoint |
| 可观测性 | ✅ LangSmith 全链路追踪 | ⚠️ 日志为主 | ⚠️ 文件输出 | ✅ trace_id 全链路+SSE 事件流 |

**判定**：SeedAI 架构 = **自定义的轻量 Supervisor 模式**。Router 类似 Supervisor，Skill 类似 Worker。与 LangGraph 比缺少显式图定义和内置断点续跑，但更轻量；与 AutoGen 比缺少动态协商能力，但更可控。

### 2.2 状态管理

| 维度 | LangGraph | AutoGen | OpenAI SDK | SeedAI |
|------|-----------|---------|------------|--------|
| 状态定义 | TypedDict/Pydantic 强类型 | 消息列表 | 隐式(SDK 管理) | 散布在多处(Redis+MySQL+Chroma) |
| 持久化 | ✅ SqliteSaver/PostgresSaver | ⚠️ 应用层管理 | ⚠️ 仅平台 trace | ✅ Redis Stream+MySQL Trace |
| 断线恢复 | ✅ checkpoint 时间旅行 | ⚠️ | ❌ | ✅ Stream XRANGE 回放 |
| 上下文窗口 | ✅ 消息摘要+记忆层 | ⚠️ 对话历史全量 | ⚠️ | ✅ L1/L2/L2+/L3 四层 |

**判定**：SeedAI 的**四层记忆架构是本项目最大优势**，LangGraph 仅到消息摘要层，AutoGen/CrewAI 更弱。但 SeedAI 状态分散在 Redis/MySQL/Chroma 三处，不如 LangGraph 的单一 TypedDict 清晰。

### 2.3 错误处理与可靠性

| 维度 | LangGraph | SeedAI |
|------|-----------|--------|
| 超时 | recursion_limit | repair_loop 30s/timeout |
| 重试 | 手动 retry 边 | Reflexion ≤3 轮 |
| 降级 | fallback 节点 | intent fallback→explain |
| 死循环 | ✅ 内置限制 | ✅ QC低分→repair |
| 幻象传递 | Validator 节点 | 三裁判 QC 交叉验证 |

**判定**：SeedAI 的 QC+repair 闭环与 LangGraph 的 Validator 节点异曲同工。但 SeedAI 缺少 LangGraph 的全局 `recursion_limit` 保护机制。

### 2.4 开发体验

| 维度 | LangGraph | CrewAI | SeedAI |
|------|-----------|--------|--------|
| 新增 Agent | 定义 node+edge 含状态 | 20 行 YAML | 注册 SkillEntry + handler |
| 学习曲线 | 中高（需学图概念） | 低（角色隐喻） | 中（需理解意图管道） |
| 调试 | ✅ LangSmith 可视化 | ⚠️ 日志 | ✅ SSE 事件流+trace_id |
| 模型绑定 | 无 | 无 | 无（多种切换） |

**判定**：SeedAI 新增 Agent 成本 = CrewAI < SeedAI < LangGraph。但没有可视化编排工具是短板。

## 三、SeedAI 独特优势（业内方案不具备的）

| 优势 | 说明 |
|------|------|
| **四层记忆压缩** | L0(MySQL) → L1(Redis摘要 TTL 1d) → L2(LLM精炼) → L3(Chroma偏好+项目记忆)。LangGraph 只为消息和摘要两层，AutoGen 更弱 |
| **Chroma 六集合** | 按 user_id/project_id 隔离的向量检索：对话上下文/用户偏好/项目记忆/代码语义/错误模式/组件库 |
| **QC 三裁判** | 3 个 LLM 各自 6 维打分→聚合。LangGraph/CrewAI 均无内置质量门禁 |
| **done 钩子链** | QC→闲聊重答→L2精炼→蒸馏→代码索引→git提交，六步非阻塞串联 |
| **SSE 实时推流** | 从 intent 到 done 全程事件流推前端，用户可实时看到每个阶段产出 |
| **修复闭环** | QC低分→自评×QC交叉定位→只重启失败阶段 ≤3轮/best-of-N。LangGraph 需手动实现 |
| **零框架依赖** | 不依赖 LangChain/LangGraph/AutoGen，全自研 Python+FastAPI，可完全掌控 |

## 四、SeedAI 可借鉴的业内最佳实践

### 4.1 从 LangGraph 借：显式状态 Schema + 全局递归限制

```python
# 当前：SeedAI 状态散落多系统
# 借鉴：用 Pydantic 统一定义 AgentState（类似我们方案中的 AgentInput/AgentOutput）
class AgentState(BaseModel):
    trace_id: str
    messages: list[dict]
    intent: dict | None = None
    requirement_doc: dict | None = None
    generated_html: str = ""
    review_passed: bool = False
    qc_result: dict | None = None
    recursion_count: int = 0

# 并在 Router 层加全局保护
if state.recursion_count > 20:
    return AgentOutput(decision="escalate", content="任务步数超限，已暂停")
```

### 4.2 从 AutoGen 借：Agent 间协商能力

```python
# 当前：Router 单向分发，Worker 只执行不反馈
# 借鉴：允许 Worker 返回 "needs_design_advice" → Router 调 design_agent
# 这已经在我们计划的 escalate 机制中体现了
```

### 4.3 从 CrewAI 借：YAML 配置 Agent 元数据

```python
# 当前：硬编码在 register_skill() 参数中
# 借鉴：用 agent_config.yaml 定义，热加载
agents:
  agent_chat:
    display_name: "小胡"
    role: "智能助手"
    intent_tags: ["闲聊","解释","对比","翻译"]
    model: "deepseek-chat"
    max_tokens: 500
    constraints:
      - "不生成代码"
      - "不操作文件"
```

### 4.4 从 LangGraph 借：可视化编排图

```python
# SeedAI 已有 SSE 事件流 → 在管理后台加一个 Agent 执行流可视化（Mermaid/流程图）
# 展示：intent→Router→skill_name→Planner→Coder→Reviewer→QC→refined→done
```

## 五、结论：该不该换框架？

| 方案 | 换框架（迁移到 LangGraph） | 不改框架（优化现有架构） |
|------|---------------------------|------------------------|
| 优势 | 显式状态管理、内置断点、可视化调试、社区生态、招聘友好 | 零迁移成本、完全可控、已验证可用、四层记忆+QC+repair 都是独有的 |
| 劣势 | 3-6 个月迁移周期、依赖 LangChain 生态、四层记忆/QC/repair 需重写 | 状态分散、无全局 recursion_limit、无可视化编排 |
| 结论 | **不建议全量迁移** | **建议借鉴最佳实践做增量优化** |

**推荐策略**：保持自研架构，借鉴 LangGraph 4 个最佳实践：
1. `AgentState` Pydantic 统一状态（已在 v1.0 方案中）
2. 全局 `recursion_limit`=20（新增 2 行）
3. YAML 配置 Agent 元数据（新增文件）
4. 管理后台加 Agent 执行流可视化（用 SSE 事件数据渲染）

SeedAI 的自研架构在**LLM 建站**这个垂直场景上，四层记忆 + Chroma 六集合 + QC 三裁判 + done 钩子链的组合拳，LangGraph/AutoGen/CrewAI 三个框架拼起来都做不到。通用 Agent 框架适合「通用任务编排」，SeedAI 适合「AI 建站全链路」，选型是合理的。
