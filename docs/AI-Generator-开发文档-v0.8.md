# AI 生成器平台 — 开发文档 & 架构方案 (v0.8)

> 版本:v0.8(草稿,持续共创)
> 最后更新:2026-07-17
> 一句话定位:一个通过 AI 对话即可生成**网站/前端页面**(后续扩展文档、代码)的平台;微服务架构(业务服务 + 核心 AI 服务),支持多模型自选、多 agent 编排、用户系统、RAG 增强,可自托管、可横向扩容。

> 变更史:
> - v0.2:定位升级为可自托管/可扩容;定多模型、MySQL+Chroma+Redis、Next.js 前端 + 独立后端。
> - v0.3:后端定 FastAPI (Python),模型抽象层改 Python 实现。
> - v0.4:新增多 Agent 编排层(Supervisor + LangGraph)。
> - v0.5:新增用户系统(账号密码 + 短信预留);plan/tier 配额抽象与订阅表预留位。
> - v0.6:短信与收费改为「先预留、后期再加」,MVP 只做账号密码。
> - v0.7:微服务拆分(业务服务 + 核心 AI 服务),内网调用,模型 Key 仅存 AI 服务。
> - v0.8:**引入缓存与异步持久化** —— 活跃用户常用数据 Cache-Aside 进 Redis(30min TTL);写先同步更 Redis 再异步落 MySQL;失败进错误队列,定时检查器重试对账。
> - v0.3:后端定 FastAPI (Python),模型抽象层改 Python 实现。
> - v0.4:新增多 Agent 编排层(Supervisor + LangGraph)。
> - v0.5:新增用户系统(账号密码 + 短信预留);plan/tier 配额抽象与订阅表预留位。
> - v0.6:短信与收费改为「先预留、后期再加」,MVP 只做账号密码。
> - v0.7:**微服务拆分** —— 拆为「业务需求服务器」与「核心 AI 服务器」两个独立服务,内网调用,模型 Key 仅存 AI 服务。

---

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
| 前端 | **Next.js (App Router) + TypeScript** | 对话 UI、预览面板、模型选择器、登录页 |
| 前端 UI | Tailwind CSS + shadcn/ui | 快速搭建、风格统一 |
| 业务服务 | **FastAPI (Python)** | 鉴权/JWT、用户、项目、消息、分享、配额;唯一对外入口 |
| 核心 AI 服务 | **FastAPI (Python)** | 模型抽象层、LangGraph Supervisor+Agents、RAG;独享模型 Key |
| Agent 编排 | **LangGraph (Python)** | Supervisor 模式,状态图 + 条件边(评审回退) |
| 认证 | **JWT(Access) + Refresh 存 Redis**;密码 bcrypt/argon2 | 无状态令牌利于横向扩容 |
| 短信 | **SMSService 抽象层(预留,先不接)** | 同模型抽象层思路;开放时再接真实服务商 |
| AI 编排 | OpenAI SDK(Python)/自封装流式 | 统一流式输出(async generator) |
| 模型 | **DeepSeek + Qwen + HY3(前端可选,可扩展)** | 模型抽象层,见 §6 |
| 关系库 | **MySQL** | 业务数据(用户/项目/对话/产物),归业务服务 |
| 向量库 | **Chroma** | 组件/记忆检索,归 AI 服务 |
| 缓存/队列 | **Redis** | 会话/限流(业务)、队列/缓存(AI) |
| ORM | ⬜ SQLAlchemy(推荐,Python 原生) / Prisma(可选) | 见 §12 |
| 部署 | **Docker Compose(本地) → 多服务器扩容** | 见 §8 |

> **前后端跨语言说明**:前端 TS、后端 Python(双服务),通过 HTTP/SSE 解耦。接口契约以 **OpenAPI(Swagger)** 为准;本地 Next.js dev 配 `rewrites` 将 `/api/*` 代理到业务服务(`localhost:8000`),业务服务再内网调用 AI 服务(`localhost:8001`)。模型 Key 与短信 Key 只在 AI 服务/配置内,前端与业务服务均不接触。

### 3.4 服务拆分(微服务架构)

将后端拆为两个独立部署的服务,职责清晰、可独立扩缩容:

```
┌──────────────────────────────────────────────────────────────┐
│  前端层  Next.js (TS)  对话 UI · 预览 · 登录 · 模型选择器         │
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
│  模型抽象层(Provider) · LangGraph Supervisor+Agents · RAG       │
│  Chroma(向量) · Redis(队列/缓存) · → DeepSeek/Qwen/HY3          │
└───────────────────────────────────────────────────────────────┘
```

