# AI 生成器平台 — 开发文档 & 架构方案 (v0.6)

> 版本:v0.6(草稿,持续共创)
> 最后更新:2026-07-17
> 一句话定位:一个通过 AI 对话即可生成**网站/前端页面**(后续扩展文档、代码)的平台;支持多模型自选、多 agent 编排、用户系统(账号密码,短信/收费预留)、RAG 增强,可自托管、可横向扩容。

> 变更史:
> - v0.2:定位升级为可自托管/可扩容;定多模型、MySQL+Chroma+Redis、Next.js 前端 + 独立后端。
> - v0.3:后端定 FastAPI (Python),模型抽象层改 Python 实现。
> - v0.4:新增多 Agent 编排层(Supervisor + LangGraph)。
> - v0.5:新增用户系统(账号密码 + 手机号短信验证码登录);引入 plan/tier 配额抽象与订阅表预留位。
> - v0.6:**范围收敛** —— 短信登录与收费模式均改为「先预留、后期再加」:M1 只做账号密码登录,SMSService 抽象层与订阅表预留位保留但不接真实服务商/支付;MVP 不卡在短信签名审核。

---

## 1. 项目概述

### 1.1 产品定位
- **形态**:类 v0.dev / bolt.new / Lovable 的"对话即生成"平台。
- **使用范围**:自己为主 + 小范围分享;用户系统支持账号密码与手机短信;架构预留收费模式扩展点。
- **首要产物**:网站 / 前端页面(带实时预览、可分享、可迭代)。
- **后续扩展**:文档生成、代码工程生成(架构预留,不在 MVP)。

### 1.2 核心价值
自然语言描述需求 → 多 agent(Supervisor 调度 + RAG 增强)生成可运行网页 → 实时预览 → 对话迭代 → 分享/导出。

---

## 2. 核心功能范围

### 2.1 MVP(第一阶段必须有)
| 模块 | 说明 |
|------|------|
| 用户系统 | 账号密码注册/登录(JWT 鉴权);手机短信 / 收费模式预留扩展点 |
| 对话生成 | 输入需求,流式返回生成过程与结果 |
| 多模型自选 | 前端可切换 DeepSeek / Qwen / HY3,后端统一调度 |
| 多 Agent 编排 | Supervisor 调度专职 agent 协同生成(见 §5) |
| 实时预览 | 生成页面在 iframe / 沙箱中即时渲染 |
| 迭代修改 | 基于已有结果继续对话("把按钮改成蓝色") |
| RAG 增强 | 组件/模板检索 + 历史对话记忆(见 §7) |
| 用量限流 | 按 plan/tier 配额限制,防 Key 被刷 |
| 分享链接 | 生成只读预览链接 |

### 2.2 第二阶段(增强)
版本历史与回滚 / 代码在线编辑 / 导出 zip / 一键部署 / 模板库运营 / 收费模式接入

### 2.3 暂不做(已明确后置)
- **手机短信验证码登录**:MVP 不做,仅保留 `SMSService` 抽象层与 Redis 验证码结构(见 §4.4);待开放给更多用户时再接真实短信服务商(需签名+模板审核)。
- **完整订阅计费与支付**:当前只做 plan/tier 配额抽象与表预留位(见 §4.5),不实现支付;未来接微信/支付宝/Stripe 只增表、改配额取数一处。
- 复杂后端逻辑生成 / 团队协作权限体系

---

## 3. 技术选型

| 层 | 选型 | 说明 |
|----|------|------|
| 前端 | **Next.js (App Router) + TypeScript** | 对话 UI、预览面板、模型选择器、登录页 |
| 前端 UI | Tailwind CSS + shadcn/ui | 快速搭建、风格统一 |
| 后端 | **独立服务 · FastAPI (Python)** | AI/向量生态成熟,Chroma Python SDK 原生支持 |
| Agent 编排 | **LangGraph (Python)** | Supervisor 模式,状态图 + 条件边(评审回退) |
| 认证 | **JWT(Access) + Refresh 存 Redis**;密码 bcrypt/argon2 | 无状态令牌利于横向扩容 |
| 短信 | **SMSService 抽象层(预留,先不接)** | 同模型抽象层思路,见 §4.4;开放时再接真实服务商 |
| AI 编排 | OpenAI SDK(Python)/自封装流式 | 统一流式输出(async generator) |
| 模型 | **DeepSeek + Qwen + HY3(前端可选,可扩展)** | 模型抽象层,见 §6 |
| 关系库 | **MySQL** | 用户/项目/对话/产物/订阅预留 |
| 向量库 | **Chroma** | 组件模板检索 + 对话记忆,见 §7 |
| 缓存/队列 | **Redis** | 缓存、限流、会话、短信码、Refresh Token、异步队列 |
| ORM | ⬜ SQLAlchemy(推荐,Python 原生) / Prisma(可选) | 见 §12 |
| 部署 | **Docker Compose(本地) → 多服务器扩容** | 见 §8 |

