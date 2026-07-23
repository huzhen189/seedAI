# SeedAI v1.0 Agent/Skill 重整理方向

> 目标：12→8 agent（消除重叠），两大方向（chat/build）各4个，命名规范化，入参/出参强约束JSON。

## 一、现状诊断

### 1.1 当前 12 个 skill 重叠矩阵

| 名称 | 方向 | 本质 | 与谁重叠 |
|------|------|------|----------|
| `explain` | chat | 问答/解释/闲聊 | — |
| `search_agent` | chat | 联网搜索 | — |
| `rag_retrieve` | chat | 向量检索 | 更像 tool，不是 agent |
| `design_agent` | chat | 设计咨询 | 与 `requirement_agent` 设计阶段重叠 |
| `generate_doc` | chat | 文档生成 | 与 `explain` 文档意图重叠 |
| `requirement_agent` | build | 需求分析 | 分析阶段含设计建议 |
| `builder_agent` | build | 代码生成 | 与 `generate_site`/`write_code` 三重重叠 |
| `generate_site` | build | 建站全流程 | Planner→Coder→Reviewer 一体化 |
| `write_code` | build | 代码编写 | 与 `builder_agent` 重叠 |
| `fix_agent` | build | Bug 修复 | 与 `review_agent` 形成"评审→修复"链 |
| `review_agent` | build | 代码评审 | 与 `fix_agent` 形成"评审→修复"链 |
| `rag_retrieve` | — | 工具 | 应归入 tools/ 目录 |

### 1.2 命名问题
- 有的叫 `xxx_agent`，有的不叫——不一致
- `builder_agent` / `write_code` 语义重叠但不统一
- `rag_retrieve_skill` 文件名和注册名不一致

### 1.3 入参/出参问题
- 大部分 skill 接收 `**kwargs` 透传，无类型约束
- 出参格式不统一（dict/AsyncGenerator/文本），Router 需要大量判断

---

## 二、目标架构（8 agent）

```
SeedAI Agent 体系
├── Chat 方向（4 个）
│   ├── agent_chat          问答/闲聊/技术解释
│   ├── agent_search        联网搜索（合并 rag_retrieve）
│   ├── agent_design        设计顾问（从 requirement 抽离）
│   └── agent_doc           文档生成
│
└── Build 方向（4 个）
    ├── agent_requirement   需求分析（不含设计建议）
    ├── agent_build         建站全流程（合并 builder_agent + write_code）
    ├── agent_review        代码评审+Bug修复（合并 review_agent + fix_agent）
    └── agent_generate_site 站内生成（原 generate_site，保留内部四阶段）
```

### 2.1 各 Agent 定位

| Agent | 方向 | 单句定位 | 输入 | 输出 |
|-------|------|----------|------|------|
| `agent_chat` | chat | 问答解释闲聊，只讲不做 | 用户问题+上下文 | 文本（流式 token→done） |
| `agent_search` | chat | 联网搜索+向量检索，返回参考源 | 搜索关键词 | 搜索结果摘要+来源链接 |
| `agent_design` | chat | 设计咨询：配色/布局/字体/动效 | 设计需求描述 | 设计建议+示例代码 |
| `agent_doc` | chat | 技术文档/README/API 文档 | 文档需求+上下文 | Markdown 文档 |
| `agent_requirement` | build | 需求分析+结构化需求文档 | 用户描述+项目上下文 | JSON requirement_doc |
| `agent_build` | build | 建站全流程：规划→编码→自审→修复 | requirement_doc+项目上下文 | 单文件 HTML+预览 URL |
| `agent_review` | build | 代码评审+问题修复，给出结构和修复后代码 | HTML/代码+上下文 | 评审报告+fix 建议+修复后代码 |
| `agent_generate_site` | build | 站内生成：保留现有 Planner→Coder→Reviewer→Reflexion | 用户请求+项目记忆 | HTML+SSE 事件流 |

### 2.2 合并/删除/降级

| 旧 | 去向 | 理由 |
|----|------|------|
| `rag_retrieve_skill` | 降级为 `tools/rag_retrieve.py` | 不是 agent，是工具 |
| `builder_agent` | 合并进 `agent_build` | 与 generate_site/write_code 三重重叠 |
| `write_code` | 合并进 `agent_build` | 同上 |
| `fix_agent` | 合并进 `agent_review` | 评审→修复是一体的 |
| `review_agent` | 合并进 `agent_review` | 同上 |
| `explain` | 改名 `agent_chat` | 命名规范化 |
| `search_agent` | 改名 `agent_search` | 命名规范化 |
| `design_agent` | 改名 `agent_design` | 命名规范化，从 requirement 抽离 |
| `generate_doc` | 改名 `agent_doc` | 命名规范化 |
| `requirement_agent` | 改名 `agent_requirement` | 命名规范化 |
| `generate_site` | 改名 `agent_generate_site` | 命名规范化，保留内部逻辑 |

---

## 三、统一 Agent 规范

### 3.1 输入契约

每个 Agent 必须接收标准入参类（Pydantic）：

