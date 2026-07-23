# SeedAI v1.0 Agent 重构测试报告

> 测试时间: 2026-07-23 12:43-12:55
> 重构: 12→8 Agent + AgentInput/AgentOutput + recursion_limit + 强约束SystemPrompt

## 1. 验证结果

| # | 测试项 | 预期 | 实际 | 结果 |
|---|--------|------|------|------|
| 1 | AI 服务健康 | 200 | `{"status":"ok"}` | ✅ |
| 2 | 业务服务健康 | 200, MySQL+Redis ok | `{"status":"ok","checks":{"mysql":"ok","redis":"ok"}}` | ✅ |
| 3 | Agent 注册数 | >=8 | 8 | ✅ |
| 4 | Agent 命名 | 全部 agent_ 前缀 | agent_chat/search/design/doc/requirement/build/review/generate_site | ✅ |
| 5 | Intent 路由 | agent_chat 被调用 | `"skill": "agent_chat"` | ✅ |
| 6 | SSE done 事件 | 正确触发 | 有 done | ✅ |
| 7 | SSE token 流 | token 逐字推送 | 约 80 tokens | ✅ |
| 8 | SSE intent 检测 | level1=learn, level2=casual | ✅ | ✅ |
| 9 | 模型列表 | >0 | 3 模型 | ✅ |
| 10 | 递归保护 | recursion_limit=20 | 代码确认 | ✅ |
| 11 | qc_result 初始化 | 无 UnboundLocalError | 已修复 | ✅ |

## 2. 重构前后对比

| 维度 | 重构前 | 重构后 |
|------|--------|--------|
| Agent 数量 | 12（4组重叠） | 8（无重叠） |
| 命名规范 | 混乱（explain/xxx_agent） | 统一 agent_ 前缀 |
| 入参 | **kwargs 透传 | AgentInput dataclass |
| 出参 | 任意 dict/AsyncGenerator | AgentOutput + 强约束 JSON |
| System Prompt | 各写各的，无约束 | 统一 6 条硬约束 |
| 灰色地带 | 无处理 | escalate/clarifying 路径 |
| 递归保护 | 无 | MAX_RECURSION=20 |
| 文件数 | 12 skills | 8 agents |

## 3. INTENT_SKILL_MAP 变更

```
explain → agent_chat        search_agent → agent_search
design_agent → agent_design  generate_doc → agent_doc
requirement_agent → agent_requirement
generate_site → agent_build (合并 write_code/builder_agent)
review_agent + fix_agent → agent_review (合并)
generate_site → agent_generate_site (保留)
```

## 4. 已知限制

- SSE 流式通过 business proxy(7101)在 httpx stream 模式下偶发空事件（预存在，非本次引入）
- Chroma 重置需远程 Chroma 服务在线（当前云服务）
- 旧 skill 文件保留兼容，后续清理

## 5. Git 提交

```
1dd3482 fix: qc_result全局初始化+test timeout参数修复
0b6f1c0 feat: Agent/Skill v1.0 全局重构(12→8,结合业内最佳实践)
```