> **前后端跨语言说明**:前端 TS、后端 Python,通过 HTTP/SSE 解耦。接口契约以 **OpenAPI(Swagger)** 为准(FastAPI 自动生成);本地 Next.js dev 配 `rewrites` 将 `/api/*` 代理到 FastAPI(`localhost:8000`),模型 Key 与短信 Key 只在后端。

---

## 4. 用户系统与鉴权(已定方向)

### 4.1 需求(MVP 与后期)
- **MVP(现在做)**:账号密码注册/登录,JWT 鉴权,按 plan/tier 配额限流。
- **后期(开放时再做)**:手机号 + 短信验证码注册/登录/找回 —— 仅先预留 `SMSService` 抽象层与 Redis 验证码结构,不接真实服务商(避免短信签名审核阻塞 MVP)。
- **收费模式**:设计为未来可平滑接入 —— 用户带「套餐/层级(plan)」概念,配额由套餐决定;计费/订阅相关表预留扩展位(见 §4.5)。

### 4.2 认证方式
- **密码**:服务端用 `argon2` / `bcrypt` 哈希存储(绝不存明文),使用 `passlib` 或 `argon2-cffi`。
- **短信验证码**:通过 `SMSService` 抽象层调用短信服务商(见 §4.4);验证码存 Redis(5 分钟时效 + 限频防刷)。
- **会话令牌**:**JWT(Access Token 短期有效) + Refresh Token 存 Redis**(可吊销、支持多端)。无状态 Access Token 利于横向扩容;Redis 仍保留会话/限流/短信码状态。
  - ⬜ 待定:Access Token 时长、是否用 HttpOnly Cookie 防 XSS(见 §12)。

### 4.3 关键流程(MVP:账号密码)
- 注册(账号密码):提交 username/email + password → 哈希入库(默认 plan=`free`)→ 签发 JWT。
- 登录:校验 password_hash → 签发 JWT(Access + Refresh 存 Redis)。
- 限流:登录/注册接口用 Redis 令牌桶限频(同 §9 限流机制),防 Key 盗刷。
- *(后期)手机短信流程*:`POST /sms/send` → Redis 存 code(5min)→ `POST /auth/phone-login`(校验)→ 签发 JWT;找回密码同理。结构已预留,接真实服务商时实现。

### 4.4 抽象层设计(SMSService,预留)
与模型抽象层同思路:新增 Provider 即可接入不同短信商。MVP 阶段仅实现 `MockSMSService`(验证码打日志,不真实发送);开放时再补 `AliyunSMSService` / `TencentSMSService`,并在配置切换,业务代码无需改动。
```python
class SMSService(ABC):
    @abstractmethod
    async def send_code(self, phone: str, code: str) -> None: ...

SMS_PROVIDERS: dict[str, SMSService] = {
    "mock":    MockSMSService(),          # MVP:打印到日志,不真实发送
    # "aliyun":  AliyunSMSService(...),   # 开放时启用
    # "tencent": TencentSMSService(...),  # 开放时启用
}
```

### 4.5 为收费模式预留的扩展点
- `User.plan`(默认 `free`)+ `plan_expire_at`;当前免费层写死 quota,取数集中在 `get_quota()` 依赖。
- 后续加 `Subscription`(plan, status, provider, external_id, expire_at)、`Payment/Invoice` 表即可接支付(微信/支付宝/Stripe)。
- 配额校验集中于一处(如 FastAPI 依赖 `quota_guard`),未来从「按 plan 查 quota」改为「查 Subscription」只改该依赖。
- **不现在实现支付**,但表结构与配额抽象已就位,未来改造成本低。

---

## 5. 多 Agent 编排层(LangGraph · Supervisor 模式)

### 5.1 模式选择(已定)
采用 **Supervisor(主管)模式**:一个 Supervisor agent 把"生成网站"拆解为子任务,调度专职 worker agent 协作,并汇总结果。选 **LangGraph** 实现 —— 其 `StateGraph` + 条件边(`add_conditional_edges`)天然支持"Reviewer 不合格 → 回退 Coder 重生成"的循环,可控性最强。