```python
@dataclass
class AgentInput:
    model_id: str                    # 使用的模型
    user_text: str                   # 本轮用户输入（必填）
    messages: list[dict]             # 对话历史（必填）
    trace_id: str | None = None      # 追踪 ID
    user_id: int | None = None       # 用户 ID（Chroma 隔离）
    project_id: int | None = None    # 项目 ID（Chroma 隔离）
    conversation_id: int | None = None
    conversation_summary: str = ""   # L1 摘要
    requirement_doc: dict | None = None
    project_constraints: list[str] | None = None
    is_cancelled: Callable | None = None
```

### 3.2 输出契约

所有 Agent 必须返回 JSON 结构（chat 方向文本内容嵌入 JSON）：

```python
@dataclass
class AgentOutput:
    agent_name: str                  # 本 Agent 标识
    decision: str                    # done | repair_needed | escalate | clarifying
    content: str                     # 文本输出（chat 方向主要信息）
    artifact: dict | None = None     # 产物（build 方向）
    suggestions: list[str] | None = None   # 建议的下一步
    raw_data: list[dict] | None = None     # 搜索结果等原始数据
    meta: dict | None = None         # {tokens, model_used, elapsed, qc_overall}
```

### 3.3 System Prompt 必须包含的强约束

每个 Agent 的 System Prompt 头部必须包含以下 6 条：

```
## 强约束
1. 你只做 [本 Agent 定位的一句话描述]。
2. 遇到以下情况必须输出 {"decision": "escalate", "reason": "..."}：
   - 用户要求超出本 Agent 能力范围
   - 涉及后端/数据库/服务器/运维/安全入侵
   - 不确定或歧义很大且涉及高风险操作
3. 遇到需要澄清但不属于高风险的情况，输出 {"decision": "clarifying", "questions": [...]}
4. 所有输出必须是 JSON 对象（聊天文本在 content 字段中），不得输出裸文本。
5. 如果生成代码，必须包含完整的可运行 HTML/CSS/JS，内联所有资源。
6. 不得输出任何违反中国法律或公序良俗的内容。
```

### 3.4 灰��地带求助路径

```
Agent 遇到不确定
  → decision="escalate" + reason="我需要 [具体能力] 才能完成 [具体任务]"
  → Router 收到 escalate 后：
      if reason 匹配另一个 Agent 的 intent_tags：
        路由到那个 Agent
      else：
        返回给用户："我需要 [能力]，但我目前只做 [本 Agent 定位]。要帮你转到 [建议 Agent] 吗？"
```

---

## 四、各 Agent 详细规格

### 4.1 `agent_chat`
- **定位**：问答/解释/闲聊，纯文本输出，不做任何代码生成或操作
- **触发意图**：learn/casual/explain/debug(不涉及代码)/compare/text
- **子场景切换**（内部 System Prompt 按 level2 变化）：
  - `explain` →"你正在解释技术概念"
  - `casual` →"你正在闲聊，轻松自然"
  - `compare` →"你正在对比技术方案"
  - `text` →"你正在翻译文本"
- **出参示例**：
```json
{
  "agent_name": "agent_chat",
  "decision": "done",
  "content": "CSS 是层叠样式表，用于控制网页的外观...",
  "meta": {"tokens": 156, "model_used": "deepseek", "elapsed": 2.3}
}
```

### 4.2 `agent_search`
- **定位**：联网搜索 + RAG 向量检索，提供参考信息
- **能力**：调用 `web_search` tool + `rag_retrieve` tool
- **注意**：不参与生成，只听命检索和返回
- **触发意图**：learn/search
- **出参示例**：
```json
{
  "agent_name": "agent_search",
  "decision": "done",
  "content": "找到 5 条相关结果...",
  "raw_data": [
    {"source": "MDN", "title": "CSS Grid", "url": "https://...", "snippet": "..."}
  ],
  "meta": {"tokens": 80, "elapsed": 3.1}
}
```

### 4.3 `agent_design`
- **定位**：纯设计咨询——配色/布局/字体/动效建议
- **不做**：不写完整代码，只出设计建议和代码片段示例
- **触发意图**：learn/design
- **出参示例**：
```json
{
  "agent_name": "agent_design",
  "decision": "done",
  "content": "推荐深蓝色主色调 #1e3a5f...",
  "suggestions": ["试试金色 #d4a574 作为强调色", "字体推荐 Inter + 系统衬线"],
  "artifact": {"css_sample": "body { background: #1e3a5f; ... }"}
}
```

### 4.4 `agent_doc`
- **定位**：技术文档/README/API 文档生成
- **触发意图**：doc/readme/tutorial
- **出参示例**：
```json
{
  "agent_name": "agent_doc",
  "decision": "done",
  "content": "# 项目文档\n## 技术栈\n- Vue 3 + Vite...",
  "meta": {"format": "markdown", "length": 1200}
}
```

