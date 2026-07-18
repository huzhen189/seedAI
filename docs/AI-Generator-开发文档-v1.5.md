# AI 生成器平台 — 开发文档 & 架构方案 (v1.5)

> 版本:v1.17(本地 Skills/Tools 落地:新增 §5.10 把真实的 Skill/Tool 落到 `backend/ai_service/app/`,严格对齐 §5.8/§5.9 注册表契约,引入成熟开源库 + 成熟 agent 模式实现,注册表从空变可用;目录 `registry/`(SkillRegistry/ToolRegistry)/ `tools/`(9 内置工具)/ `skills/`(5 Skill)/ `router.py`(意图分发)/ `registries.py`(bootstrap+目录扫描热插拔);v1.16 内容:§5.9 Tool 来源机制 / 前端栈 Vue3+Vite / SkillRegistry/记忆闭环/关键词/RAG 命中率/预览线上直连/产物版本确认+评价1–10 回归/指标回放/矛盾收口/自动建表/三库连接池/本地三域名/复审 §14 + Hybrid/RBAC 三级/SSE+WebLLM+优化 1-C~5-C 已定)
> 最后更新:2026-07-18
> 一句话定位:一个通过 AI 对话即可生成**网站/前端页面**(后续扩展文档、代码)的平台;微服务架构(业务服务 + 核心 AI 服务),支持多模型自选、多 agent 编排、用户系统、RAG 增强、管理监控后台(权限/实时/控制/统计),可自托管、可横向扩容。