### 5.2 Agent 角色
| Agent | 职责 | 输入 | 输出 |
|-------|------|------|------|
| **Supervisor** | 理解需求、决定调用哪些 worker、编排顺序、汇总 | 用户需求 + 上下文 | 调度决策 + 最终答复 |
| **Planner** | 需求拆解、页面结构规划、技术选型 | 需求 | 结构化规格(spec) |
| **Coder** | 生成单文件 HTML/CSS/JS(或 React 片段) | spec + RAG 组件 | 网页代码 |
| **Designer** | 视觉风格、配色、排版美化 | 草稿网页 | 美化后网页 |
| **Reviewer** | 校验代码可运行、查错、按需打回 | 网页 | 通过 / 问题清单 |

### 5.3 LangGraph 状态图(伪代码)
```python
from langgraph.graph import StateGraph, END

class GenState(TypedDict):
    messages: list[dict]
    spec: str
    html: str
    reviews: int          # 已迭代次数
    passed: bool

def supervisor(state): ...   # 决定下一步:planner / 汇总 / END
def planner(state): ...       # 写 spec,调用模型抽象层
def coder(state): ...         # 生成 html,调用模型抽象层
def designer(state): ...      # 美化,调用模型抽象层
def reviewer(state): ...      # 校验,置 passed / 写问题

graph = StateGraph(GenState)
graph.add_node("supervisor", supervisor)
graph.add_node("planner", planner)
graph.add_node("coder", coder)
graph.add_node("designer", designer)
graph.add_node("reviewer", reviewer)

graph.add_edge("planner", "coder")
graph.add_edge("coder", "designer")
graph.add_edge("designer", "reviewer")
# 条件边:评审通过→END;否则回退 coder(带迭代上限,防死循环)
graph.add_conditional_edges("reviewer", route_after_review,
    {"pass": END, "retry": "coder"})
graph.add_conditional_edges("supervisor", route_supervisor,
    {"plan": "planner", "done": END})

app = graph.compile()
```

**要点**
- 各 agent 内部统一走 §6 的 `PROVIDERS[model_id]` 抽象层,模型可切换。
- RAG(§7)在 Planner/Coder 前注入组件与记忆上下文。
- `reviews` 设上限(如 ≤3),避免评审回退死循环;超限则带告警返回当前最佳结果。
- 长耗时生成后续可把 `app.invoke` 放入 Redis 队列 + Worker(M2)。

### 5.4 与现有架构的关系
```
用户 → Next.js → FastAPI /api/generate
                  └─ Supervisor(LangGraph) → {Planner, Coder, Designer, Reviewer}
                        ├─ 模型抽象层 → DeepSeek/Qwen/HY3
                        ├─ Chroma(RAG: components + memory)
                        └─ Redis(会话/限流/队列)
                  └─ 流式返回 HTML → iframe 预览
```

---

## 6. 模型抽象层设计(Python · FastAPI)

目标:新增一个模型 = 写一个适配器 + 注册,前端无感、后端无侵入。

```python
from abc import ABC, abstractmethod
from typing import AsyncGenerator

class BaseLLMProvider(ABC):
    id: str            # 'deepseek' | 'qwen' | 'hy3'
    label: str         # 前端展示名

    @abstractmethod
    def chat_stream(self, messages: list[dict], **kwargs) -> AsyncGenerator[str, None]:
        """统一流式输出,逐块 yield 文本片段。"""
        ...

PROVIDERS: dict[str, BaseLLMProvider] = {
    "deepseek": DeepSeekProvider(api_key=settings.DEEPSEEK_KEY),
    "qwen":     QwenProvider(api_key=settings.QWEN_KEY),
    "hy3":      HY3Provider(api_key=settings.HY3_KEY),
}

@app.post("/api/generate")
async def generate(req: GenerateReq):
    provider = PROVIDERS[req.model_id]
    return StreamingResponse(
        provider.chat_stream(req.messages),
        media_type="text/event-stream",
    )

@app.get("/api/models")
async def list_models():
    return [{"id": p.id, "label": p.label} for p in PROVIDERS.values()]
```

- 前端:`GET /api/models` → 渲染模型选择器;生成请求带 `model_id`。
- 后端:按 `model_id` 取 Provider,统一处理限流、日志、计费统计、错误兜底。
- 扩展:新增模型只需实现 `BaseLLMProvider` 并注册一行,不改业务代码。

---

## 7. RAG 设计(Chroma)

两个用途,两个独立 Collection:

| Collection | 内容 | 何时写入 | 何时检索 |
|-----------|------|---------|---------|
| `components` | 优质组件/页面模板片段 + 描述 | 运营预置/沉淀 | 生成前:检索 Top-K 拼进 Prompt,提升质量与风格一致性 |
| `memory` | 用户历史项目摘要/对话要点 | 每次生成后异步写入 | 生成前:检索该用户相关历史,注入上下文,让 AI"记得"偏好 |