**关键设计**
- **业务需求服务器**:唯一对外暴露的入口。负责鉴权、用户、项目管理、对话消息、分享链接、配额限流、用量计量。**不持有模型 API Key**。生成类请求转发给 AI 服务。
- **核心 AI 服务器**:仅内网可达(网络隔离 / 不暴露公网)。负责模型抽象层(Provider 注册表)、LangGraph Supervisor + agents、RAG(Chroma)、流式生成。**独享模型 API Key**。
- **服务间通信(MVP)**:业务服务 → AI 服务走**内网 HTTP/SSE 同步代理**(业务把客户端 SSE 请求透传给 AI 服务,再把流转回前端)。后续可加 Redis 队列做异步生成(与 M2 队列一致)。
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

**关键取舍(⬜ 见 §12)**
- 哪些写**同步**(关键:注册 / 改密 / 订阅付费)、哪些**异步**(高频追加:Message / UsageLog / Artifact / memory 回写)。
- 写成功后对 Redis 取「更新(推荐,读一致)」还是「仅失效」。
- 定时检查器放在业务服务内(APScheduler,MVP 简单)还是独立 Worker 服务(M2 扩容)。

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
  - ⬜ 待定:Access Token 时长、是否用 HttpOnly Cookie 防 XSS(见 §12)。

### 4.3 关键流程(MVP:账号密码)
- 注册:提交 username/email + password → 哈希入库(默认 plan=`free`)→ 签发 JWT(业务服务)。
- 登录:校验 password_hash → 签发 JWT。
- 生成请求:前端带 JWT → 业务服务校验 → 透传 modelId + 上下文给 AI 服务(内网)→ 流式返回。
- 限流:登录/注册/生成接口用 Redis 令牌桶限频,防 Key 盗刷。

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

## 5. 多 Agent 编排层(LangGraph · Supervisor 模式)

### 5.1 模式选择(已定)
采用 **Supervisor(主管)模式**:一个 Supervisor agent 把"生成网站"拆解为子任务,调度专职 worker agent 协作,并汇总结果。选 **LangGraph** 实现 —— 其 `StateGraph` + 条件边天然支持"Reviewer 不合格 → 回退 Coder 重生成"的循环。

### 5.2 Agent 角色(运行于核心 AI 服务)
| Agent | 职责 |
|-------|------|
| **Supervisor** | 理解需求、决定调用哪些 worker、编排顺序、汇总 |
| **Planner** | 需求拆解、页面结构规划、技术选型 |
| **Coder** | 生成单文件 HTML/CSS/JS(或 React 片段) |
| **Designer** | 视觉风格、配色、排版美化 |
| **Reviewer** | 校验代码可运行、查错、按需打回 |

### 5.3 LangGraph 状态图(伪代码)
```python
from langgraph.graph import StateGraph, END

class GenState(TypedDict):
    messages: list[dict]
    spec: str
    html: str
    reviews: int
    passed: bool

def supervisor(state): ...
def planner(state): ...
def coder(state): ...
def designer(state): ...
def reviewer(state): ...

graph = StateGraph(GenState)
graph.add_node("supervisor", supervisor)
graph.add_node("planner", planner)
graph.add_node("coder", coder)
graph.add_node("designer", designer)
graph.add_node("reviewer", reviewer)

graph.add_edge("planner", "coder")
graph.add_edge("coder", "designer")
graph.add_edge("designer", "reviewer")
graph.add_conditional_edges("reviewer", route_after_review,
    {"pass": END, "retry": "coder"})
graph.add_conditional_edges("supervisor", route_supervisor,
    {"plan": "planner", "done": END})
app = graph.compile()
```

**要点**
- 各 agent 内部统一走 §6 的 `PROVIDERS[model_id]` 抽象层(在 AI 服务内)。
- RAG(§7)在 Planner/Coder 前注入组件与记忆上下文(Chroma,AI 服务内)。
- `reviews` 设上限(如 ≤3),避免评审回退死循环。
- 长耗时生成后续可把 `app.invoke` 放入 Redis 队列 + Worker(M2)。

### 5.4 服务间调用关系
```
前端 → 业务服务 /api/generate (校验 JWT, 记用量)
         └─ 内网 → AI 服务 /generate (SSE)
                     └─ Supervisor(LangGraph) → {Planner,Coder,Designer,Reviewer}
                           ├─ 模型抽象层 → DeepSeek/Qwen/HY3
                           ├─ Chroma(RAG)
                           └─ Redis(队列/缓存)
         └─ 流式 HTML 经业务服务转回前端 → iframe 预览
```

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

# AI 服务对外暴露
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

⬜ 待定:Embedding 模型用哪家(可复用 Qwen/DeepSeek embedding 接口,或开源 bge)。

---

## 8. 部署方案

**阶段一 — 本地(Docker Compose)**
`docker-compose.yml` 起全套(各自独立容器):
- `frontend`(Next.js)
- `business-api`(业务服务:FastAPI,持 DB/JWT 配置;含缓存读写与错误队列定时检查器,MVP 内置)
- `ai-service`(AI 服务:FastAPI + LangGraph,持模型 Key / Chroma 配置)
- `mysql`、`chroma`、`redis`