### 4.5 `agent_requirement`
- **定位**：结构化需求分析→产出 requirement_doc JSON
- **不做**：不涉及设计建议（交给 agent_design）、不写代码
- **触发意图**：build/requirement
- **出参示例**：
```json
{
  "agent_name": "agent_requirement",
  "decision": "done",
  "content": "需求分析完成",
  "artifact": {
    "type": "requirement_doc",
    "title": "企业官网",
    "industry": "corporate",
    "pages": [{"title":"首页","sections":["Banner","服务介绍","客户案例"]}],
    "features": ["响应式","SEO","暗色模式"]
  },
  "suggestions": ["风格方面可以咨询设计 agent", "确认后我可以帮你生成网站"]
}
```

### 4.6 `agent_build`
- **定位**：建站全流程——接收 requirement_doc，产出完整网站
- **内部阶段**：Planner→Coder→Reviewer→Reflexion（4 阶段，保留现有逻辑）
- **额外能力**：可以修改/增强/重构已有代码
- **触发意图**：build/site | build/page | code/modify | code/refactor
- **出参示例**：
```json
{
  "agent_name": "agent_build",
  "decision": "done",
  "content": "网站生成完成，已部署预览",
  "artifact": {"url": "https://...", "html_length": 8456},
  "meta": {"tokens": 3420, "elapsed": 45.2, "qc_overall": 8.2}
}
```

### 4.7 `agent_review`
- **定位**：代码评审+Bug修复——发现问题和修复一体完成
- **流程**：评审→发现问题→给出修复建议+修复后代码
- **不做**：不从零生成代码（那是 agent_build 的事）
- **触发意图**：code/review | code/fix
- **出参示例**：
```json
{
  "agent_name": "agent_review",
  "decision": "done",
  "content": "发现 3 个问题，已修复",
  "artifact": {
    "issues": [
      {"severity": "high", "desc": "导航颜色对比度不足", "fix": "改为 #1a252f"},
      {"severity": "medium", "desc": "缺少 meta viewport", "fix": "已添加"}
    ],
    "fixed_code": "<html>...修复后完整代码...</html>"
  },
  "meta": {"issues_count": 3, "fixed_count": 3}
}
```

### 4.8 `agent_generate_site`
- **定位**：保留现有 generate_site 内部逻辑（Planner→Coder→Reviewer→Reflexion+阶段自评+repair）
- **与其他 agent 的关系**：Router 将 build/site 意图路由到此 agent，它内部调度上述 4 阶段
- **不变**：SSE 事件流、QC 触发、git 提交、COS 投递

---

## 五、命名规范与文件结构

```
skills/
├── agent_chat.py             # (原 explain.py)
├── agent_search.py           # (原 search_agent.py)
├── agent_design.py           # (原 design_agent.py)
├── agent_doc.py              # (原 generate_doc.py)
├── agent_requirement.py      # (原 requirement_agent.py)
├── agent_build.py            # (合并 builder_agent + write_code)
├── agent_review.py           # (合并 review_agent + fix_agent)
├── agent_generate_site.py    # (原 generate_site.py，保留核心逻辑)
└── __init__.py               # 导入所有 agent
```

**删除**：`builder_agent.py`, `write_code.py`, `fix_agent.py`, `review_agent.py`, `explain.py`, `design_agent.py`, `search_agent.py`, `generate_doc.py`, `rag_retrieve_skill.py`, `requirement_agent.py`, `generate_site.py`

**降级**：`rag_retrieve_skill.py` → `tools/rag_retrieve.py`（已是 tool）

## 六、Router 路由表（精简后）

| 意图 (level1/level2) | 路由到 | 说明 |
|----------------------|--------|------|
| learn/casual | `agent_chat` | 闲聊 |
| learn/explain | `agent_chat` | 解释 |
| learn/compare | `agent_chat` | 对比 |
| learn/search | `agent_search` | 搜索 |
| learn/design | `agent_design` | 设计咨询 |
| learn/translate | `agent_chat`(text) | 翻译 |
| doc/* | `agent_doc` | 文档生成 |
| build/requirement | `agent_requirement` | 需求分析 |
| build/site | `agent_build` 或 `agent_generate_site` | 建站 |
| build/page | `agent_build` | 单页 |
| code/snippet | `agent_build` | 代码生成 |
| code/modify | `agent_build` | 修改 |
| code/fix | `agent_review` | 修复 |
| code/review | `agent_review` | 评审 |
| code/refactor | `agent_build` | 重构 |

---

## 七、实施步骤

1. **创建 `AgentInput` / `AgentOutput` dataclass**（`core/models.py` 新增）
2. **逐 Agent 改造**（从简单到复杂）：
   - 先 agent_chat（最常用，风险最低）
   - agent_search / agent_design / agent_doc
   - agent_requirement
   - agent_review（合并 fix_agent + review_agent）
   - agent_build（合并 builder_agent + write_code，最大改动）
   - agent_generate_site（保留现有，改注册名）
3. **更新 Router**：`detect_intent_v2` 的路由映射表更新
4. **更新注册表**：`skills/__init__.py` 导入新 agent
5. **验证**：跑 `scripts/run_tests.py --quick`
6. **清理**：删除旧文件