> 变更史:
> - v0.2:定位升级为可自托管/可扩容;定多模型、MySQL+Chroma+Redis、Next.js 前端 + 独立后端。
> - v0.3:后端定 FastAPI (Python),模型抽象层改 Python 实现。
> - v0.4:新增多 Agent 编排层(Supervisor + LangGraph)。
> - v0.5:新增用户系统(账号密码 + 短信预留);plan/tier 配额抽象与订阅表预留位。
> - v0.6:短信与收费改为「先预留、后期再加」,MVP 只做账号密码。
> - v0.7:微服务拆分(业务服务 + 核心 AI 服务),内网调用,模型 Key 仅存 AI 服务。
> - v0.8:**引入缓存与异步持久化** —— 活跃用户常用数据 Cache-Aside 进 Redis(30min TTL);写先同步更 Redis 再异步落 MySQL;失败进错误队列,定时检查器重试对账。
> - v0.9:**管理监控后台** —— 带 RBAC 权限的管理页,实时监控各服务状态/性能/接口/访问量/模型用量,支持手动启停与扩缩容,配套统计系统。
> - v1.0:**§12 全部开放问题拍板** —— ORM=SQLAlchemy(async);JWT=HttpOnly Cookie+15min Access+Refresh 存 Redis;缓存写=写后更新 Redis;检查器=业务服务内后台任务;Embedding=Qwen text-embedding(M1);视觉=浅色简洁;限流=每日配额(free 50 次/天);提示词=通用中性。文档进入「可编码」状态。
> - v1.1:**WebSocket 传输 + Orchestrator 思考链路 + 日志管理(已定)** —— 前端↔业务改 WebSocket(每问一连接,结束断连);AI 核心由 Orchestrator 编排多 agent 并产出"思考过程"事件;后端结构化日志(trace_id)+ 思考链路面板;#12=MVP 不落库、#13=WebSocket 端到端。文档进入「可编码」。
> - v1.2:**SSE 回退 + WebLLM 客户端首过 + 优化方案采纳** —— 实时传输由 WebSocket **回退为 SSE**(每问一连接,结束断连,§3.7);前端引入 **WebLLM 浏览器内首过**(§3.9);采纳优化 **1-C**(Redis 队列+Worker 池,提前)/**2-C**(跨模型降级路由,子决策已确认)/**3-C**(静态分析+LLM 自审)/**4-C**(独立沙箱域+严格 CSP)/**5-C**(语义相似缓存,随 M1 向量库)。文档进入「可编码」。
> - v1.3:**RBAC 三级权限细化** —— `role` 由 admin/user 两级扩为 **super_admin / admin / user**(§3.6 权限矩阵 + §4.3 角色初始化):super_admin=全部(含控制面执行+用户角色管理)、admin=仅查看后台、user=普通用户;控制面由 admin-only 收紧为 super_admin-only;初始 super_admin 经种子/环境变量注入。
> - v1.4:**编排架构改 Hybrid** —— 弃纯多 Agent,改 **Router+Skill 外层分发(意图/权限/配额闸门)+ generate_site 内层轻量多 Agent(Planner→Coder→Reviewer,共享上下文、Designer 降为 style 约束)**;其余 skill(写代码/文档/解释/RAG)单次 LLM 直出。成本更优,思考可视化与回退机制保留(§5)。新增 §5.6 用户对话端到端详细流程图。
> - v1.5:**架构演进候选(§5.7)** —— 梳理 ReAct / Plan&Execute / Graph(DAG) / Debate / Reflexion 五种形态,明确 **内层 generate_site 演进为 Plan-and-Execute(先计划再执行)+ Reflexion(失败→反思→重写)叠加**,外层 Router+Skill 不变。
> - v1.6:**预览/体验/取消/账本/密钥复审(§14)** —— 采纳 **A2(腾讯云 COS 对象存储投递,替代 srcdoc,预览域 `seedhtml.huzhen.net.cn` 作自定义 CDN 域名直链)/ B1(WebLLM 单固定小模型 + 首屏预取缓存)/ C1(SSE 客户端 abort + 服务端级联取消省 token)/ D1(多租户 key 账本:次数配额 + 全局限频 + 成本账本)/ E1(生产密钥 Docker Secrets 注入)**;文档进入「可编码」。
> - v1.7:**运行时自动建表 + 三库连接池**:MySQL 启动 `create_all`(幂等自检,无表则建,不删改已有)/ Chroma `get_or_create_collection` / Redis `ping` 探活;MySQL(SQLAlchemy async 连接池)/ Redis(asyncio ConnectionPool)/ Chroma(单例 HttpClient + httpx Limits)三库均单例连接池复用。文档进入「可编码」。
> - v1.8:**本地三域名 + hosts 配置**:新增 **§3.11 本地开发域名与 hosts 配置** —— 三域名 `seedai.huzhen.net.cn`(前端主站)/ `seedapi.huzhen.net.cn`(后端 API)/ `seedhtml.huzhen.net.cn`(预览规划域,实际走 COS 默认域)均映射 `127.0.0.1`,本机 `C:\Windows\System32\drivers\etc\hosts` 已写入(2026-07-18 先加 seedai/seedhtml,本版追加 seedapi);给出 `next.config.js` 代理改 `seedapi`、CORS/`cookie_domain` 配合项。
> - v1.9:**收口文档矛盾 + M0 决策拍板** —— (1)修 §9.1 `Artifact` 补 `preview_url(可空)` 字段(对齐 §14.1);(2)§10 预览主路径改为 **COS 直链(A2)**,`iframe srcdoc` 降为离线/兜底;(3)§9.1 列名统一 **snake_case**(`project_id`/`user_id`/`model_id`/`created_at`);(4)决策表新增 **#30 M0 匿名跑通**(不带登录,User/quota/UsageLog 延到 M1)/ **#31 默认降级序 HY3→Qwen→DeepSeek** / **#32 COS 预览不打对象 CSP 响应头,仅靠 iframe `sandbox` 隔离**。文档进入「可编码」。
> - v1.10:**可观测性增强(指标 + 回放)** —— 新增 **§3.12 多维度运营指标**(6 大类:AI 效率 / 准确性 / 消耗量 / 回滚率 / 用户回馈 / 健康度;数据源 Trace/UsageLog/Feedback + Redis,`/admin` 看板呈现);**§3.13 对话追踪与回放**(每次会话落 `Trace` + `TraceEvent` + `Feedback` 三表,可还原"用户说 / AI 想 / AI 做 / 操作"全链路);§9.1 补 `Trace`/`TraceEvent`/`Feedback`;§3.8 日志归集由"MVP 不落库"改为"MVP 起即落 MySQL 可回放";决策表 **#33 指标维度已定义** / **#34 回放存储=MySQL 双表(推荐,非文件 / 非 OTel)** / **#35 产物版本方案=推荐方案1 线性递增(回滚即复制为新版本,待用户最终确认)**。
> - v1.11:**产物版本方案1 确认 + 对话评价 1–10 分(统计 + 回归)** —— (1)#35 产物版本方案1 **正式确认**(线性递增,回滚即复制为新版本,历史不丢);(2)`Feedback.rating` 范围由 1–5 **改为 1–10**,评分时机=每轮对话(Trace)结束后对话页提供 1–10 分 + 点赞/点踩 + 可选评论;(3)**反馈数据双重用途**——既进 §3.12 用户回馈统计,又经 `trace_id` 关联 `Trace`/`TraceEvent`/`Artifact` 形成「(用户输入 + 生成产物 + 思考链 + 操作)→ 评分」三元组,可一键导出为**模型回归/评测数据集**(按 rating 分层筛选高分/低分样本),支撑日后模型迭代回归测试;(4)§9.1 `Feedback` 表定义同步、§3.12 新增「回归数据集」说明。文档进入「可编码」。
> - v1.12:**RAG 检索质量指标 + 预览域线上直连**:(1)#37 **RAG 命中率采用方案2**——§3.12 新增第7类「RAG 检索质量」:检索命中率(返回结果最高相似度 ≥ 阈值 0.7 的 rag_retrieve 占比)、无结果率、上下文采纳率(可选,Planner/Reviewer 标记采纳);数据源 `TraceEvent(rag_retrieve)`,`/admin`「AI 质量」标签页呈现;(2)**预览域改线上 COS 默认域直连、不配本地 host 代理**——§3.11 hosts 代码块对齐为两行(seedai/seedapi),预览沙箱行改「COS 默认域 `seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com` 线上直接访问、不代理本地」;本机 hosts 实际仅两行(无 seedhtml 行),与要求一致。文档进入「可编码」。
> - v1.13:**记忆系统 + Skill/Agent 引入机制 + 关键词来源** —— (1)新增 **§5.8 SkillRegistry 注册表**:Router 按 `intent_tags` 从注册表匹配 Skill;新 Skill=注册 handler(开闭原则,不动 Router 核心),新多 Agent=注册 `is_graph=True` 的 LangGraph app(封装在 Skill 内);(2)新增 **§7.1 多层记忆闭环**(短期 `Trace` / 长期 Chroma `memory` / 组件 `components` / 语义缓存 `cache:gen` / 反馈 `Feedback`),含**检索闭环流程图**(读前增强→写后沉淀→自我改进飞轮);(3)新增 **§7.2 关键词来源**:采用混合检索(稠密 embedding + 关键词/metadata 过滤),关键词主要来自 **A 原始 prompt + B Planner 结构化 spec**,辅以 **C Router 意图 scope** 与 **D userId/projectId 过滤**;(4)决策表 **#38 Skill 引入=SkillRegistry 注册表(开闭原则)** / **#39 记忆系统=多层闭环 + 关键词=B+A 主、C/D 辅助**。文档进入「可编码」。
> - v1.14:**清理 WebSocket 残留描述**:§3.4「服务间通信」原误写为「前端↔业务 = WebSocket」,本版对齐 **§3.7(前端实时流 = SSE)** 与 **#13(业务↔AI = SSE 透传)**,改为「前端↔业务 = SSE(每问一连接)」+「业务→AI = SSE 透传」,并显式标注 **全链路不使用 WebSocket**;历史变更记录(v1.1/v1.2)保留 WebSocket→SSE 回退留痕。文档进入「可编码」。
> - v1.15:**前端技术栈改为 Vue 3 + Vite + TS**:因前端与 business-api 已 **REST/SSE 解耦、框架无关**,用户确认前端用 **Vue 3 + Vite + TS**(纯静态 SPA;状态 Pinia、UI 库 Naive UI / Element Plus、浅色主题),替代原 Next.js;相应 **§3.11 代理由 `next.config.js` 改为 `vite.config.ts` 的 `server.proxy`**(`/api` → `http://seedapi.huzhen.net.cn:8000`,且 dev `server.port=3000` 对齐 hosts/CORS)、**管理后台由 Next.js 路由组改为 Vue `/admin` 路由**、**docker-compose `frontend` 镜像改为 Vite 构建静态产物(+ 静态服务器)**;后端契约(OpenAPI/SSE)不变。决策表 #41。文档进入「可编码」。

---

> - v1.16:**Tool 来源机制**:新增 **§5.9 ToolRegistry 注册表 + 开闭原则**,澄清 **Skill(完整任务能力,§5.8 经 Router 分发)** 与 **Tool(内部 agent 经 function calling 调用的原子操作)** 区别;Tool 来源 = 内置 `@tool` 注册(如 `rag_retrieve`/`cos_upload`/`web_search`/`code_run`,MVP 全走这层)/ M2+ `tools/` 目录扫描运营用户贡献 / M3+ 第三方 MCP;明确 `rag_retrieve` **双入口**(对外 Skill + 对内 Tool,同一实现);与 SkillRegistry 同风格(注册表 + 开闭原则 + M2+ 目录扫描)。文档进入「可编码」。
> - v1.17:**本地 Skills/Tools 落地(§5.10,决策 #43)**:把 §5.8/§5.9 的注册表契约落到 `backend/ai_service/app/` 真代码——`registry/`(SkillRegistry+ToolRegistry+`@tool` 装饰器)、`tools/`(9 个内置工具:rag_retrieve/web_search/fetch_url/cos_upload/browser_screenshot/image_generate/html_validate/file_io)、`skills/`(5 个 Skill:generate_site[LangGraph 多 agent]/write_code/generate_doc/explain/rag_retrieve[双入口])、`router.py`(detect_intent+dispatch)、`registries.py`(bootstrap 全量注册 + 目录扫描热插拔);工具均用成熟库(httpx/BeautifulSoup4/Chroma+Qwen embedding/cos-python-sdk-v5/Playwright/标准库),重依赖函数内懒加载、缺包优雅报错不阻断注册;新增 AI 服务调试端点 `/skills` `/tools` `/registry`;实测 bootstrap 注册 5 Skill + 9 Tool,graceful degradation 验证通过。文档进入「可编码」。

## 1. 项目概述

### 1.1 产品定位
- **形态**:类 v0.dev / bolt.new / Lovable 的"对话即生成"平台。
- **使用范围**:自己为主 + 小范围分享;用户系统支持账号密码(短信/收费预留);架构预留收费扩展点。
- **首要产物**:网站 / 前端页面(带实时预览、可分享、可迭代)。
- **后续扩展**:文档生成、代码工程生成(架构预留,不在 MVP)。

### 1.2 核心价值
自然语言描述需求 → 业务服务编排 → 核心 AI 服务(多 agent + RAG)生成可运行网页 → 实时预览 → 对话迭代 → 分享/导出。

---

## 2. 核心功能范围

### 2.1 MVP(第一阶段必须有)
| 模块 | 归属服务 |
|------|---------|
| 用户系统(账号密码) | 业务服务 |
| 对话生成(流式) | 业务服务入口 → AI 服务生成 |
| 多模型自选 | 业务服务透传 modelId → AI 服务 |
| 多 Agent 编排 | 核心 AI 服务 |
| 实时预览 | 前端(iframe / 沙箱) |
| AI 思考过程可视化 | 前端(思考链路面板,SSE 实时展示 Router/Skill 思考链路) |
| 迭代修改 | 业务服务 + AI 服务 |
| RAG 增强 | 核心 AI 服务 |
| 用量限流(plan/tier) | 业务服务 |
| 分享链接 | 业务服务 |

### 2.2 第二阶段(增强)
版本历史与回滚 / 代码在线编辑 / 导出 zip / 一键部署 / 模板库运营 / 收费模式接入

### 2.3 暂不做(已明确后置)
- **手机短信验证码登录**:仅保留 `SMSService` 抽象层与 Redis 验证码结构;开放时再接真实服务商。
- **完整订阅计费与支付**:只做 plan/tier 配额抽象与表预留位;未来接微信/支付宝/Stripe 只增表、改配额取数一处。
- 复杂后端逻辑生成 / 团队协作权限体系

---

## 3. 技术选型

| 层 | 选型 | 说明 |
|----|------|------|
| 前端 | **Vue 3 + Vite + TypeScript**(纯静态 SPA) | 对话 UI、预览面板、模型选择器、登录页、`/admin` 管理台 |
| 前端 UI | Tailwind CSS + shadcn/ui | 快速搭建、风格统一 |
| 业务服务 | **FastAPI (Python)** | 鉴权/JWT、用户、项目、消息、分享、配额;唯一对外入口 |
| 核心 AI 服务 | **FastAPI (Python)** | 模型抽象层、Router+Skill 路由、generate_site 内层 LangGraph 轻量多 Agent、RAG;独享模型 Key |
| Agent 编排 | **Router+Skill(外层) + LangGraph(内层)** | 外层 Router 意图分发/鉴权/配额;内层仅 generate_site 走 LangGraph 状态图 + 条件边(评审回退);其余 skill 单次 LLM |
| 认证 | **JWT(Access) + Refresh 存 Redis**;密码 bcrypt/argon2 | 无状态令牌利于横向扩容 |
| 短信 | **SMSService 抽象层(预留,先不接)** | 同模型抽象层思路;开放时再接真实服务商 |
| AI 编排 | OpenAI SDK(Python)/自封装流式 | 统一流式输出(async generator) |
| 模型 | **DeepSeek + Qwen + HY3(前端可选,可扩展)** | 模型抽象层,见 §6 |
| 关系库 | **MySQL** | 业务数据(用户/项目/对话/产物),归业务服务 |
| 向量库 | **Chroma** | 组件/记忆检索,归 AI 服务 |
| 缓存/队列 | **Redis** | 会话/限流(业务)、队列/缓存(AI) |
| ORM | **SQLAlchemy(async)** — Python 原生,与 FastAPI/AI 服务一致 | ✅ v1.0 已定 |
| 可观测性 | **自研轻量 /metrics + Stats 聚合 + SSE**(MVP);Prometheus+Grafana(可选升级) | 见 §3.6 |
| 控制面 | **Orchestrator 抽象**:DockerCompose(MVP) / K8s(生产) | 启停/扩缩容,见 §3.6 |
| 部署 | **Docker Compose(本地) → 多服务器扩容** | 见 §8 |

> **前后端跨语言说明**:前端 TS、后端 Python(双服务),通过 HTTP/SSE 解耦。接口契约以 **OpenAPI(Swagger)** 为准;本地 Vite dev 配 `server.proxy` 将 `/api/*` 代理到业务服务(`http://seedapi.huzhen.net.cn:8000`),业务服务再内网调用 AI 服务(`localhost:8001`)。模型 Key 与短信 Key 只在 AI 服务/配置内,前端与业务服务均不接触。

### 3.4 服务拆分(微服务架构)

将后端拆为两个独立部署的服务,职责清晰、可独立扩缩容:

```
┌──────────────────────────────────────────────────────────────┐
│  前端层  Vue 3 (TS)   对话 UI · 预览 · 登录 · 模型选择器 · /admin    │
└───────────────────────────────┬──────────────────────────────┘
                                 │ 对外 HTTPS
┌───────────────────────────────▼──────────────────────────────┐
│  业务需求服务器 (Business Service)  ← 唯一对外入口               │
│  鉴权/JWT · 用户 · 项目 · 消息 · 分享 · 配额限流 · 用量计量       │
│  MySQL(业务数据) · Redis(会话/限流)                             │
└───────────────────────────────┬──────────────────────────────┘
              内网 HTTP/SSE(同步转发流式) │
┌───────────────────────────────▼──────────────────────────────┐
│  核心 AI 服务器 (AI Service)  ← 仅内网,持有模型 Key             │
│  模型抽象层(Provider) · Router+Skill · LangGraph(generate_site) · RAG  │
│  Chroma(向量) · Redis(队列/缓存) · → DeepSeek/Qwen/HY3          │
└───────────────────────────────────────────────────────────────┘
```

**关键设计**
- **业务需求服务器**:唯一对外暴露的入口。负责鉴权、用户、项目管理、对话消息、分享链接、配额限流、用量计量。**不持有模型 API Key**。生成类请求转发给 AI 服务。
- **核心 AI 服务器**:仅内网可达(网络隔离 / 不暴露公网)。负责模型抽象层(Provider 注册表)、Router+Skill 路由、generate_site 内层 LangGraph 轻量多 Agent、RAG(Chroma)、流式生成。**独享模型 API Key**。
- **服务间通信(MVP)**:前端 ↔ 业务服务 = **SSE(每问一连接,结束断连)**(§3.7);业务服务 → AI 服务 = **SSE 透传**(业务 `httpx` 流式读 AI `/generate` SSE 事件原样转发,见 §12 #13)。后续可加 Redis 队列做异步生成(与 M2 队列一致)。**全链路不使用 WebSocket**(前端实时生成流一律走 SSE)。
- **内部信任**:AI 服务不重复校验终端用户,信任来自业务服务的内网调用(可加内部 service token 或纯网络隔离);**前端永不可直达 AI 服务**。
- **数据与 Redis 归属**:MySQL 归业务服务;Chroma 归 AI 服务;Redis 可共享实例(业务用会话/限流,AI 用队列/缓存),后期可按服务拆分实例。

### 3.5 缓存与异步持久化(Cache-Aside + Write-Behind)

目标:活跃用户高频读取的数据不每次打 MySQL;写入解耦、失败可重试,保证最终一致。归属:**业务服务**(它持有 MySQL + Redis)。

**读(Cache-Aside / 懒加载)**
- 活跃用户常用数据(用户资料、plan/quota、最近项目列表、用量计数等)以 `cache:user:<id>:*` 存 Redis,**TTL 30 分钟**,用户有活动则续期。
- 读:先查 Redis,命中即返;未命中(miss)回源 MySQL,写入 Redis 后返回。

**写(Write-Behind / 异步)**
- 写:先**同步更新 Redis**(保证后续读一致,避免最长 30min 陈旧),再把写操作投入**异步写队列**(Redis),由 Worker 落 MySQL。
- 失败:Worker 落库失败 → 进**错误队列**(Redis),附重试次数与错误原因。

**定时对账(Reconciler)**
- **定时检查器**(调度任务,见 §8)周期性扫描错误队列,按退避策略重试;超过最大重试次数 → 死信(dead-letter)+ 告警,交人工介入,不阻塞主流程。

**关键取舍(✅ v1.0 已定)**
- **同步写**:注册 / 改密 / 订阅付费(用户强一致,失败直接报错)。**异步写**:高频追加 Message / UsageLog / Artifact / memory 回写(经 `queue:write`,Worker 落库)。
- **写成功后对 Redis 取「更新」**(回填 `cache:user:*`,读侧永远最新,避免最长 30min 陈旧)。
- **定时检查器(MVP)= 业务服务内后台任务**(asyncio 后台循环扫 `queue:error` 重试);M2 可抽独立 Worker。重试策略:指数退避,最多 5 次,超限进 `queue:error:dlq` + 告警。

### 3.6 管理监控后台(Admin & Observability)

需求:运营者需要一个**带权限的管理页面**,实时查看各服务状态/性能/接口/访问量/模型用量,并能**手动停止或扩容**实例,配套统计系统。归属:监控读路径在业务服务(它聚合指标),控制面也在业务服务;前端为 Vue 内的 `/admin` 路由。

**权限(RBAC — 三级)**
`User.role` 取值 `super_admin` / `admin` / `user`(默认 `user`)。三档权限边界:

| 能力 | super_admin | admin | user |
|------|:---:|:---:|:---:|
| 前台对话 / 生成 / 个人项目 | ✅ | ✅ | ✅ |
| 管理页只读(指标 / 统计 / 日志 / 思考链路) | ✅ | ✅ | ❌ |
| 控制面执行(手动启停 / 扩缩容) | ✅ | ❌ | ❌ |
| 用户与角色管理(分配 role / 改 plan / 改 quota) | ✅ | ❌ | ❌ |

- 鉴权依赖按 role 分层校验(见 §4.2):`require_admin`(super_admin **或** admin,开放只读后台)、`require_super_admin`(仅 super_admin,开放控制面与用户管理)。
- 管理页作为 Vue 内的 `/admin` 路由,与用户前台共享登录态、彼此隔离;admin 进入后控制面板置灰/隐藏,仅 super_admin 可见可执行。

**实时监控(指标采集 + 实时推送)**
- 各服务暴露 `/metrics`(健康、CPU/内存、QPS、接口延迟/错误率、模型调用次数)。MVP 用自研轻量:FastAPI 中间件把接口计数写 Redis,进程指标用 `psutil`;聚合进 Stats 模块。
- 实时推送:管理页通过 **SSE** 订阅指标流(与生成流同技术),轮询作兜底。
- 模型用量直接来自 `UsageLog`(按 modelId 统计 tokens / 次数 / 估算成本)——已设计,天然可统计。
- ✅ 指标栈取舍:**已定 MVP 自研轻量**(各服务 /metrics + Redis 计数 + Stats 聚合 + SSE);后期可平滑接 Prometheus+Grafana 做深度可视化。

**控制面(手动启停 / 扩缩容)**
- 抽象 `Orchestrator`:`stop(instance)` / `scale(service, replicas)`。MVP 实现 `DockerComposeOrchestrator`(本地封装 `docker compose` 命令,操作面最小化);后期 `K8sOrchestrator` 对接集群 API。
- 控制 API 仅 `super_admin` 可调用(admin 仅能查看、无执行权),动作需二次确认,避免误操作。
- ✅ 控制面落地:**已定先 docker-compose 封装**(MVP,super_admin-only + 二次确认);多服务器时换 `K8sOrchestrator`,接口不变。

**统计系统**
- Stats 模块聚合:实时计数(Redis)+ `UsageLog`(模型/用量)+ 周期性快照,产出仪表盘(访问量、接口性能、模型分布、成本估算)。
- 汇总可存 MySQL 时序表(轻量)或 Prometheus TSDB(若采用)。**AI 生成质量维度的细分指标(效率 / 准确性 / 消耗量 / 回滚率 / 用户回馈 / 健康度)见 §3.12,数据源为 Trace / UsageLog / Feedback(§9.1)。**

---

### 3.7 实时传输:SSE(每问一连接,结束断连)

前端与业务服务之间的实时生成流采用 **SSE(Server-Sent Events)**——单向服务器推送,天然适配「思考过程 + 产物」的事件流,且比 WebSocket 更轻、在 FastAPI/`sse-starlette` 中更易实现:

- **连接生命周期**:用户每次发起提问时,前端新建一个 `EventSource` 连到业务服务 `GET /api/chat?projectId=...`(带 HttpOnly Cookie 自动鉴权);本次提问(含多 agent 协作与最终产物)推送完毕后,业务服务发 `done` 事件并**结束响应**(连接随之关闭);用户中断时前端 `eventSource.close()` 主动关闭。
- **鉴权**:复用 HttpOnly Cookie 中的 JWT(同源自动带),业务服务在处理请求前校验,非法返回 401。
- **事件协议**:SSE `event:` 字段区分类型,`data:` 为 JSON:
  - `token` —— Coder 产出的 HTML 字符流(最终产物,进预览)
  - `think` —— 各 agent 的思考/推理文本(Planner 规格、Orchestrator 决策、Reviewer 结论)
  - `node`  —— agent 节点进入/离开(前端高亮当前阶段)
  - `error` —— 错误(随后结束)
  - `done`  —— 结束(随后断连)
- **业务↔AI 服务**:见 §12 决策 #13(业务侧用 `httpx` 流式读取 AI 服务 `/generate` 的 SSE 并透传事件;AI 服务内部经 Redis 队列 + Worker 产出进度,见 §3.9 与 1-C)。

### 3.8 日志与思考链路管理

对应需求"做好日志管理 + 前端输出",在 §3.6 运行指标之外新增应用日志与思考链路:

- **结构化日志**:后端统一结构化日志(JSON),每条带 `trace_id`(= 本次 SSE 会话 id)。记录:请求进出、agent 节点进入/离开、模型调用时延与 token 用量、错误堆栈。
- **思考链路(trace)**:Orchestrator 每步产出既写日志,也作为 `think`/`node` 事件推前端(§3.7),前端"思考过程"面板实时展示 AI 规划/推理/评审思路。
- **日志归集**:MVP 起思考链路与对话操作即落 MySQL(`Trace`/`TraceEvent`,§3.13),管理页(§3.6 `/admin`)可历史回放;最近实时仍经 Redis 环形队列(`logs:recent`)供实时看板。原 `TraceLog` 表方案废弃,统一为 §3.13 的 Trace/TraceEvent 模型。
- **前端输出**:对话页三区 —— 对话气泡、思考过程面板(实时 agent 思路)、产物预览(iframe);思考过程随 SSE 事件滚动更新。

### 3.9 前端 WebLLM 浏览器内首过(客户端优先)

目标:利用用户本机算力(WebGPU)在浏览器内跑一个小模型,对**简单需求做"首过"生成 / 规划**,从而**降低云端 token 消耗、缩短首字延迟、增强隐私**(需求不离开浏览器)。云端 Orchestrator 仍是主力,WebLLM 是其**本地加速 / 隐私 / 降成本**的补充通道。

- **选型**:浏览器端 LLM 运行时用 **@mlc-ai/web-llm**(WebAssembly + WebGPU,支持 Llama-3-8B-Instruct / Qwen2.5-7B-Instruct 等 GGUF 量化模型)。模型首次使用需从 CDN 下载权重(~2–5GB),后续走浏览器 Cache Storage 缓存;仅 Chrome/Edge 等支持 WebGPU 的浏览器可用。
- **在生成流中的定位(MVP 用 A)**:
  - **A. 本地首过 + 可选云端精修(推荐 MVP)**:模型选择器新增「本地 WebLLM(快速)」。选中后,前端用 WebLLM **本地**完成 Planner 规划 +（简单站点）Coder 生成,全程本地、用与云端**相同的 `think`/`token`/`node` 事件帧**在本地产出(前端自起一个本地事件源),产物直接进 iframe 预览;仅当用户选择云端模型、或本地生成失败 / 超时 / 不支持 WebGPU,才回退到业务服务 → AI 服务 SSE。
  - **B. WebLLM 仅做 Planner**:本地跑 Planner 产出结构化 spec,再把 spec 发给云端 Coder/Designer/Reviewer 精修。最省 token,但本地仍需下载模型、且简单站点也走云端。
- **事件一致性**:WebLLM 本地产出的 `think`/`node`/`token` 与云端 SSE 帧**完全同构**,前端"思考过程面板 + 预览"无需区分来源。
- **限制与边界**:WebLLM 仅能跑小模型,复杂 / 大页面质量不如云端;首下载慢、占本机内存;因此**云端 Orchestrator 不被替代**,WebLLM 仅作为首过通道。WebLLM 完全在前端,不触及业务 / AI 服务的模型 Key。
- **与架构关系**:云端路径不变(§3.7 SSE → 业务 → AI Orchestrator);WebLLM 是前端侧的旁路首过,失败即无缝回退云端。

### 3.10 资源层:连接池管理(MySQL / Redis / Chroma)
- **目标**:三个存储(MySQL、Redis、Chroma)均通过**单例 + 连接池**复用连接,避免每请求新建、失控耗资源;随服务生命周期创建与释放。
- **MySQL(SQLAlchemy async 连接池)**:
  - 建引擎:`create_async_engine(MYSQL_URL, pool_size=10, max_overflow=20, pool_pre_ping=True, pool_recycle=1800)`。
  - `pool_size`=常驻连接数;`max_overflow`=峰值额外;**`pool_pre_ping=True`**=取连接前探活防失效;`pool_recycle=1800`=30min 回收,规避 MySQL 8h 空闲断连。
  - 生命周期:服务 `lifespan` 启动建引擎,关闭时 `await engine.dispose()`。
- **Redis(redis.asyncio 连接池)**:
  - `pool = redis.asyncio.ConnectionPool.from_url(REDIS_URL, max_connections=50, decode_responses=True, health_check_interval=30)`;客户端 `redis.asyncio.Redis(connection_pool=pool)` 全局复用。
  - `max_connections` 上限防突发;`health_check_interval` 保活。
- **Chroma(单例 HttpClient + httpx 连接池)**:
  - Chroma 无传统连接池(底层 HTTP);复用**单例 `HttpClient`**,注入自定义 `httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))` 复用 TCP 连接。
  - 启动时 `client.get_or_create_collection(name=...)` 确保集合存在(§9.0 / §9.3);生产 Chroma 公网(1.12.219.195)需加认证/内网隔离(已提醒)。
- **统一约束**:三库客户端均在服务启动时初始化为模块级单例,关闭时释放;**禁止在请求处理函数内反复 `create_engine` / 新建连接**。

### 3.11 本地开发域名与 hosts 配置
- **三套域名(本地开发映射)**:
  | 用途 | 域名 | 本地指向 | 生产指向 |
  |---|---|---|---|
  | 前端主站 | `seedai.huzhen.net.cn` | `127.0.0.1`(本机 Vite dev :3000) | 真实服务器 / CDN / 静态托管 |
  | 后端 API | `seedapi.huzhen.net.cn` | `127.0.0.1`(本机 business-api :8000) | 真实服务器(公网入口) |
  | 预览沙箱 | COS 默认域 `seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com`(线上直接访问,**不配本地 host 代理**) | 不代理(走线上 COS) | 腾讯云 COS(已配) |
- **为什么要改 hosts**:本地无 DNS,用 `hosts` 把**前端主站 + 后端 API** 两域名指到 `127.0.0.1`,使本地联调时前端 / 后端用**与生产一致的域名**(CORS、`cookie_domain`、Cookie 作用域、跨域隔离逻辑都能按真实环境验证),避免 `localhost` 与域名混用导致的环境差异 bug。**预览域不在本机 hosts 代理**——预览产物存于线上腾讯云 COS,本地/线上均直接访问其真实域名 `seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com`,天然跨域隔离,无需本地解析。
- **hosts 配置(已写入本机 `C:\Windows\System32\drivers\etc\hosts`:2026-07-18 先加 seedai / seedhtml,本版追加 seedapi)**:
  ```
  # seedAI 本地开发域名(仅前端主站 + 后端 API 需本地解析;预览域走线上 COS 默认域,不在此列)
  127.0.0.1 seedai.huzhen.net.cn
  127.0.0.1 seedapi.huzhen.net.cn
  ```
- **操作命令(Windows,需管理员权限)**:
  - 编辑:`notepad C:\Windows\System32\drivers\etc\hosts`(或任意编辑器**以管理员运行**),追加上面三行。
  - 生效:保存即生效,无需重启;若浏览器仍解析旧值,执行 `ipconfig /flushdns` 清缓存。
  - 备份:改前先复制 `hosts.bak`,误改可还原(本机已存在 `hosts.bak.20260718`)。
- **验证**:`ping seedai.huzhen.net.cn` / `ping seedapi.huzhen.net.cn` 回 `127.0.0.1` 即正确;`seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com` 应解析到腾讯云(线上可直连)。
- **配合项**:
  - 前端 `vite.config.ts` 的 `server.proxy` 将 `/api` 代理到 `http://seedapi.huzhen.net.cn:8000`(不再用 localhost),且 dev `server.port = 3000` 对齐 hosts/CORS;与 `CORS_ORIGINS` 的 `http://seedai.huzhen.net.cn:3000` 完全一致,消除跨域源不一致。
  - `COOKIE_DOMAIN` 本地留空(浏览器按宿主 `seedai.huzhen.net.cn` 自动作用);`CORS_ORIGINS` 已含 `http://seedai.huzhen.net.cn:3000`。
  - 预览域:直接走线上 **COS 默认域** `seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com` 直链(§14.1),**不配本地 host 代理**;前端 `iframe` 的 `src` 即该线上 URL,本地联调时也能直连预览(需联网);`seedhtml.huzhen.net.cn` 自定义品牌域待生产绑定(可选增强)。

---

### 3.12 多维度运营指标(AI 效率 / 准确性 / 消耗量 / 回滚率 / 用户回馈 / 健康度)

§3.6 的「统计系统」偏**运行层**(接口 QPS、错误率、成本);本节约下沉到 **AI 生成质量与运营层**,直接回应"统计 AI 效率、准确性、消耗量、回滚率、用户回馈等多个维度"的需求。指标由实时 Redis 计数 + 落库聚合(Trace / UsageLog / Feedback,§9.1)双源计算,在 `/admin` 仪表盘(§3.6)新增「AI 质量」标签页呈现。

**指标维度总表**

| 维度 | 指标 | 定义 / 计算 | 数据源 |
|------|------|------------|--------|
| **AI 效率** | 首字延迟 TTFT | 收到提问 → 首个 token 的毫秒数(`Trace.first_token_ms`) | Trace |
| | 总生成耗时 | 提问 → done 的毫秒数(`Trace.total_ms`) | Trace |
| | 吞吐 | `tokens_out / (total_ms/1000)`(tokens/s) | Trace |
| | 对话轮次 / 站点 | 单 Project 的 Trace 数 | Trace + Project |
| | 迭代深度 | 单 Project 的平均 `Artifact.version` 数 | Artifact |
| | WebLLM 首过命中率 | 选 WebLLM 且本地成功 / 选 WebLLM 总数 | Trace.source |
| **准确性**(近似,无单一真值) | Reviewer 一次通过率 | `review_retries=0` 的 Trace / 总 Trace | Trace |
| | 平均评审打回次数 | `avg(review_retries)` | Trace |
| | 回滚率 | 见下方「回滚率」维度 | TraceEvent(rollback) |
| | 用户纠错率 | 生成后用户大改(prompt 显著变更)占比 * | Message + TraceEvent(edit) |
| | 显式评分均值(1–10) | `avg(Feedback.rating)` | Feedback |
| **消耗量** | Token 用量 | `sum(tokens_in)`、`sum(tokens_out)`,按 model / provider 拆分 | UsageLog + Trace |
| | 估算成本 | `Σ tokens × 单价`(¥,按模型) | UsageLog + Trace.cost |
| | WebLLM 省下 token | 本地首过成功省去的云端 tokens | Trace.source |
| | 取消省下 token | 经 C1 取消中断省去的 tokens | Trace.cancelled |
| **回滚率** | 回滚率 | `count(TraceEvent.type='rollback') / count(Artifact)` | TraceEvent + Artifact |
| | 回滚到旧版占比 | 回滚目标为更早版本的占比 | TraceEvent.payload |
| | 平均迭代深度 | 同「AI 效率 · 迭代深度」 | Artifact |
| **用户回馈** | 点赞 / 点踩 | `count(Feedback.thumb=up/down)` | Feedback |
| | 评分均值 | `avg(Feedback.rating)`(1–10) | Feedback |
| | 评论数 | `count(Feedback.comment 非空)` | Feedback |
| | 净推荐(后续) | NPS-ish,留扩展位 | Feedback |
| **健康度(其他)** | 成功率 | `status='done'` / 总 Trace | Trace |
| | 取消率 | `cancelled=true` / 总 Trace(C1 机制) | Trace |
| | 降级率 | `fallback_used=true` / 总 Trace(对应 #31) | Trace |
| | 模型分布 | 各 model_id 的 Trace 占比 | Trace |
| | 时间 / 用户聚合 | 按 user / project / 日 / 周 / 月 切片上述所有指标 | 以上 |
| **RAG 检索质量**(✅ v1.12 已定,方案2) | 检索命中率 | `命中(返回结果最高相似度 ≥ 阈值,默认 0.7)的 rag_retrieve 次数 / 总 rag_retrieve 次数` | TraceEvent(rag_retrieve) |
| | 无结果率 | `返回空或全低于阈值的 rag_retrieve 次数 / 总次数` | TraceEvent(rag_retrieve) |
| | 上下文采纳率(可选,M1+) | `Planner/Reviewer 标记"已采用"的检索片段 / 总注入片段` | TraceEvent(rag_retrieve.payload.adopted) |

> 注:*「用户纠错率」精确口径待 M1 用户系统落地后结合 Message 编辑距离定义;MVP 可先用「用户对某版本后立即发新 prompt 修改」粗略近似。

- **准确性为什么是"近似"**:AI 生成没有客观真值。本设计用 **三方交叉** 近似准确性 —— ① Reviewer 自动化通过率(代码可运行 / 静态分析)、② 回滚率(用户用脚投票)、③ 显式评分(用户主动打分)。三者同向恶化即预警。
- **看板呈现**:`/admin` 仪表盘在现有「运行指标」旁增「AI 质量」标签页:6 维度卡片 + 趋势折线(按日)+ 按模型 / 按用户下钻。指标计算走 Stats 模块(§3.6),原始数据全在 MySQL(Trace / UsageLog / Feedback),无需额外 TSDB。
- **取舍**:MVP 不引 Prometheus / 专业 BI;指标随 Trace 落库自然沉淀,SQL 聚合即可。量大后再做物化视图 / 定时快照(§3.6 已预留"MySQL 时序表"路线)。
- **用户回馈采集时机与分量(✅ v1.11 已定)**:对话页在**每次会话(Trace)结束后**向用户提供评价组件——**1–10 分评分**(主评价,默认不评留空)+ 点赞/点踩(可选)+ 文字评论(可选)。评分经 `trace_id` 绑定当次完整上下文(用户 prompt + 生成产物 + 思考链 + 回滚/操作),M0 匿名时 `user_id` 可空、评分仍落 `Feedback` 与 Trace 关联。
- **反馈数据的双重用途:统计 + 回归数据集(✅ v1.11 已定)**:`Feedback` 既作为本维度统计源(评分均值/分布,与回滚率交叉预警),也作为**模型回归/评测数据集**。因其经 `trace_id` 关联 `Trace`/`TraceEvent`/`Artifact`,每条评分都自带完整的「用户输入 → 生成产物 → 思考链 → 操作 → 评分」三元组;可按 `rating` 分层导出(如低分 `rating<4` 为失败样本、高分 `rating>8` 为优质样本),用于日后模型迭代时的**回归测试 / 自动评测**(CSV/Parquet 导出,接入评测脚本或人工复核)。MVP 不强制做导出功能,但保证数据可经 SQL 按 `rating` + `trace_id` 关联取出,为回归预留数据底座。
- **RAG 检索质量维度(✅ v1.12 已定,方案2)**:RAG 是独立子系统,其检索质量单独成类统计(见上方总表第7类)。**指标**:① 检索命中率 = 命中(返回结果最高相似度 ≥ 阈值,默认 0.7)的 `rag_retrieve` 次数 / 总 `rag_retrieve` 次数;② 无结果率 = 返回空或全低于阈值的检索次数 / 总次数;③ 上下文采纳率(可选,M1+) = Planner/Reviewer 标记「已采用」的检索片段 / 总注入片段。**采集**:`rag_retrieve` Skill 每次执行写一条 `TraceEvent(type='tool', name='rag_retrieve', payload={query, top_k, returned_count, max_score, threshold_hit, adopted})`,统计走 Stats 模块聚合。**看板**:`/admin`「AI 质量」标签页新增「RAG 检索质量」卡片(命中率/无结果率趋势)。**取舍**:阈值命中率比方案1「非空率」更准(过滤无意义碎片)、比方案3「LLM 评判」更省(零额外调用),采纳率反映真实效用;MVP 先落地①②、③随 M1 RAG 实装。

---

### 3.13 对话追踪与回放(Trace & Replay)

需求:"从日志层面可以回看每一次用户对话操作与对打"——即持久化 **每一次会话** 的完整过程:用户消息、AI 回复(对打)、思考链路、以及所有操作(回滚 / 反馈 / 编辑 / 取消),支持事后回放还原。

- **核心模型**:一次 SSE 会话 = 一个 `Trace`(`trace_id`);会话内所有事件按 `seq` 追加到 `TraceEvent`,形成可重放的时序流。`Message` 表(§9.1)存对话气泡(user / assistant 文本),`TraceEvent` 存颗粒度更细的 SSE 事件流(think / node / token 进度 / 操作),二者通过 `trace_id` 关联,回放时合并渲染。
- **存储方案对比(3 方案)**

  | | 方案1 MySQL 双表(Trace + TraceEvent) | 方案2 文件 JSONL(每 trace 一文件)+ 索引 | 方案3 OpenTelemetry + Jaeger |
  |---|---|---|---|
  | 落点 | 业务服务 MySQL(§9.1) | 对象存储 / 磁盘,每 `trace_id` 一个 JSONL | OTel Collector + Jaeger 后端 |
  | 回放查询 | SQL 按 trace_id / project / user / 时间窗直查,易聚合(§3.12 指标同源) | 需读文件 + 自建索引,聚合弱 | 专业 trace 查询 UI,但业务指标需另算 |
  | 一致性 | 强一致,随 §3.5 异步写队列落库 | 追加写简单,但易丢最后几条 | 最终一致 |
  | 复杂度 | ⭐ 低(已有 MySQL) | ⭐ 低,但运维索引麻烦 | ⭐⭐⭐ 重(引入 Collector / 存储 / UI) |
  | MVP 适配 | ✅ 最贴合 | 偏裸,统计要另做 | 过度设计 |

- **采纳:方案1(MySQL 双表 + Feedback)**(见决策 #34)。理由:已有 MySQL,SQL 直接回放 + 与 §3.12 指标同源计算,零额外组件;TraceEvent 只追加不改,写入复用 §3.5 异步写队列,不阻塞主流程。
- **回放视图**:管理页(`/admin`,super_admin 或本人)按 `trace_id` 拉 `TraceEvent` 序列 → 还原"用户说 / AI 想 / AI 做 / 操作"全过程;前端可复用对话页组件渲染(气泡 + 思考面板 + 操作标记)。普通 `user` 仅能回放 **自己** 的会话(RBAC,§3.6)。
- **写入时机**:业务服务在 SSE 生命周期内(§5.6 步骤 [3]–[6])把每个 SSE 帧(user_msg / think / node / token / done / error)与操作事件(rollback / feedback / edit)以 `TraceEvent` 追加;会话结束写 `Trace` 汇总(耗时 / tokens / 状态 / cancelled / fallback_used)。
- **取舍与边界**:
  - `token` 类型事件量大;MVP 全量落库(单文件 HTML 产物小,可接受),但 `TraceEvent.payload` 对 `token` 可仅存 **采样 / 增量** 或指向 `Artifact` 完整产物,避免爆表;完整产物仍以 `Artifact.files` 为准。
  - 冷数据:若未来量极大,`TraceEvent` 热数据留 MySQL、冷数据按 retention(如 90 天)归档对象存储,回放走归档检索。
  - 隐私:`Trace` 含用户原始 prompt,仅本人 / super_admin 可见;MVP 先靠 RBAC 控访问,后续可加列脱敏。
  - 与 §3.8 关系:原"MVP 不落库"已改为"MVP 起即落 MySQL 可回放"(§3.8 日志归集同步更新)。

---

## 4. 用户系统与鉴权(已定方向)

### 4.1 需求(MVP 与后期)
- **MVP(现在做)**:账号密码注册/登录,JWT 鉴权,按 plan/tier 配额限流(在业务服务内)。
- **后期(开放时再做)**:手机号 + 短信验证码注册/登录/找回 —— 仅先预留 `SMSService` 抽象层与 Redis 验证码结构。
- **收费模式**:设计为未来可平滑接入 —— 用户带「套餐/层级(plan)」概念;计费/订阅相关表预留扩展位(见 §4.5)。

### 4.2 认证方式
- **密码**:服务端用 `argon2` / `bcrypt` 哈希存储(绝不存明文),使用 `passlib` 或 `argon2-cffi`。
- **短信验证码**:经 `SMSService` 抽象层;验证码存 Redis(5 分钟 + 限频)。MVP 仅 Mock。
- **会话令牌**:**JWT(Access Token 短期) + Refresh Token 存 Redis**(可吊销)。无状态 Access Token 利于横向扩容。
  - ✅ v1.0 已定:**Access Token 15 分钟**,通过 **HttpOnly + Secure + SameSite Cookie** 下发(防 XSS 盗 token);**Refresh Token 存 Redis**(`auth:refresh:<userId>`,可吊销,7 天)。前端请求带 `credentials`,不直接持有 token。

### 4.3 关键流程(MVP:账号密码)
- 注册:提交 username/email + password → 哈希入库(默认 plan=`free`)→ 签发 JWT(业务服务)。
- 登录:校验 password_hash → 签发 JWT。
- 生成请求:前端带 JWT → 业务服务校验 → 透传 modelId + 上下文给 AI 服务(内网)→ 流式返回。
- 限流:登录/注册/生成接口用 Redis 令牌桶限频,防 Key 盗刷;**free plan 每日生成配额默认 50 次/天**(见 §9.1 `quota_limit`),超额提示升级(付费预留)。
- **角色与初始化**:普通注册一律得到 `user`;**初始 `super_admin` 通过种子脚本 / 环境变量 `SEED_SUPER_ADMIN=<username>` 创建**(首次启动注入,避免角色自举漏洞);仅 `super_admin` 可在用户管理接口将 `user` ↔ `admin` 互相调整,`super_admin` 角色本身不可被降级/移除(防锁死)。详见 §3.6 RBAC 三级矩阵。

### 4.4 抽象层设计(SMSService,预留)
```python
class SMSService(ABC):
    @abstractmethod
    async def send_code(self, phone: str, code: str) -> None: ...

SMS_PROVIDERS: dict[str, SMSService] = {
    "mock":    MockSMSService(),          # MVP:打印到日志,不真实发送
    # "aliyun":  AliyunSMSService(...),    # 开放时启用
    # "tencent": TencentSMSService(...),   # 开放时启用
}
```

### 4.5 为收费模式预留的扩展点
- `User.plan`(默认 `free`)+ `plan_expire_at`;配额取数集中在 `get_quota()` 依赖(业务服务)。
- 后续加 `Subscription`、`Payment` 表即可接支付(微信/支付宝/Stripe)。
- 配额校验集中一处,未来从「按 plan 查 quota」改为「查 Subscription」只改该依赖。

---

## 5. 编排层:Router + Skill 路由 + 轻量多 Agent 协作(Hybrid)

### 5.1 模式选择(Hybrid,已定)
编排架构由「纯多 Agent」调整为 **Hybrid**:
- **外层 Router + Skill**:一个轻量 **Router**(一次小模型调用或规则)做意图识别,把请求分发到对应 **Skill**(封装好的能力单元);Router 同时是**鉴权 + 配额闸门**(接 §3.5 free 50 次/天)。
- **内层仅对 `generate_site` 保留轻量多 Agent**:`Planner → Coder → Reviewer`,且 **Planner 与 Coder 共享上下文**(不重复喂整段对话),**Designer 在 MVP 降级为 Coder 的 style 约束**(非独立 agent)。
- **其余 Skill 单次 LLM 直出**:`write_code` / `generate_doc` / `explain` / `rag_retrieve` 无需多 agent 协作,一次模型调用即出结果。

**为什么 Hybrid 而非纯多 Agent**:主产物(单文件站点)是收敛输出,并不需要 N 个专家反复商量;纯多 Agent 每次生成 3-4 次完整上下文调用,成本与延迟偏高。Hybrid 把"分发/权限"与"协作"各放对位置——简单请求 1 次调用,只有"生成站点"且首版不达标才走完整链 + 回退,配合 §3.9 WebLLM 首过 / §13.2 模型降级更稳。**思考过程可视化(`think`/`node`)与 Reviewer 回退机制原样保留**(§5.5),前端无感。

### 5.2 角色与 Skill 清单(运行于核心 AI 服务)
| 层 | 组件 | 职责 |
|----|------|------|
| 路由 | **Router** | 意图识别、分发 Skill、鉴权、配额闸门;产出 `node:enter router` / `node:dispatch` 思考事件 |
| Skill | **generate_site** | 生成网站/前端页面 —— 内部走轻量多 Agent(Planner→Coder→Reviewer,见 §5.3) |
| Skill | **write_code** | 写/改代码片段(单次 LLM 直出) |
| Skill | **generate_doc** | 生成文档/说明(单次 LLM 直出) |
| Skill | **explain** | 解释/问答(单次 LLM 直出) |
| Skill | **rag_retrieve** | 向量检索(§7,返回上下文给上游 Skill) |
| 协作 | **Planner**(仅 generate_site) | 需求拆解、页面结构规划、技术选型(与 Coder 共享上下文) |
| 协作 | **Coder**(仅 generate_site) | 生成单文件 HTML/CSS/JS(+ style 约束替代 Designer) |
| 协作 | **Reviewer**(仅 generate_site) | 静态分析 + LLM 自审(§13.3),校验可运行、查错、按需打回(≤3 轮) |

### 5.3 generate_site 内层状态图(LangGraph · 伪代码)
```python
from langgraph.graph import StateGraph, END

class SiteState(TypedDict):
    spec: str          # Planner 产出(与 Coder 共享,不重复喂原始对话)
    html: str
    reviews: int
    passed: bool

def planner(state): ...      # 需求拆解 → spec
def coder(state): ...        # spec + style 约束 → html
def reviewer(state): ...     # 静态分析 + LLM 自审 → passed / 修改建议

graph = StateGraph(SiteState)
graph.add_node("planner", planner)
graph.add_node("coder", coder)
graph.add_node("reviewer", reviewer)
graph.add_edge("planner", "coder")
graph.add_edge("coder", "reviewer")
graph.add_conditional_edges("reviewer", route_after_review,
    {"pass": END, "retry": "coder"})   # ≤3 轮,避免死循环
app = graph.compile()
```
- 其余 Skill(`write_code`/`generate_doc`/`explain`)是普通函数,**单次 `PROVIDERS[model_id]` 调用即返回**(无状态图)。
- RAG(§7)在 `generate_site`/`generate_doc` 的 Planner/Coder 前注入组件与记忆上下文(Chroma)。
- 各调用统一走 §6 模型抽象层 + §13.2 `FallbackRouter`(降级)。

### 5.4 服务间调用关系
```
前端 → 业务服务 /api/chat (SSE, 校验 JWT cookie, 记用量/配额)
         └─ [WebLLM 旁路] 若选「本地 WebLLM」:前端本地首过,同构 think/node/token 事件,失败/超时回退下方
         └─ 内网 → AI 服务 /generate (SSE, 经 Redis 队列+Worker 产出, 见 §3.7/§3.9 与 1-C)
                     └─ Router(意图/鉴权/配额) → 分发 Skill
                           ├─ generate_site → {Planner→Coder→Reviewer}(LangGraph, 轻量多 Agent)
                           ├─ write_code / generate_doc / explain → 单次 LLM
                           ├─ rag_retrieve → Chroma(5-C 语义缓存)
                           └─ 模型抽象层 + FallbackRouter → DeepSeek/Qwen/HY3(2-C 降级)
                           └─ Redis(queue:generate / 进度 / 缓存)
         └─ think/node/token 事件经业务服务透传回前端 → 思考面板 + iframe 预览
```

---

### 5.5 思考链路事件(Thinking Trace)

Router 与 generate_site 内层在协作中产出**结构化思考事件**,经业务服务转发为 SSE `think`/`node` 帧(§3.7),前端据此展示"AI 是怎么想的":

| 事件 | 触发点 | 内容示例 |
|------|--------|---------|
| node:enter router | 收到需求 | "正在识别意图、校验配额" |
| node:dispatch | Router 分发 | "判定为 generate_site,进入网站生成链路" |
| node:enter planner | 进入规划 | "拆解需求 → 页面结构/技术选型" |
| think:planner | Planner 产出 | 结构化规格(板块/布局/风格) |
| node:enter coder | 进入编码 | "开始生成单文件 HTML(含 style 约束)" |
| think:coder | Coder 推理 | 关键实现思路(可选) |
| node:enter reviewer | 进入评审 | "静态分析 + LLM 自审" |
| think:reviewer | Reviewer 结论 | 通过 / 打回原因(≤3 轮) |
| node:done | 完成 | 产物就绪 |

> 思考事件与 `token`(产物流)分离:前端可同时显示"思路"与"成品"。简单 Skill(写代码/文档)仅发 `node:dispatch` + `token`,无多余 `think` 噪音。

---

### 5.6 用户对话端到端详细流程图

下图描述一次完整提问从前端到产物的全链路(含 WebLLM 旁路、队列、Router/Skill、思考事件流与断连):

```
[1] 打开/登录
  前端 → 业务服务 /auth/me(校验 HttpOnly JWT Cookie)
  未登录 → /auth/login → 签发 Access(15min)+Refresh(Redis) → 回到对话页

[2] 选择模型(模型选择器)
  ├─ 本地 WebLLM(快速)        → 走 [3B] 本地首过旁路
  └─ 云端模型(DeepSeek/Qwen/HY3,用户手选优先级) → 走 [3A] 云端链路

[3A] 云端链路 · 发起提问
  前端 ──GET /api/chat(SSE, 带 Cookie)──▶ 业务服务
    业务服务:
      a. 校验 JWT → 取 user/role/plan
      b. 配额检查(free 50 次/天,Redis 令牌桶)→ 超额则 error 帧
      c. 建 trace_id(=本次 SSE 会话),写结构化日志(§3.8)
      d. 任务入 Redis 队列 queue:generate(1-C),返回 SSE 流
    前端:对话气泡显示"生成中";思考面板订阅 think/node 事件

[3B] 本地首过旁路(WebLLM)
  前端用 WebGPU 本地跑 Planner+Coder,**同构** think/node/token 事件本地产出
  成功 → 直接进 iframe 预览,不发云端请求
  失败/超时/不支持 WebGPU → 自动回退 [3A] 云端链路

[4] AI 服务 · Worker 消费(1-C)
  Worker 出队 → 调 Router(意图/鉴权/配额复核)
    node:enter router → node:dispatch(判定 Skill)
    ├─ generate_site → LangGraph{Planner→Coder→Reviewer}
    │     think:planner → node:enter coder → think:coder
    │     → node:enter reviewer → think:reviewer(≤3 轮回退)
    ├─ write_code/generate_doc/explain → 单次 LLM
    └─ 需 RAG → rag_retrieve(Chroma)
  模型调用经 FallbackRouter(2-C):按优先级,失败回退+「已降级」标记
  进度(think/node/token)实时写入 Redis Stream gen:progress:<trace_id>

[5] 透传与呈现
  业务服务 SSE 端点订阅 gen:progress:<trace_id> → 逐帧转发前端
    前端:对话气泡 / 思考面板(实时思路)/ iframe 预览(产物)三区同步
    产物经 §13.4 sandbox+CSP 注入 iframe 预览

[6] 结束
  Worker 完成 → 发 done 帧 → 业务服务结束 SSE 响应 → 前端 EventSource 断连
  用量记 UsageLog(模型/tokens);思考链路仅实时(MVP 不落库,§3.8)
```

> 关键性质:**每问一连接**(步骤 [3] 起 SSE,步骤 [6] done 即断连);**思考可见**(步骤 [4] 的 think/node 实时到面板);**成本可控**(简单 Skill 单次调用、复杂才走完整链 + 回退,叠加 WebLLM 首过与模型降级)。

---

### 5.7 架构演进候选(备选形态与推荐)

> 当前已定 Hybrid 为基线。以下梳理其余主流 agent 形态作为**演进候选**,并给出针对本项目的推荐姿态(不破坏已锁定的任何决策)。

| 形态 | 一句话 | 与本项目的契合度 | 是否纳入 |
|------|--------|------------------|----------|
| ReAct(单 agent+工具循环) | 一个 agent 自决调用工具(搜组件/跑代码/抓网页) | 简单 skill(write_code/explain)已接近此形态;对"生成完整站点"表达力弱,思考可视化需自埋点 | 仅作简单 skill 实现思路,不成体系 |
| Plan&Execute(先计划再执行) | 轻量 LLM 先出完整页面规格,再交 Coder 按计划生成 | **高**:与"Planner/Coder 共享上下文"一脉,但 Plan 不被执行污染;思考面板可展示完整计划书;Reviewer 按计划逐条核对;比 Supervisor 线性链更省 token | ✅ **推荐为内层 generate_site 升级方向** |
| Graph/DAG(有向图编排) | 流程画成图,支持并行/分支/循环(LangGraph 本质) | 中:适合"生成站点+同时写文档+同时存向量"多产物并行;MVP 用不上 | M2 多产物时再上 |
| Debate(多体互审辩论) | 两个 Coder 互相挑刺收敛 | 低:token 翻倍、延迟高;对单文件 HTML 产物过度设计 | 不推荐 |
| Reflexion(失败→反思→重写) | 叠加层:生成失败/评审打回时先反思再重写 | **高**:与已定"Reviewer 回退"天然契合,把回退升级为"带记忆的回退",成本几乎为零 | ✅ **推荐叠加到任一架构(含 generate_site)** |

**推荐姿态(已采纳方向)**
- **外层 Router+Skill 保持不变**(意图/鉴权/配额闸门)。
- **内层 `generate_site` 演进为 Plan-and-Execute + Reflexion 叠加**:
  1. Planner 产出结构化页面规格(板块/布局/风格/技术选型)→ 作为 `plan` 事件推前端思考面板;
  2. Coder 严格按 `plan` 生成单文件 HTML(不重复喂整段对话,共享 plan 上下文);
  3. Reviewer 按计划逐条核对 + 静态分析(3-C);不达标则进入 **Reflexion 循环**:将失败原因写入短期记忆 → 反思 → 重写(≤3 次,避免死循环);
  4. 思考事件 `think`/`node` 原生保留,前端无感。
- 该演进不引入新依赖,仅调整 `generate_site` skill 内部节点顺序与回退逻辑,可在 M0 落地或 M1 增强时实施。

### 5.8 Skill / Agent 的引入与注册机制(扩展性设计)

**问题**:已有 Router + 多个 Skill(`generate_site` / `write_code` / `generate_doc` / `explain` / `rag_retrieve`),未来还会加更多能力(如 `generate_docx`、`generate_ppt`、自定义 Agent)。需要一套"引入新 Skill / 新 Agent 不改动 Router 核心"的扩展机制,避免路由表越改越脆。

**采纳方案:SkillRegistry(技能注册表)+ 开闭原则(✅ v1.13,决策 #38)**

- 核心是一个**注册表** `SkillRegistry: dict[name → SkillEntry]`,每个 `SkillEntry` 包含:
  - `name`:技能名(如 `generate_site`)
  - `intent_tags`:意图标签(辅助 Router 匹配,如 `["site","网页","页面"]`)
  - `handler`:处理函数,或已编译的 LangGraph app
  - `is_graph`:是否多 agent 状态图(`True` 时 `handler` 为 `graph.compile()`)
  - `description`:给 Router / 前端展示的技能说明
- **Router 分发流程(运行期)**:
  1. Router 用一次小模型调用(或规则)判定用户 `intent`;
  2. 在 `SkillRegistry` 中按 `intent_tags` / 显式指令匹配 `SkillEntry`;
  3. 调 `entry.handler(...)`,返回 `think` / `node` / `token` 事件流(§5.5)。
- **如何引入一个新 Skill(单次 LLM 型,如 `generate_docx`)**:
  1. 写 handler 函数(输入 `ctx`,输出结果;统一走 §6 Provider + §13.2 `FallbackRouter`);
  2. 注册:`register_skill(name="generate_docx", intent_tags=["docx","文档生成"], handler=generate_docx, is_graph=False)`;
  3. Router 即可识别并分发,**无需改 Router 核心代码**(符合开闭原则)。
- **如何引入一个新 Agent / 多 agent 协作(如新 `generate_ppt` 内部多 agent)**:
  1. 用 LangGraph 定义状态图(类似 §5.3 的 Planner/Coder/Reviewer);
  2. 注册为 `is_graph=True` 的 Skill,`handler = graph.compile().invoke`;
  3. 对外仍表现为"一个 Skill",Router 无感知差异——**多 agent 协作被封装在 Skill 内部**。
- **动态发现(可选,M2+)**:未来可扫描 `ai_service/skills/` 目录下的 `SKILL.md` / Python 模块自动注册(类似 WorkBuddy 的 skill 加载机制),支持运营/用户上传自定义 Skill,实现能力热插拔。
  - **Skill 的"触发词/关键词"从哪来**:Router 的意图判定依据是 **用户 prompt 语义 + 可选显式指令**(如命令式 `/doc`、UI 上"生成文档"按钮);即 Skill 触发来自**用户输入与界面操作**,`intent_tags` 仅作语义匹配辅助提示,不替代模型判定(避免硬编码路由表的脆弱匹配)。

### 5.9 Tool(工具)的来源与注册机制(执行层原子能力 · ✅ v1.16,决策 #42,方案2)

**问题**:§5.2 中 `rag_retrieve` 既是 Skill 又是被 `generate_site` 调用的工具,概念混用。需澄清 **Skill(完整任务能力)** 与 **Tool(原子操作)** 的区别,并定义 tool 的**来源 / 注册 / 调用方式**。

**概念区分:Skill vs Tool**
- **Skill**:对外暴露的"一个完整任务能力",由 Router 按意图分发(如 `generate_site` / `explain`);一次调用产出完整结果(§5.8)。
- **Tool**:Skill 内部 agent 可调用的"原子操作",经 LLM **function calling**(工具调用)暴露给模型,模型自主决定何时调用。如向量检索、COS 上传、代码执行、HTTP 请求。

**采纳方案:ToolRegistry(工具注册表)+ 开闭原则(方案2)**

- 核心是一个注册表 `ToolRegistry: dict[name → ToolEntry]`,每个 `ToolEntry` 包含:
  - `name`:工具名(如 `rag_retrieve`)
  - `schema`:JSON Schema(参数名 / 类型 / 描述),用于 function calling 暴露给模型
  - `func`:实际执行函数
  - `scope`:`internal`(仅内部 agent 调用)/ `user_exposed`(用户可见可触发,如 UI 按钮 / 显式指令)
  - `risk`:`safe` / `dangerous`(危险工具需权限校验 / 沙箱隔离)
- **Tool 来源分类(3 类)**:
  - **A. 内置工具**:代码内用 `@tool` 装饰器实现并注册(如 `rag_retrieve` / `cos_upload` / `web_search` / `code_run`)。**MVP 全部走这层**(内置、可控、零额外依赖)。
  - **B. 运营 / 用户贡献工具(M2+)**:一个 Python 文件 + schema 声明,放入 `ai_service/tools/` 目录;启动扫描自动注册(与 §5.8 `SkillRegistry` 同款目录发现机制),支持能力热插拔。
  - **C. 第三方 MCP 工具(M3+)**:接外部 MCP server,协议自动将 MCP 能力转为 tool schema(如高德地图、GitHub);MVP 不接(过度设计,留扩展口)。
- **如何引入一个新内置 Tool**:
  1. 写函数 + `@tool` 装饰器声明 schema;
  2. `register_tool(name=..., schema=..., func=..., scope=..., risk=...)`;
  3. Skill 内部 agent 经 function calling 即可见可用,Router / 核心无需改动(开闭原则)。
- **`rag_retrieve` 双入口澄清**:
  - **对外是 Skill**(用户说"检索我的组件库" → Router 分发到 `rag_retrieve` skill,返回上下文给用户);
  - **对内是 Tool**(`generate_site` 内部 Planner 检索上下文时调用 `rag_retrieve` tool 增强生成质量,§7)。
  - 一个实现、`scope=internal` 供内部调用、`scope=user_exposed` 供 UI / 显式指令触发;本质同一函数两个入口。
- **动态发现(可选, M2+)**:扫描 `ai_service/tools/` 自动注册(类比 §5.8 `skills/` 扫描);用户贡献工具须经**沙箱 + 权限白名单**(由 `risk` 字段控制),防恶意工具。
- **与 SkillRegistry 关系**:`ToolRegistry` 是**执行层原子能力池**,`SkillRegistry` 是**编排层任务分发**;Skill 内部 agent 通过 function calling 从 `ToolRegistry` 取用 tool。两者都遵循"注册表 + 开闭原则 + M2+ 目录扫描",风格统一、互不耦合。

> **本地实现(✅ v1.17,§5.10)**:上述注册表已落到 `backend/ai_service/app/registry/`(`skill_registry.py` / `tool_registry.py`),`tools/` 与 `skills/` 目录即能力插件目录,`registries.bootstrap()` 完成注册。详见 §5.10。

---

### 5.10 本地 Skills / Tools 落地清单(✅ v1.17,决策 #43)

**背景**:§5.8 / §5.9 只定义了注册表契约,本版把**真实的 Skill / Tool 落到本地代码**(`backend/ai_service/app/`),严格对齐契约,并引入**成熟开源库 + 成熟 agent 模式**实现,使注册表从"空"变为"可用、可扩展"。

**目录结构**(均位于 `backend/ai_service/app/`):

```
registry/                 # §5.8/§5.9 注册表核心
  skill_registry.py       # SkillRegistry + register_skill + match(意图子串匹配)
  tool_registry.py        # ToolRegistry + register_tool + @tool 装饰器(含 schema 推断)
  __init__.py
tools/                    # §5.9 来源 A 内置工具(@tool 注册,导入即注册)
  rag_retrieve.py  web_search.py  fetch_url.py  cos_upload.py
  browser_screenshot.py  image_generate.py  html_validate.py  file_io.py
  __init__.py
skills/                   # §5.8 Skill(register_skill 注册)
  generate_site.py        # 核心建站(Planner→Coder→Reviewer, LangGraph, is_graph=True)
  write_code.py  generate_doc.py  explain.py
  rag_retrieve_skill.py   # 与 tools.rag_retrieve 双入口
  __init__.py
registries.py             # bootstrap():导入 skills/tools 包完成注册 + 目录扫描热插拔
router.py                 # detect_intent(规则匹配) + dispatch(分发到 Skill,流式透传)
```

**已落地 Skill 清单(§5.8)**:

| Skill | intent_tags(节选) | 类型 | 成熟来源 / 模式 | 文件 |
|-------|------------------|------|----------------|------|
| `generate_site` | site/网页/页面/落地页/官网/博客 | 图(is_graph) | LangGraph + Plan-and-Execute + Reflexion(§5.7) | `skills/generate_site.py` |
| `write_code` | 代码/code/脚本/函数 | 单次 LLM | ReAct 简化形态(§5.7) | `skills/write_code.py` |
| `generate_doc` | 文档/doc/readme/教程 | 单次 LLM | 单次 LLM 直出 | `skills/generate_doc.py` |
| `explain` | 解释/问答/为什么 | 单次 LLM | 单次 LLM 直出 | `skills/explain.py` |
| `rag_retrieve` | 检索/组件库/记忆/搜一下 | 单次(双入口) | 复用 `tools.rag_retrieve` | `skills/rag_retrieve_skill.py` |

**已落地 Tool 清单(§5.9,来源 A 内置)**:

| Tool | scope | risk | 成熟来源 | 文件 |
|------|-------|------|----------|------|
| `rag_retrieve` | user_exposed | safe | Chroma + Qwen text-embedding(§7) | `tools/rag_retrieve.py` |
| `web_search` | internal | safe | Tavily / Serper / DuckDuckGo lite(三级回退) | `tools/web_search.py` |
| `fetch_url` | internal | safe | httpx + BeautifulSoup4 | `tools/fetch_url.py` |
| `cos_upload` | internal | safe | 腾讯云 cos-python-sdk-v5(§10) | `tools/cos_upload.py` |
| `browser_screenshot` | internal | dangerous | Playwright(无头 Chromium) | `tools/browser_screenshot.py` |
| `image_generate` | user_exposed | safe | OpenAI 兼容 images API | `tools/image_generate.py` |
| `html_validate` | internal | safe | 标准库 html.parser | `tools/html_validate.py` |
| `file_write` / `file_read` | internal | safe | 标准库 pathlib | `tools/file_io.py` |

**关键工程约定(落地时确立)**:
1. **重依赖懒加载**:`chromadb` / `cos-python-sdk-v5` / `playwright` 均在**函数体内** `import`;缺包时该工具返回清晰错误(`{"ok":False,"error":"..."}`),**不阻断包导入与整体注册**——这是"单个 Skill/工具失败不拖垮注册表"的健壮性来源。
2. **导入即注册**:`tools/__init__.py` / `skills/__init__.py` 导入各模块时执行 `@tool` / `register_skill`;`registries.bootstrap()` 导入两包即完成全量注册。
3. **目录扫描热插拔(M2+)**:`bootstrap()` 额外扫描 `tools/` / `skills/` 目录,直接丢 `.py` 文件即生效(无需改 `__init__`);单文件异常被捕获,不阻断整体。
4. **Router 默认兜底**:`detect_intent` 无命中时兜底到 `generate_site`,保证任何输入都有响应。
5. **调试端点**:AI 服务新增 `GET /skills`、`GET /tools`、`GET /registry`,查看已注册能力(admin 看板可对接)。

**如何新增(开闭原则)**:
- 新内置 Tool:`tools/` 下新建模块,`@tool(name=, schema=, scope=, risk=)` 装饰函数,在 `tools/__init__.py` 加一行 import;模型经 function calling 即可见。
- 新 Skill:`skills/` 下新建模块,调 `register_skill(name=, intent_tags=, handler=, is_graph=)`;Router 自动识别分发。
- 新多 Agent:用 LangGraph 定义图,`register_skill(..., is_graph=True, handler=graph.compile())`,对外仍是一个 Skill。
- 运营/用户贡献(M2+):直接把 `.py` 丢进 `tools/` 或 `skills/`,重启即被 `bootstrap()` 扫描注册(`risk=dangerous` 工具须走沙箱 + 权限白名单)。

**验证(本地实测)**:建托管 venv,`bootstrap()` 实测注册 **5 Skill + 9 Tool**;`/generate` 经 Router 分发;`web_search` 无 key 走 DuckDuckGo 实返回 5 条;`rag_retrieve` / `cos_upload` 缺依赖时优雅返回错误而非崩溃。契约完全对齐 §5.8 / §5.9。

---

## 6. 模型抽象层设计(Python · FastAPI,位于 AI 服务)

目标:新增一个模型 = 写一个适配器 + 注册,前端无感、业务服务无侵入。

```python
from abc import ABC, abstractmethod
from typing import AsyncGenerator

class BaseLLMProvider(ABC):
    id: str
    label: str
    @abstractmethod
    def chat_stream(self, messages: list[dict], **kwargs) -> AsyncGenerator[str, None]: ...

PROVIDERS: dict[str, BaseLLMProvider] = {
    "deepseek": DeepSeekProvider(api_key=settings.DEEPSEEK_KEY),
    "qwen":     QwenProvider(api_key=settings.QWEN_KEY),
    "hy3":      HY3Provider(api_key=settings.HY3_KEY),
}

# AI 服务对外暴露(流式 SSE):POST /generate(SSE,见 §3.7);内部经 Redis queue:generate + Worker 产出(1-C)。思考事件同 §5.5。
@app.post("/generate")
async def generate(req: GenerateReq):
    provider = PROVIDERS[req.model_id]
    return StreamingResponse(provider.chat_stream(req.messages),
                             media_type="text/event-stream")

@app.get("/models")
async def list_models():
    return [{"id": p.id, "label": p.label} for p in PROVIDERS.values()]
```

- 业务服务:把前端 `model_id` 与上下文透传给 AI 服务;不感知具体模型实现。
- 扩展:新增模型只需实现 `BaseLLMProvider` 并注册一行。

---

## 7. RAG 设计(Chroma,位于 AI 服务)

两个用途,两个独立 Collection:

| Collection | 内容 | 何时写入 | 何时检索 |
|-----------|------|---------|---------|
| `components` | 优质组件/页面模板片段 + 描述 | 运营预置/沉淀 | 生成前:检索 Top-K 拼进 Prompt,提升质量 |
| `memory` | 用户历史项目摘要/对话要点 | 每次生成后异步写入 | 生成前:检索该用户相关历史,注入上下文 |

**流程**:用户需求 → Embedding → Chroma 检索(components + memory) → 拼装增强 Prompt → 调模型 → 生成 → 异步回写 memory。

✅ v1.0 已定:**Qwen text-embedding**(复用 DashScope,与千问同生态、零额外部署)。M0 不接向量检索,M1 做 RAG/components 检索时落地。

### 7.1 记忆系统(Memory)与检索闭环(✅ v1.13,决策 #39)

本系统的"记忆"不只有 Chroma,而是一个**多层读写闭环**。

**记忆分层**:

| 层 | 载体 | 内容 | 生命周期 | 读写时机 |
|----|------|------|---------|---------|
| 短期记忆 | MySQL `Trace` / `TraceEvent`(§3.13) | 本次会话完整事件流(用户说 / AI 想 / AI 做 / 操作) | 一次提问;结束落库可回放 | 实时写;**不进向量库** |
| 长期记忆(用户) | Chroma `memory` | 用户历史项目摘要 / 对话要点 | 跨会话、跨项目 | 生成后异步回写;生成前检索 |
| 组件记忆 | Chroma `components` | 优质组件 / 模板库 | 长期(运营沉淀) | 运营预置;生成前检索注入 |
| 语义缓存 | Chroma `cache:gen` | 相似 prompt → 历史产物(5-C) | 长期 | 生成前查(命中复用);生成后写 |
| 反馈记忆 | MySQL `Feedback`(§3.12) | 1–10 评分 + 评论 | 长期 | 用户评分后写;既统计又回归 |

**检索闭环流程图(记忆如何读写)**:

```
┌──────────────────────────────────────────────────────────────────┐
│  [0] 用户需求(prompt)                                              │
└────────────────────────────────┬─────────────────────────────────┘
                                  │
                 ┌────────────────┴─────────────────┐
                 ▼ [1] 构造查询 / 关键词               │ (见 §7.2 关键词来源)
                 │  ├─ A 原始 prompt(稠密 embedding)    │
                 │  ├─ B Planner 结构化 spec 抽取关键词 │
                 │  └─ C 意图 + D 项目上下文(userId)    │
                 └────────────────┬─────────────────┘
                                  ▼
                        [2] Qwen text-embedding
                                  ▼
                   [3] Chroma 检索 Top-K(混合召回)
                        ├─ components(组件库)
                        ├─ memory(该用户历史)
                        └─ cache:gen(相似历史产物)
                                  ▼
                        [4] 拼装增强 Prompt(上下文注入)
                                  ▼
              ┌───────────────────┴───────────────────┐
              ▼                                         ▼
   [5] Router 分发 Skill              (cache:gen 命中 ⇒ 直接复用产物,省 token)
              │
              ▼
   [6] generate_site 内层(Planner→Coder→Reviewer)
              │
              ▼
   [7] 生成产物 HTML ──→ COS 投递(preview_url)
              │
   ┌──────────┼──────────────────────────┐
   ▼          ▼                           ▼
[8] 异步回写 [9] 异步回写                [10] 用户评分(1–10)
   memory     cache:gen                  → Feedback(统计 + 回归)
   (跨会话复用)(未来相似请求省 token)
              │
              ▼
   [11] 优质产物(Reviewer 通过 + 高评分)经运营审核 → 沉淀 components

   ⇒ 闭环:读前增强 → 写后沉淀 → 模型越用越准、越省 token(自我改进飞轮)
```

**性质**:记忆系统是**读写闭环**——生成前从 Chroma / 缓存"读"上下文增强质量,生成后把产物 / 摘要 / 评分"写"回长期记忆与缓存,形成持续自我改进的飞轮(§3.12 的"准确性 / 消耗量 / 回滚率"指标正是这个飞轮的观测窗口)。

### 7.2 关键词来源(检索查询从哪来 · 混合检索)

RAG 默认用 **Qwen text-embedding** 对查询做稠密向量召回(§7)。但仅靠整句 embedding 会有噪声,因此采用 **混合检索(稠密 + 关键词 / metadata 过滤)**:稠密向量负责"语义召回",关键词与 metadata 负责"精确过滤 + 排序加权"。关键词来自以下四个来源:

| 来源 | 说明 | 用途 |
|------|------|------|
| **A. 原始用户 prompt** | 整句做 embedding;同时轻量分词 / 去停用词得关键词 | 稠密召回主体 + 基础关键词 |
| **B. Planner 结构化 spec 抽取** | `generate_site` 的 Planner 产出(板块 / 布局 / 风格 / 技术选型),提取关键词如 `landing page` / `react` / `深色主题` | 最准的查询构造,提升检索精度(**推荐主来源**) |
| **C. Router 意图标签** | Router 判定的 `intent`(如 `generate_site` / `explain`) | 限定检索 scope(只搜相关 collection) |
| **D. 项目 / 用户上下文** | 当前 `projectId` / `userId` + 前序对话 | `memory` 检索的过滤条件(`userId` 必带) |

- **回写侧的关键词(写入)**:生成完成后,从产物提取标题 / 技术标签(`components.tags`,见 §9.3)+ 用户评分,作为该条 `memory` 的 `summary` 与 metadata,供未来检索命中。
- **推荐**:以 **B(Planner spec)+ A(原始 prompt)** 为主构造查询,**C(意图)** 做 scope 限定,**D(userId/projectId)** 做 `memory` 过滤。即"关键词"主要来自 **Planner 的结构化理解与用户输入本身**,而非独立的关键词库(符合"模型判定优先、规则仅辅助"的一贯取向)。

---

## 8. 部署方案

**阶段一 — 本地(Docker Compose)**
`docker-compose.yml` 起全套(各自独立容器):
- `frontend`(Vue 3 + Vite + TS,静态 SPA;docker 镜像用 Vite build 产物 + 轻量静态服务器,如 nginx-alpine)
- `business-api`(业务服务:FastAPI,持 DB/JWT 配置;含缓存读写与错误队列定时检查器,MVP 内置)
- `ai-service`(AI 服务:FastAPI + Router+Skill + LangGraph(generate_site 内层),持模型 Key / Chroma 配置)
- `mysql`、`chroma`、`redis`

**阶段二 — 多服务器扩容**
- 前端、业务服务、AI 服务各自镜像化,可分别多实例部署。AI 服务算力吃紧时**单独加副本**(微服务拆分的核心收益)。
- 数据层独立部署或云托管。
- 前置 Nginx / 负载均衡:仅暴露前端与业务服务,AI 服务留在内网。
- 会话/限流/Refresh Token 在 Redis,保证业务服务无状态、可水平扩展;AI 服务亦无状态(状态在 Chroma/Redis),可水平扩展。
- 长耗时生成走 Redis 队列 + Worker 池(**1-C,MVP 即上**,挂在 AI 服务侧,可多副本水平扩展);错误队列的定时检查器随业务服务内后台任务(§3.5)。

---

## 9. 数据模型(初稿)

### 9.0 Schema 自动初始化(启动时自检建表)
- **背景**:用户尚未手动建表;要求**系统运行时一次性自检**:无表则用代码构建。
- **机制(幂等,三库分别处理)**:
  1. **MySQL**:业务服务 `lifespan` 启动事件调用 `init_db()`,`inspect(engine).get_table_names()` 检测,缺失则 `Base.metadata.create_all(bind=engine)` 建全部表(SQLAlchemy `create_all` 本身幂等,只建不存在的表);**不删不改已有表**,可重复安全执行。
  2. **Chroma**:`client.get_or_create_collection(name=...)`(§9.3),不存在则建、存在则复用。
  3. **Redis**:无表概念(KV 即用);启动时仅 `await redis.ping()` 探活,失败告警。
- **取舍**:MVP 用**代码建表**(零迁移工具、随代码演进);生产可选 Alembic 迁移(不在 MVP)。代码建表仅适用于"从零初始化",后续结构演进(改列/加索引)再引入迁移工具。
- **落点**:`business/app/db/base.py` 定义 `Base` 与 `init_db()`;`ai_service` 若需本地表同样在自身 `lifespan` 初始化。

### 9.1 MySQL(归业务服务)
```
User      { id, username, email(可空), phone(可空,唯一), password_hash,
            role(默认 'user', 可 'admin' / 'super_admin'),
            plan(默认 'free'), plan_expire_at(可空),
            quota_used, quota_limit, created_at }
Project   { id, user_id, title, share_id(可空), created_at, updated_at }
Message   { id, project_id, role(user/assistant), content, model_id, created_at }
Artifact  { id, project_id, version, files(JSON), preview_url(可空,COS 直链,§14.1), created_at }
UsageLog  { id, user_id, model_id, tokens_in, tokens_out, created_at }

# 对话追踪与回放(§3.13,归业务服务)
Trace     { id(trace_id), user_id(可空,M0 匿名时临时 id), project_id(可空), model_id,
            source(cloud/webllm), status(done/cancelled/error),
            first_token_ms(可空), total_ms(可空), tokens_in, tokens_out, cost(可空,¥),
            review_retries(默认 0), fallback_used(默认 false), cancelled(默认 false),
            started_at, ended_at(可空) }
TraceEvent { id, trace_id, seq, ts, type(user_msg/think/node/token/tool/error/done/rollback/feedback/edit),
             payload(JSON), created_at }
Feedback  { id, trace_id, user_id(可空), rating(1–10,可空), thumb(可空 up/down), comment(可空), created_at }
```
> 命名约定:MySQL 列名统一 **snake_case**(与 SQLAlchemy 惯例一致);上表 `User` 沿用 `password_hash`/`plan_expire_at`/`quota_used`/`quota_limit`/`created_at` 均已 snake_case。Redis Key(§9.4)用冒号分隔的键名约定,不受此列名规则约束。

### 9.2 收费模式预留表(未来接入时建,归业务服务)
```
Subscription { id, userId, plan, status, provider, external_id, expire_at, created_at }
Payment      { id, userId, subscriptionId, amount, currency, status, provider, created_at }
```

### 9.3 Chroma Collections(归 AI 服务)
```
components { embedding, metadata:{type, tags, code, description} }
memory     { embedding, metadata:{userId, projectId, summary, createdAt} }
```

### 9.4 Redis Keys(约定)
```
# 业务服务
session:<token>           会话
ratelimit:<userId>        限流计数(令牌桶/滑动窗口)
sms:code:<phone>          短信验证码(预留)
auth:refresh:<userId>     Refresh Token
cache:user:<userId>:*     活跃用户常用数据(30min TTL)  ← §3.5
metric:*                  接口/服务实时指标计数(Stats)  ← §3.6
queue:write               异步写队列(Write-Behind)      ← §3.5
queue:error               错误队列(失败待重试)          ← §3.5
queue:error:dlq           死信队列(超限待人工)          ← §3.5

# AI 服务
queue:generate            生成任务队列(1-C,MVP 即上)
gen:progress:<traceId>   生成进度流(think/node/token,1-C)
gen:inflight             在途生成计数(限并发,1-C)
cache:models              模型列表缓存
cache:gen                prompt Embedding → 历史生成(5-C,随 M1 向量库)
```

---

## 10. 核心技术难点:生成结果预览

| 生成内容 | 预览方案 | 复杂度 |
|---------|---------|--------|
| 单文件 HTML(内联 CSS/JS + CDN) | **上传 COS → 独立域直链 `<iframe src>`(A2,§14.1)** | ⭐ 低(**MVP 主路径**) |
| 单文件 HTML(离线/兜底) | `<iframe srcdoc>` 直接注入 | ⭐ 低(降级兜底) |
| 含 React/多文件/需构建 | Sandpack(浏览器内打包) | ⭐⭐ 中(增强) |
| 需真实 Node 运行 | WebContainer / 服务端沙箱 | ⭐⭐⭐ 高(暂不做) |

**MVP 决策(v1.9 收口,对齐 §14.1 A2)**:生成的单文件 HTML **上传腾讯云 COS**(桶 `seedhtml-1252059540`,`ap-guangzhou`),预览走 **COS 默认访问域名直链**(`https://seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com/{base_path}/{user_id}/{site_id}/{version}/index.html`),`iframe src` 指向该 URL —— 与主站 `seedai.huzhen.net.cn` **天然跨域隔离**(§13.4 独立沙箱域效果 MVP 即达成)。COS 投递失败或本地离线时,**降级回 `iframe srcdoc`** 兜底。增强阶段上 Sandpack;`seedhtml.huzhen.net.cn` 自定义域 / CDN 为后续增强。

**预览安全(4-C,v1.9 收口)**:
- **同源隔离**:由 COS 独立域天然保证(不同源于主站),`iframe` 仍加 `sandbox="allow-scripts"`(不含 `allow-same-origin`),防生成脚本访问主站 Cookie/存储。
- **CSP(决策 #32)**:当前 COS **未开 CDN**,不在预览对象上打 `Content-Security-Policy` 响应头(采用备选),**仅靠 iframe `sandbox` 属性做隔离**;后续绑定 `seedhtml.huzhen.net.cn` + CDN 时再由回源重写注入严格 CSP(`default-src 'none'` + 仅白名单 CDN/字体)。
- srcdoc 兜底路径下,同样加 `sandbox="allow-scripts"` + 内联 CSP `<meta>`(离线场景无外链风险)。

---

## 11. 开发路线图

- **M0 打通闭环**:搭双服务骨架 —— 业务服务(鉴权入口 + SSE `/api/chat` 代理 + 结构化日志 + Redis `queue:generate` 透传)+ AI 服务(Orchestrator 编排 Planner→Coder→Reviewer + 思考链路事件 + 模型抽象层,经 Redis 队列 + Worker 产出,1-C)+ 前端 **WebLLM 首过旁路**(§3.9)→ DeepSeek/Qwen/HY3 流式生成单文件 HTML + 思考过程 → 业务服务经 SSE 转回前端(思考面板 + iframe 预览:**产物上传 COS 走独立域直链(A2,§14.1)**,失败降级 `srcdoc`)→ 提问结束断连。**M0 匿名跑通**(决策 #30:不做登录,用临时匿名 `user_id`)。Docker Compose 起 business-api / ai-service / mysql / redis / chroma。
- **M1 可用**:业务服务补全用户系统(账号密码 + JWT)→ 配额限流(plan/tier,Redis)→ **缓存读写(Cache-Aside,§3.5)** + 项目/对话持久化(MySQL)→ AI 服务基础 RAG(components 检索)。
- **M2 可分享 + 记忆**:分享只读链接 + memory 回写与检索 + **异步写队列 + 错误队列定时检查器(§3.5 对账)** + 生成队列/Worker + 迭代修改。
- **M3 增强**:手机短信登录、Designer 深度美化、Sandpack 多文件、版本历史、导出 zip、模板库运营、收费模式接入、多服务器扩容上线。
- **M4 运维监控后台**:RBAC 管理页(§3.6)→ 实时监控(各服务状态/性能/接口/访问量/模型用量, SSE 推送)→ 手动启停/扩缩容(Orchestrator)→ 统计系统仪表盘。

---

## 12. 决策与开放项(v1.0 已定 1–11;v1.1 新增 12–13 已定)

| # | 问题 | 决议 / 状态 |
|---|------|----------|
| 1 | JWT 细节 | Access 15min + **HttpOnly Cookie** 防 XSS;Refresh 存 Redis(7天,可吊销) ✅ |
| 2 | ORM | **SQLAlchemy(async)** ✅ |
| 3 | Embedding 模型 | **Qwen text-embedding**(M1 落地) ✅ |
| 4 | HY3 API/鉴权 | **OpenAI 兼容**:`base_url=https://tokenhub.tencentmaas.com/v1`,model=`hy3`;Key=`TokenHub_QCSLinkedRoleInInitialization`(已确认) ✅ |
| 5 | 视觉风格 | **浅色简洁** ✅ |
| 6 | 月度预算 | **不设硬预算**;free plan 每日生成配额默认 **50 次/天** ✅ |
| 7 | 提示词基调 | **通用中性** ✅ |
| 8 | 前端↔业务 传输 | **SSE(每问一连接,结束断连)** —— 回退方案(§3.7) ✅ |
| 9 | 是否独立网关 | **业务服务兼聚合入口**,MVP 不单开网关 ✅ |
| 10 | 缓存写策略 | 写后**更新** Redis;同步写=注册/改密/订阅,异步写=Message/UsageLog/Artifact/memory ✅ |
| 11 | 定时检查器 | **业务服务内后台任务**(MVP);M2 可抽独立 Worker;重试指数退避≤5次→DLQ ✅ |
| 12 | 思考链路持久化 | **MVP 不落库**(仅实时 SSE + Redis `logs:recent`);M1 落 MySQL `TraceLog` ✅ |
| 13 | 业务↔AI 传输形式 | **SSE 端到端**:业务 `httpx` 流式读 AI `/generate` SSE 透传事件 ✅ |
| 14 | 生成任务并发 | **Redis 队列 + Worker 池(1-C)**:SSE 入 `queue:generate`、Worker 消费、进度经 Redis 转发;可水平扩、重启不丢 ✅ |
| 15 | 模型故障转移 | **跨模型降级路由(2-C)**:预算/套餐选主模型 + 用户前端手选备用优先级,失败按序回退;前端显示「已降级」标记 ✅ |
| 16 | 产物可运行校验 | **静态分析 + LLM 自审(3-C)**:HTML 解析查错 + Reviewer 多轮自修(复用 LangGraph 回退) ✅ |
| 17 | 预览安全隔离 | **独立沙箱域 + 严格 CSP(4-C)**:iframe 独立子域 + `sandbox` 属性 + CSP ✅ |
| 18 | 生成结果缓存 | **语义相似缓存(5-C)**:Embedding + Chroma 复用;随 M1 向量库落地 ✅ |
| 19 | 权限分级(RBAC) | **三级**:`super_admin`(全部:后台查看+控制面执行+用户/角色管理)/ `admin`(仅查看后台)/ `user`(普通用户)。控制面由 admin-only 收紧为 super_admin-only;初始 super_admin 经 `SEED_SUPER_ADMIN` 注入(§3.6 矩阵 + §4.3) ✅ |
| 20 | 编排架构 | **Hybrid**(§5):弃纯多 Agent,改 **Router+Skill 外层分发(意图/鉴权/配额)+ generate_site 内层轻量多 Agent(Planner→Coder→Reviewer,共享上下文,Designer 降为 style 约束)**;其余 Skill 单次 LLM 直出。思考可视化与回退保留 ✅ |
| 21 | 架构演进候选 | **§5.7 五种形态梳理**(ReAct/Plan&Execute/Graph/Debate/Reflexion);推荐内层 generate_site 升级为 **Plan-and-Execute + Reflexion 叠加**,外层 Router+Skill 不变 ✅ |
| 22 | 预览域投递 | **腾讯云 COS 对象存储 + 独立域直链(A2)**:产物上传 COS(桶 `seedhtml-1252059540`,ap-guangzhou),经 COS 默认访问域名直链(`seedhtml.huzhen.net.cn` 自定义域作后续增强);iframe 跨域隔离天然满足;含 CAM 子账号降权(主账号暂代)+ CSP;生命周期暂不清理 ✅ |
| 23 | WebLLM 体验 | **单固定小模型 + 预取缓存(B1)**:锁定 Qwen2.5-7B/q4f16 档,首屏空闲预取权重到 Cache Storage,降首字延迟 ✅ |
| 24 | SSE 取消 | **客户端 abort + 服务端级联取消(C1)**:`AbortController` → 业务感知断连 → Redis `cancel:<traceId>` → AI Worker 中断生成,省 token ✅ |
| 25 | 多租户 Key 账本 | **次数配额 + 全局限频 + 成本账本(D1)**:复用每日配额 + 令牌桶限频 + `UsageLog`(user×provider×model)成本归集 ✅ |
| 26 | 生产密钥管理 | **Docker Secrets 注入(E1)**:生产密钥以文件挂载进容器,不进镜像/仓库;本地 `.env` 维持现状 ✅ |
| 27 | Schema 自动初始化 | **启动时自检建表(幂等)**:MySQL `create_all`(缺失才建,不删改已有)/ Chroma `get_or_create_collection` / Redis `ping` 探活;MVP 代码建表,生产可选 Alembic ✅ |
| 28 | 连接池管理 | **三库单例 + 连接池**:MySQL `create_async_engine(pool_size=10,max_overflow=20,pool_pre_ping,recycle=1800)`/ Redis `ConnectionPool(max_connections=50)`/ Chroma 单例 HttpClient+httpx `Limits(max_connections=50)` ✅ |
| 29 | 本地三域名 + hosts | **三域名本地映射(§3.11)**:`seedai.huzhen.net.cn`(前端主站)/ `seedapi.huzhen.net.cn`(后端 API)/ `seedhtml.huzhen.net.cn`(预览规划域,实际走 COS 默认域)均指 `127.0.0.1`;本机 hosts 已写入(2026-07-18 加 seedai/seedhtml,本版追加 seedapi);前端 `next.config.js` 代理改 `seedapi`、`CORS_ORIGINS`/`COOKIE_DOMAIN` 配合一致 ✅ |
| 30 | M0 范围(是否带登录) | **M0 匿名跑通闭环**(选 A):M0 不做登录,`User`/配额/`UsageLog` 延到 M1;COS 路径用临时匿名 `user_id`(如 `anon`/会话级 ID)。最快验证核心生成链路,符合路线图 M0=打通闭环 / M1=补全用户系统 ✅ |
| 31 | 模型默认降级序 | **HY3 → Qwen → DeepSeek**(用户指定):`FallbackRouter` 默认主模型 HY3,失败按序回退 Qwen、DeepSeek;用户前端仍可手选覆盖优先级(§13.2 / 决策 #15) ✅ |
| 32 | COS 预览 CSP | **不在 COS 对象打 CSP 响应头(选备选)**:当前未开 CDN,仅靠 iframe `sandbox="allow-scripts"` 做隔离;待绑定 `seedhtml.huzhen.net.cn`+CDN 后由回源重写注入严格 CSP(§10 / §14.1) ✅ |
| 33 | 多维度运营指标 | **6 大类已定义(§3.12)**:AI 效率(TTFT/耗时/吞吐/轮次/WebLLM 命中)、准确性(Reviewer 通过率/回滚率/评分)、消耗量(tokens/成本/省下 token)、回滚率、用户回馈(点赞/评分/评论)、健康度(成功率/取消率/降级率/模型分布);数据源 Trace/UsageLog/Feedback+Redis,`/admin` 看板呈现 ✅ |
| 34 | 对话回放存储 | **MySQL 双表 + Feedback(§3.13,方案1)**:`Trace`+`TraceEvent`+`Feedback` 落业务服务 MySQL,SQL 直查回放且与 §3.12 指标同源;**不**用文件 JSONL、**不**引 OpenTelemetry/Jaeger(MVP 过度);写入走 §3.5 异步队列 ✅ |
| 35 | 产物版本方案 | **方案1 线性递增(回滚即复制为新版本)✅ 已确认**:`Artifact.version` 整数自增(按 project 内顺序),「恢复此版本」= 把选中旧版内容复制成最新版(历史不丢、可追溯、可审计);前端项目详情页版本时间线 +「恢复此版本」按钮 |
| 36 | 对话评价 + 回归 | **每轮(Trace)结束后 1–10 分评分 + 点赞/点踩 + 可选评论(`Feedback.rating` 1–10)✅**:评分记 §3.12 用户回馈统计;同时经 `trace_id` 关联完整上下文,可导出为**模型回归/评测数据集**(按 rating 分层)支撑日后迭代回归测试 ✅ |
| 37 | RAG 命中率 | **方案2 阈值命中率 + 采纳率(✅ 已定)**:§3.12 新增第7类「RAG 检索质量」——检索命中率(最高相似度≥阈值 0.7 的 rag_retrieve 占比)、无结果率、上下文采纳率(可选);数据源 `TraceEvent(rag_retrieve)`,`/admin`「AI 质量」标签页呈现;比方案1 非空率更准、比方案3 LLM 评判更省 ✅ |
| 38 | Skill/Agent 引入机制 | **SkillRegistry 注册表 + 开闭原则(✅ v1.13)**:Router 按 `intent_tags` 从 `SkillRegistry` 匹配 Skill;新 Skill=注册 handler(不动 Router 核心),新多 Agent=注册 `is_graph=True` 的 LangGraph app(封装在 Skill 内);M2+ 可扫描 `skills/` 目录自动注册实现热插拔;Skill 触发词来自用户输入/UI 而非硬编码路由表 ✅ |
| 39 | 记忆系统 + 关键词来源 | **多层记忆闭环(✅ v1.13)** = 短期 `Trace` / 长期 Chroma `memory` / 组件 `components` / 语义缓存 `cache:gen` / 反馈 `Feedback`,读写闭环自我改进(§7.1 流程图);**关键词=混合检索**(稠密 embedding + 关键词/metadata 过滤),主来源 **A 原始 prompt + B Planner spec**,辅以 **C Router 意图 scope** 与 **D userId/projectId 过滤**(§7.2) ✅ |
| 40 | 前端实时传输 | **前端 ↔ 业务实时生成流 = SSE(不使用 WebSocket)**:每问一连接、结束断连(§3.7);业务 `httpx` 流式读 AI `/generate` 的 SSE 事件原样透传(§3.4 / #13);全链路无 WebSocket。前端取消用 `AbortController` + 服务端级联取消(#23 C1) ✅ |
| 41 | 前端技术栈 | **Vue 3 + Vite + TypeScript(纯静态 SPA)✅**:因前端与 business-api 已 REST/SSE 解耦、框架无关,替代原 Next.js;状态 Pinia、UI 库 Naive UI / Element Plus、浅色主题;§3.11 代理改 `vite.config.ts` 的 `server.proxy`(`/api`→`seedapi:8000`,dev `port=3000` 对齐 hosts/CORS)、管理后台改 Vue `/admin` 路由、docker `frontend` 改为 Vite 静态产物(+ 静态服务器);后端契约(OpenAPI/SSE)不变 ✅ |
| 42 | 工具(Tool)来源机制 | **ToolRegistry 注册表 + 开闭原则(方案2 ✅ v1.16)**:Skill=完整任务能力(§5.8 经 Router 分发),Tool=Skill 内 agent 经 function calling 调用的**原子操作**(向量检索/COS 上传/代码执行等);Tool 来源 = 内置 `@tool` 注册(如 `rag_retrieve`/`cos_upload`/`web_search`/`code_run`,MVP 全走这层)/ M2+ `tools/` 目录扫描运营用户贡献 / M3+ 第三方 MCP;明确 `rag_retrieve` **双入口**(对外 Skill + 对内 Tool,同一实现);与 SkillRegistry 同风格(注册表 + 开闭原则 + M2+ 目录扫描) ✅ |
| 43 | 本地 Skills/Tools 落地 | **§5.10 真代码落地(✅ v1.17)**:`backend/ai_service/app/` 下 `registry/`+`tools/`+`skills/`+`router.py`+`registries.py` 实现 §5.8/§5.9 契约;5 Skill(generate_site[LangGraph]/write_code/generate_doc/explain/rag_retrieve 双入口)+ 9 内置 Tool(成熟库:httpx/BeautifulSoup4/Chroma+Qwen embedding/cos-python-sdk-v5/Playwright/标准库),重依赖函数内懒加载、缺包优雅报错;bootstrap 全量注册 + `tools/` `skills/` 目录扫描热插拔(M2+);新增 `/skills` `/tools` `/registry` 调试端点 ✅ |

> HY3 Key 已确认(凭证):正式 Key = `TokenHub_QCSLinkedRoleInInitialization`;示例里的 `sk-unJV...` 仅 demo 测试用,已写入 `.env` 的 `HY3_API_KEY_DEMO` 作区分,生产不启用。

> 已定(用户确认):监控指标栈 = 自研轻量(MVP);管理后台 = Vue `/admin` 路由;控制面 = docker-compose 封装。详见 §3.6。

> 已后置(无需现在决定):短信服务商(M3 接真实服务商时定)、收费模式表结构(只留 plan 字段 + 配额抽象,接支付时再建)。

---

## 13. v1.2 优化方案采纳详情(1-C / 2-C / 3-C / 4-C / 5-C)

针对 v1.1 梳理的 5 个薄弱点,本版采纳各点的 **C 变体(用户指定)**,下方说明问题、采纳方案、落地改动与状态。

### 13.1 生成任务并发与阻塞 → 1-C:Redis 队列 + Worker 池
- **问题**:AI 服务单进程内长任务(几十秒~分钟)会占满 worker / 阻塞事件循环,并发一高即超时失败;MVP 单副本无排队。
- **采纳方案**:生成请求改为经 **Redis `queue:generate` + Worker 池**。
  - 流程:业务服务 SSE 到达 → AI 服务将任务(含 `trace_id`、modelId、上下文)入 `queue:generate` → Worker 池(可多副本)消费 → **Router 分发 Skill**(generate_site 走 LangGraph 轻量多 Agent)→ 进度(think/node/token)经 **Redis Stream/PubSub** 写入 `gen:progress:<traceId>` → SSE 端点订阅并转发前端 → `done` 结束。
  - 收益:解耦、可水平扩(Worker 独立加副本)、**重启不丢**(队列持久)、可优先级(本地首过优先级高)、并发上限=Worker 数。
- **落地改动**:§5.3 注释(原"M2"提前到 MVP);§8 阶段一即含 `queue:generate`;Redis Keys 增 `gen:progress:<traceId>`、`gen:inflight`(在途计数)。
- **状态**:✅ 采纳(MVP 即上)。

### 13.2 模型调用故障转移 → 2-C:跨模型降级路由 ✅ 已确认
- **问题**:单模型直连,某模型 429/超时/5xx 即整个生成失败,体验脆。
- **采纳方案**:在 `PROVIDERS` 之上加 **`FallbackRouter`**,按下方已确认子决策工作。
- **✅ 已确认子决策(用户指定)**:
  1. **触发策略 = 预算选模型 + 失败回退**:主模型由用户**套餐/层级(plan/tier)**决定(免费用便宜模型如千问,付费可用贵的如 DeepSeek/HY3);主模型 429/超时/5xx 时按优先级回退到备用。
  2. **主备顺序 = 用户前端手选**:模型选择器支持用户**手动勾选模型优先级(主 + 备)**,后端按用户指定顺序回退;预算/套餐影响默认可选项(如免费默认不可选最贵模型)。
  3. **降级可见 = 显示「已降级」**:发生回退时,思考事件带 `degraded: true`,前端在思考面板 / 产物区显示「已降级」小标记,透明可追溯。
- **落地改动**:§6 Provider 层加 `FallbackRouter`(读用户优先级 + plan 默认);AI 服务模型选择支持 fallback 配置;思考事件携带 `degraded` 标记(§5.5)。
- **状态**:✅ 采纳并锁定(§12 #15)。

### 13.3 产物可运行校验 → 3-C:静态分析 + LLM 自审
- **问题**:原 Reviewer 仅查 `<html` 与长度 >50,几乎不校验,生成页面可能白屏/报错。
- **采纳方案**:Reviewer 增强为两步 ——
  1. **静态分析**:HTML 解析,检查 doctype、标签闭合、script 语法粗筛、禁止危险 API(`eval` / 注入外部源的 `innerHTML` 等);
  2. **LLM 多轮自审**:复用 LangGraph 回退机制,Reviewer 给修改建议 → Coder 修订 → 再评审,≤3 轮。
- **收益**:介于"仅启发式(A)"与"无头浏览器渲染(B)"之间,**不引入 Playwright 重依赖**,显著降白屏率。
- **状态**:✅ 采纳。

### 13.4 预览安全隔离 → 4-C:独立沙箱域 + 严格 CSP
- **问题**:生成 HTML 直接 `srcdoc` 注入,可执行任意 JS,能访问父页/同域、发外链、数据外泄;分享链接被他人打开即有风险。
- **采纳方案**:
  - **独立子域**:预览跑在独立子域(如 `sandbox.seedai.com`),与主站**不同源**,srcdoc 脚本无法访问主站 Cookie/存储。
  - **`sandbox` 属性**:iframe 加 `sandbox="allow-scripts"`(不含 `allow-same-origin`),禁同源访问/表单/弹窗。
  - **严格 CSP**:`default-src 'none'`,仅允许白名单 CDN/字体(`script-src` 限定可信源)。
  - **落地节奏**:MVP 先上 `sandbox` 属性 + CSP(§10 更新);独立子域基础设施(域名/反代)在生产增强期落地。
- **状态**:✅ 采纳(渐进落地)。

### 13.5 生成结果缓存 → 5-C:语义相似缓存
- **问题**:仅缓存用户资料,没缓存"相同/相似需求的生成结果";重复提问重复烧 token。
- **采纳方案**:用 **Qwen text-embedding**(§7)对用户 prompt 向量化 → 查 Chroma `cache:gen` collection → 相似度 > 阈值直接复用/微调上次 HTML,省 token。
  - 阈值 + 用户可"强制重生成"覆盖;误复用风险靠阈值 + 用户覆盖兜底。
  - **依赖 M1 向量库**:需 Chroma 就绪(M1 做 RAG/components 检索时一并落地)。MVP 暂不做(或先以 5-B 精确 prompt 哈希作过渡,开销极低)。
- **状态**:✅ 采纳(随 M1 向量库落地)。

---

## 14. v1.6 复审优化方案采纳详情(A2 / B1 / C1 / D1 / E1)

针对 v1.5 + 域名环境(`seedai.huzhen.net.cn` 主站 / `seedhtml.huzhen.net.cn` 预览域)复审发现的 5 个未设计缺口,本版采纳 **A2(对象存储投递)/ B1(单模型预取)/ C1(级联取消)/ D1(key 账本)/ E1(Docker Secrets)**,下方说明问题、采纳方案、腾讯云 COS 配置清单与落地改动。

### 14.1 预览域投递 → A2:腾讯云 COS 对象存储 + 独立域直链 ✅ 采纳

- **问题**:MVP 当前是 `iframe srcdoc` 注入(§10),生成 HTML 的 JS 与主站同上下文,虽有 `sandbox` + CSP(4-C)缓解,但分享出去的页面仍挂在主站域下、无法 CDN 缓存与 TTL 清理、版本管理弱、存储随业务服务本地盘膨胀。
- **采纳方案**:生成产物改为**上传腾讯云 COS 对象存储**,经 **COS 访问域名直链**访问(当前用桶默认域名 `https://seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com`;`seedhtml.huzhen.net.cn` 自定义域名作后续可选增强)。`iframe` 改为 `<iframe sandbox="allow-scripts" src="{COS_PREVIEW_DOMAIN}/{user_id}/{site_id}/{version}/index.html">`,**天然跨域隔离**(COS 域与主站 `seedai.huzhen.net.cn` 不同源,同源攻击面隔离)。
- **已配置(用户 2026-07-18 提供,实际值)**:
  - 存储桶:`seedhtml-1252059540`
  - 地域:`ap-guangzhou`
  - 访问域名(预览实际地址):`https://seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com`
  - 密钥:**主账号**(用户明确"暂时先用主账号")
  - 生命周期:**无过期时间**(用户明确)
- **规划 / 可选增强(不影响 MVP,后续可补)**:
  1. **CAM 子账号最小权限密钥**:当前用主账号,生产前务必降为仅此桶读写的子账号并轮换主账号密钥(⚠️ 主账号泄露 = 全账号失陷)。
  2. **自定义预览域名**:把 `seedhtml.huzhen.net.cn` 以自定义源站 / CNAME 绑定到该桶,做品牌化独立域;需 TXT 归属验证 + HTTPS。
  3. **CDN 加速 + Referer 防盗链**:仅允许 `seedai.huzhen.net.cn` 引用、强制 HTTPS,降 COS 回源带宽成本。
  4. **CSP 注入**(必做,防生成页外泄):通过 COS 自定义 Header 或 CDN 回源重写,对预览对象返回 `Content-Security-Policy: default-src 'none'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'`。
- **我方代码需要的环境变量(写入 `.env` / `.env.production`)**:
  - `COS_SECRET_ID` / `COS_SECRET_KEY`(**已提供,已写入 `.env` / `.env.production`,主账号暂代**)
  - `COS_BUCKET=seedhtml-1252059540`(已定)
  - `COS_REGION=ap-guangzhou`(已定)
  - `COS_PREVIEW_DOMAIN=https://seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com`(已定)
  - `COS_BASE_PATH=previews`(对象前缀)
  - `COS_TTL_DAYS=0`(无过期;保留字段以便后续收紧)
- **落地改动**:
  - 新增 `ai_service/app/preview_pusher.py`:用 `cos-python-sdk-v5` 上传产物到 `cos://{bucket}/{base_path}/{user_id}/{site_id}/{version}/index.html`,返回 `{COS_PREVIEW_DOMAIN}/.../index.html`。
  - 生成完成(Reviewer 3-C 通过)后,Orchestrator 调 `preview_pusher` 投递,URL 写入 `Artifact.preview_url`(MySQL)与 SSE `done` 事件。
  - 前端 `iframe` 的 `src` 由 `srcdoc` 改为该 URL;`vite.config.ts` 不再为预览做 `/api` 代理。
  - 安全:同源隔离由独立域天然保证(§13.4 的独立沙箱域从"生产增强期"提前落地);`sandbox="allow-scripts"` 仍保留;CSP 按上方第 4 点注入。
- **状态**:✅ 采纳(MVP 即上;桶 / 域名 / 地域 / 密钥均已定并写入 `.env` / `.env.production`,待「可以写代码了」后写 `preview_pusher.py`)。

### 14.2 前端 WebLLM 体验 → B1:单固定小模型 + 预取缓存 ✅ 采纳

- **问题**:§3.9 留了 A(本地首过+可选云端精修)/ B(本地仅 Planner)两种定位,模型选择与权重加载策略未定,首开需从 CDN 拉 2–5GB 权重,体感慢、易放弃。
- **采纳方案**:**锁定单一固定小模型**(推荐 `Qwen2.5-7B-Instruct-q4f16_1-MLC` 或 `Llama-3-8B-Instruct-q4f16_1-MLC`,7–8B 量化档,单文件 HTML 生成质量与体积平衡),并在**首屏空闲 / 用户 hover 模型选择器时预取权重**到浏览器 Cache Storage,显著降低首字延迟。
- **落地改动**:§3.9 的 MVP 定位锁定为 **A + 单模型 + 预取**;前端 WebLLM 封装新增 `warmup()`(空闲预取)、`loadModel()`(命中缓存秒开);模型选择器「本地 WebLLM(快速)」固定为该模型;不支持 WebGPU 时选项灰显并提示回退云端。
- **状态**:✅ 采纳(随 M0 WebLLM 旁路落地)。

### 14.3 SSE 取消 → C1:客户端 abort + 服务端级联取消 ✅ 采纳

- **问题**:§3.7 只定义了"每问一连接、结束断连",但用户中途点"停止"时,前端断开后**服务端 / AI Worker 仍在跑完整生成**,白白烧 token、占 Worker 槽位。
- **采纳方案**:**级联取消** —— 前端 `AbortController.abort()` 断开 SSE → 业务服务 SSE 端点感知连接关闭 → 经 Redis PubSub 广播 `cancel:<traceId>`(或复用 httpx 客户端关闭触发)→ AI 服务对应 Worker 监听到取消标记,**中断当前生成**(asyncio task cancel / LangChain callback 抛 `GenerationAborted`),释放槽位并停止计费。`done` 前可多发一帧 `aborted: true`。
- **落地改动**:§3.7 增补"主动取消"事件与级联链路;AI 服务 Worker 轮询 `cancel:<traceId>`;思考事件增 `aborted` 标记;前端停止按钮接 `AbortController`。
- **状态**:✅ 采纳(随 M0 SSE 落地)。

### 14.4 多租户 Key 账本 → D1:次数配额 + 全局限频 + 成本账本 ✅ 采纳

- **问题**:§12 #6 只有 free plan 每日 50 次配额,缺(1)突发限频(防单用户瞬间打爆上游)、(2)按模型/按租户的成本归集(未来计费/审计无据)。
- **采纳方案**:在业务服务加三层账本 ——
  1. **次数配额**:复用 plan/tier 每日生成次数(Redis 计数,已有);
  2. **全局限频**:令牌桶(如全局 20 生成/秒),防突发压垮上游与 Worker;
  3. **成本账本**:按 `user_id × provider × model` 记录 token 消耗(入 MySQL `UsageLog` 或 Redis 聚合落库),供未来计费、异常用量告警、模型成本对比。
- **落地改动**:§3.5 增 Redis Keys `ratelimit:global:gen`、`quota:daily:{user_id}`;新增 `UsageLog` 表(§9.2 收费预留表扩展 `provider`/`model`/`prompt_tokens`/`completion_tokens` 字段);AI 服务生成结束回写成本账本。
- **状态**:✅ 采纳(M1 配额阶段 + 成本账本扩展)。

### 14.5 生产密钥管理 → E1:Docker Secrets 注入 ✅ 采纳

- **问题**:`.env` / `.env.production` 内明文存 HY3 / DeepSeek / COS / MySQL / Redis 密钥(虽 gitignored),但明文进镜像 / 进容器环境变量仍可被 `docker inspect` 读出,生产有泄露风险。
- **采纳方案**:生产部署改用 **Docker Secrets**(Swarm / Compose `secrets:`)或等效**密钥管理注入**(腾讯云 SSM / 主机文件 `chmod 600`),密钥以文件挂载进容器(`/run/secrets/xxx`),业务代码读文件而非环境变量;**绝不进镜像层、绝不进仓库**;`.env.production` 仅留非敏感配置(域名 / region / TTL),敏感值改由 secrets 注入。
- **落地改动**:新增 `docker-compose.prod.yml` 的 `secrets:` 段;`config.py` 增加「secrets 文件优先、环境变量兜底」读取链;文档 §8 部署说明补「生产密钥用 Docker Secrets」一节。
- **状态**:✅ 采纳(生产部署阶段落地;本地 `.env` 维持现状满足开发)。

---

## 附:下一步
文档 v1.5 进入**「可编码」**(2-C 子决策已确认,三级权限已定,架构演进候选已定)。M0 编码核对以下与已写草稿的差异:
- **传输**:草稿用 SSE/HTTP,事件类型需补齐 `think`/`node`(§3.7);**每问一连接、结束断连**生命周期保留。
- **WebLLM**:新增前端 **WebLLM 首过旁路**(§3.9),模型选择器加「本地 WebLLM」选项,本地失败无缝回退云端 SSE。
- **生成并发**:AI 服务改 **Redis 队列 + Worker 池(1-C)**,SSE 入队、进度经 Redis 转发(草稿是同步直出)。
- **产物校验**:Reviewer 加 **静态分析 + LLM 自审(3-C)**(草稿仅查 `<html`+长度)。
- **预览安全**:iframe 加 `sandbox` 属性 + 严格 CSP(4-C 的 MVP 部分)(草稿裸 srcdoc)。
- **模型降级**:Provider 层加 **`FallbackRouter`(2-C)**,预算选主模型 + 用户手选备用优先级 + 失败回退 + 「已降级」标记。
- **Router+Skill / 日志 / JWT / 视觉 / 提示词**:同 v1.1 已列(思考链路事件、trace_id 日志、HttpOnly Cookie、浅色、中性)。
- **权限三级**:鉴权依赖由单 `require_admin` 补为 `require_admin`(super_admin+admin,开放只读后台)与 `require_super_admin`(仅 super_admin,开放控制面+用户管理);草稿的 `User.role` 由 admin/user 扩为 super_admin/admin/user;初始 super_admin 经 `SEED_SUPER_ADMIN` 种子注入(§3.6 / §4.3)。

- **架构演进(§5.7)**:内层 generate_site 由线性 Planner→Coder→Reviewer 升级为 **Plan-and-Execute(先计划再执行)+ Reflexion(失败反思重写)**;外层 Router+Skill 不变。

M0 任务:SSE 传输 + Router+Skill 编排(hybrid)+ WebLLM 首过 + 结构化日志 + Redis 队列 Worker + `FallbackRouter` + 双服务骨架 + 模型抽象层,跑通"登录→提问(SSE/本地首过)→Router 分发→(generate_site 多 agent 思考可见)→预览→断连"闭环。