**阶段二 — 多服务器扩容**
- 前端、业务服务、AI 服务各自镜像化,可分别多实例部署。AI 服务算力吃紧时**单独加副本**(微服务拆分的核心收益)。
- 数据层独立部署或云托管。
- 前置 Nginx / 负载均衡:仅暴露前端与业务服务,AI 服务留在内网。
- 会话/限流/Refresh Token 在 Redis,保证业务服务无状态、可水平扩展;AI 服务亦无状态(状态在 Chroma/Redis),可水平扩展。
- 长耗时生成走 Redis 队列 + 独立 Worker(M2,可挂在 AI 服务侧);错误队列的定时检查器可随业务服务或独立 Worker 部署。

---

## 9. 数据模型(初稿)

### 9.1 MySQL(归业务服务)
```
User      { id, username, email(可空), phone(可空,唯一), password_hash,
            plan(默认 'free'), plan_expire_at(可空),
            quota_used, quota_limit, created_at }
Project   { id, userId, title, share_id(可空), created_at, updated_at }
Message   { id, projectId, role(user/assistant), content, modelId, createdAt }
Artifact  { id, projectId, version, files(JSON), created_at }
UsageLog  { id, userId, modelId, tokens_in, tokens_out, created_at }
```

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
queue:write               异步写队列(Write-Behind)      ← §3.5
queue:error               错误队列(失败待重试)          ← §3.5
queue:error:dlq           死信队列(超限待人工)          ← §3.5

# AI 服务
queue:generate            生成任务队列(M2)
cache:models              模型列表缓存
```

---

## 10. 核心技术难点:生成结果预览

| 生成内容 | 预览方案 | 复杂度 |
|---------|---------|--------|
| 单文件 HTML(内联 CSS/JS + CDN) | `<iframe srcdoc>` 直接注入 | ⭐ 低(MVP 首选) |
| 含 React/多文件/需构建 | Sandpack(浏览器内打包) | ⭐⭐ 中(增强) |
| 需真实 Node 运行 | WebContainer / 服务端沙箱 | ⭐⭐⭐ 高(暂不做) |

**MVP 决策**:先生成单文件 HTML,iframe srcdoc 预览;增强阶段上 Sandpack。

---

## 11. 开发路线图

- **M0 打通闭环**:搭双服务骨架 —— 业务服务(鉴权入口 + 模型选择器代理)+ AI 服务(模型抽象层 + LangGraph Supervisor 最简链路 Planner→Coder→Reviewer)→ DeepSeek/Qwen/HY3 流式生成单文件 HTML → 业务服务 SSE 转回前端 → iframe 预览。Docker Compose 起 business-api / ai-service / mysql / redis / chroma。
- **M1 可用**:业务服务补全用户系统(账号密码 + JWT)→ 配额限流(plan/tier,Redis)→ **缓存读写(Cache-Aside,§3.5)** + 项目/对话持久化(MySQL)→ AI 服务基础 RAG(components 检索)。
- **M2 可分享 + 记忆**:分享只读链接 + memory 回写与检索 + **异步写队列 + 错误队列定时检查器(§3.5 对账)** + 生成队列/Worker + 迭代修改。
- **M3 增强**:手机短信登录、Designer 深度美化、Sandpack 多文件、版本历史、导出 zip、模板库运营、收费模式接入、多服务器扩容上线。

---

## 12. ⬜ 开放问题汇总(待拍板)

1. JWT 细节:Access Token 时长、是否用 HttpOnly Cookie 防 XSS?
2. ORM:SQLAlchemy(推荐)还是 Prisma(可选)?
3. Embedding 模型用哪家?(§7)
4. HY3 的 API 文档/鉴权方式?(写 Provider 需要)
5. 视觉风格 / 配色参考?(影响前端 UI 起手)
6. 模型调用月度预算?(影响限流与默认模型策略)
7. 各 agent 的 system prompt 基调 / 目标用户画像?(影响生成风格)
8. 服务间通信:内网 SSE 同步代理(MVP 推荐)还是现在就上 Redis 异步队列?
9. 是否引入独立 API 网关(Nginx/网关)还是业务服务兼做聚合入口(MVP 推荐)?
10. 缓存写策略:写成功后对 Redis「更新(推荐)」还是「仅失效」?哪些写同步(注册/改密)vs 异步(消息/用量日志)?
11. 定时检查器落地:业务服务内 APScheduler(MVP)还是独立 Worker 服务(M2)?错误队列重试次数/退避策略?

> 已后置(无需现在决定):短信服务商(M3 接真实服务商时定)、收费模式表结构(只留 plan 字段 + 配额抽象,接支付时再建)。

---

## 附:下一步
确认 §12 剩余问题(尤其 HY3 API + 服务间通信方式)后,把文档升到 v1.0,然后从 M0 开始:搭 `docker-compose.yml` + 双服务骨架(业务服务 + AI 服务)+ 模型抽象层 + LangGraph Supervisor 最简链路,跑通"登录→对话→多 agent 生成→预览"最小闭环。