**流程**:用户需求 → Embedding → Chroma 检索(components + memory) → 拼装增强 Prompt → 调模型 → 生成 → 异步回写 memory。

⬜ 待定:Embedding 模型用哪家(可复用 Qwen/DeepSeek embedding 接口,或开源 bge)。

---

## 8. 部署方案

**阶段一 — 本地(Docker Compose)**
`docker-compose.yml` 起全套:`frontend`(Next.js)、`backend`(FastAPI + LangGraph)、`mysql`、`chroma`、`redis`。

**阶段二 — 多服务器扩容**
- 前端、后端各自镜像化,可多实例部署(后端 `uvicorn --workers` + 多副本)。
- 数据层独立部署或云托管;前置 Nginx / 负载均衡。
- 会话/限流/Refresh Token 统一放 Redis,保证应用无状态、可水平扩展。
- 长耗时生成走 Redis 队列 + 独立 Worker(M2)。

---

## 9. 数据模型(初稿)

### 9.1 MySQL
```
User      { id, username, email(可空), phone(可空,唯一), password_hash,
            plan(默认 'free'), plan_expire_at(可空),
            quota_used, quota_limit, created_at }
Project   { id, userId, title, share_id(可空), created_at, updated_at }
Message   { id, projectId, role(user/assistant), content, modelId, createdAt }
Artifact  { id, projectId, version, files(JSON), created_at }
UsageLog  { id, userId, modelId, tokens_in, tokens_out, created_at }  // 计量/限流依据
```

### 9.2 收费模式预留表(未来接入时建)
```
Subscription { id, userId, plan, status(active/canceled),
               provider(wechat/alipay/stripe), external_id, expire_at, created_at }
Payment      { id, userId, subscriptionId, amount, currency,
               status, provider, created_at }
```

### 9.3 Chroma Collections
```
components { embedding, metadata:{type, tags, code, description} }
memory     { embedding, metadata:{userId, projectId, summary, createdAt} }
```

### 9.4 Redis Keys(约定)
```
session:<token>           会话(可选,若用服务端会话)
ratelimit:<userId>        限流计数(令牌桶/滑动窗口)
sms:code:<phone>          短信验证码(5min)
auth:refresh:<userId>     Refresh Token(可吊销)
cache:models              模型列表缓存
queue:generate            生成任务队列(M2)
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

- **M0 打通闭环**:前端对话 UI + 模型选择器 → 后端 FastAPI + 模型抽象层 → LangGraph Supervisor(先 Planner→Coder→Reviewer 最简链路)→ DeepSeek/Qwen/HY3 流式生成单文件 HTML → iframe 预览。Docker Compose 起 MySQL/Redis/Chroma。
- **M1 可用**:用户系统(**账号密码登录/注册** + JWT 鉴权,短信与收费仅预留)→ 用量限流(plan/tier 配额,Redis)→ 项目/对话持久化(MySQL)→ 基础 RAG(components 检索)。
- **M2 可分享 + 记忆**:分享只读链接 + memory 记忆回写与检索 + 生成队列/Worker + 迭代修改。
- **M3 增强**:手机短信登录/找回(接真实服务商)、Designer agent 深度美化、Sandpack 多文件、版本历史、导出 zip、模板库运营、收费模式接入、多服务器扩容上线。

---

## 12. ⬜ 开放问题汇总(待拍板)

1. JWT 细节:Access Token 时长、是否用 HttpOnly Cookie 防 XSS?
2. ORM:SQLAlchemy(推荐)还是 Prisma(可选)?
3. Embedding 模型用哪家?(§7)
4. HY3 的 API 文档/鉴权方式?(写 Provider 需要)
5. 视觉风格 / 配色参考?(影响前端 UI 起手)
6. 模型调用月度预算?(影响限流与默认模型策略)
7. 各 agent 的 system prompt 基调 / 目标用户画像?(影响生成风格)

> 已后置(无需现在决定):短信服务商(M3 接真实服务商时定)、收费模式表结构(只留 plan 字段 + 配额抽象,接支付时再建 Subscription/Payment)。

---

## 附:下一步
确认 §12 剩余问题(尤其短信服务商 + HY3 API)后,把文档升到 v1.0,然后从 M0 开始:先搭 `docker-compose.yml` + 前后端骨架 + 模型抽象层 + LangGraph Supervisor 最简链路 + 用户系统骨架(含 SMSService Mock),跑通"登录→对话→多 agent 生成→预览"最小闭环。
