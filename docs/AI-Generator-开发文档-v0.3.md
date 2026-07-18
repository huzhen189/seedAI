# AI 生成器平台 — 开发文档 & 架构方案 (v0.3)

> 版本:v0.3(草稿,持续共创)
> 最后更新:2026-07-17
> 一句话定位:一个通过 AI 对话即可生成**网站/前端页面**(后续扩展文档、代码)的平台;支持多模型自选、RAG 增强,可自托管、可横向扩容。

> 变更史:
> - v0.1 → v0.2:定位升级为可自托管/可扩容架构;定多模型、MySQL+Chroma+Redis、Next.js 前端 + 独立后端。
> - v0.2 → v0.3:**后端定 FastAPI (Python)**,替换原 NestJS 方案;模型抽象层/架构图改为 Python 实现。

---

## 1. 项目概述

### 1.1 产品定位
- **形态**:类 v0.dev / bolt.new / Lovable 的"对话即生成"平台。
- **使用范围**:自己为主 + 小范围分享,预留成长为多用户、可扩容平台的架构空间。
- **首要产物**:网站 / 前端页面(带实时预览、可分享、可迭代)。
- **后续扩展**:文档生成、代码工程生成(架构预留,不在 MVP)。

### 1.2 核心价值
自然语言描述需求 → AI(可选模型 + RAG 增强)生成可运行网页 → 实时预览 → 对话迭代 → 分享/导出。

---

## 2. 核心功能范围

### 2.1 MVP(第一阶段必须有)
| 模块 | 说明 |
|------|------|
| 对话生成 | 输入需求,流式返回生成过程与结果 |
| 多模型自选 | 前端可切换 DeepSeek / Qwen / HY3,后端统一调度 |
| 实时预览 | 生成页面在 iframe / 沙箱中即时渲染 |
| 迭代修改 | 基于已有结果继续对话("把按钮改成蓝色") |
| RAG 增强 | 组件/模板检索 + 历史对话记忆(见 §6) |
| 项目管理 | 每次生成保存为"项目",可重开 |
| 轻量账号 + 限流 | 登录 + 每人用量限制,防 Key 被刷 |
| 分享链接 | 生成只读预览链接 |

### 2.2 第二阶段(增强)
版本历史与回滚 / 代码在线编辑 / 导出 zip / 一键部署 / 模板库运营

### 2.3 暂不做
完整订阅计费 / 复杂后端逻辑生成 / 团队协作权限体系

---

## 3. 技术选型

| 层 | 选型 | 说明 |
|----|------|------|
| 前端 | **Next.js (App Router) + TypeScript** | 对话 UI、预览面板、模型选择器 |
| 前端 UI | Tailwind CSS + shadcn/ui | 快速搭建、风格统一 |
| **后端** | **独立服务 · FastAPI (Python)** | AI/向量生态最成熟,Chroma Python SDK 原生支持 |
| AI 编排 | OpenAI SDK(Python)/自封装流式 | 统一流式输出(async generator) |
| 模型 | **DeepSeek + Qwen + HY3(前端可选,可扩展)** | 模型抽象层,见 §5 |
| 关系库 | **MySQL** | 用户/项目/对话/产物 |
| 向量库 | **Chroma** | 组件模板检索 + 对话记忆,见 §6 |
| 缓存/队列 | **Redis** | 缓存、限流、会话、异步队列 |
| ORM | ⬜ SQLAlchemy(推荐,Python 原生) / Prisma(可选) | 见 §3.3 |
| 部署 | **Docker Compose(本地) → 多服务器扩容** | 见 §7 |

> **前后端跨语言说明**:前端 TS、后端 Python,通过 HTTP/SSE 解耦,互不直接依赖类型。约定:
> - 接口契约以 **OpenAPI(Swagger)** 为准(FastAPI 自动生成),前端按需生成/手写客户端。
> - 模型 Key 只在后端;前端只传 `modelId`。
> - 本地开发:Next.js dev 配 `rewrites` 将 `/api/*` 代理到 FastAPI(`localhost:8000`),前端无感知。

### 3.1 模型接入(已定)
DeepSeek + Qwen + HY3,**前端自选**,后端做**模型抽象层 + Provider 注册表**,后期可随时新增模型。详见 §5。

### 3.2 数据层(已定)
- **MySQL**:结构化数据(用户、项目、对话、产物快照)。
- **Chroma**:向量检索——①优质组件/页面模板;③历史对话/项目记忆。
- **Redis**:响应缓存、接口限流(令牌桶)、会话存储(支撑无状态横向扩展)、生成任务异步队列。

### 3.3 ⬜ 待决策(次要,可动手时定)
1. ORM:**SQLAlchemy(推荐,Python 原生)** vs Prisma(可选)。
2. 是否引入独立 **Worker 进程**处理长耗时生成(用 Redis 队列解耦)——建议 M2 再加。
3. HY3 的 API 规格/鉴权方式(需你提供文档,以便写进 Provider 适配器)。

---

## 4. 系统架构

```
┌──────────────────────────────────────────────────────────┐
│  前端层  Next.js (TS) (对话 UI · 预览面板 · 模型选择器)       │
│          前端只传 modelId,API Key 永不落前端              │
└───────────────────────────┬──────────────────────────────┘
                            │ HTTP / SSE 流式 (/api/* → 8000)
┌───────────────────────────▼──────────────────────────────┐
│  后端层  独立服务 (FastAPI · Python)                         │
│  API 路由 · 生成编排 · 鉴权/限流 · RAG 检索                  │
│  模型抽象层 (Provider 注册表: DeepSeek / Qwen / HY3 …)      │
└───────┬───────────────────────────────────┬──────────────┘
        │                                   │
┌───────▼────────┐            ┌─────────────▼──────────────┐
│  模型层         │            │  数据层                      │
│ DeepSeek/Qwen/ │            │  MySQL(关系) · Chroma(向量) │
│ HY3 API        │            │  · Redis(缓存/限流/队列)     │
└────────────────┘            └────────────────────────────┘

部署:Docker Compose(本地) → 多服务器扩容
     应用无状态 + 会话入 Redis + 反向代理/负载均衡
```

**核心约束**:模型 API Key 只在后端;前端仅传 modelId。应用层保持无状态(会话/限流状态放 Redis),这是横向扩容的前提。

---

## 5. 模型抽象层设计(Python · FastAPI)

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

# 注册表:启动时按环境变量实例化;新增模型只写子类 + 加一行注册
PROVIDERS: dict[str, BaseLLMProvider] = {
    "deepseek": DeepSeekProvider(api_key=settings.DEEPSEEK_KEY),
    "qwen":     QwenProvider(api_key=settings.QWEN_KEY),
    "hy3":      HY3Provider(api_key=settings.HY3_KEY),
}

# FastAPI 路由示例:用 StreamingResponse 把 async generator 推给前端
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
- 后端:按 `model_id` 从注册表取 Provider,统一处理限流、日志、计费统计、错误兜底。
- 扩展:新增模型只需实现 `BaseLLMProvider` 并写一行注册,不改业务代码。

---

## 6. RAG 设计(Chroma)

两个用途,两个独立 Collection:

| Collection | 内容 | 何时写入 | 何时检索 |
|-----------|------|---------|---------|
| `components` | 优质组件/页面模板片段 + 描述 | 运营预置/沉淀 | 生成前:按用户需求检索 Top-K 片段拼进 Prompt,提升质量与风格一致性 |
| `memory` | 用户历史项目摘要/对话要点 | 每次生成后异步写入 | 生成前:检索该用户相关历史,注入上下文,让 AI"记得"偏好 |

**流程**:用户需求 → Embedding → Chroma 检索(components + memory) → 拼装增强 Prompt → 调模型 → 生成 → 异步回写 memory。

⬜ 待定:Embedding 模型用哪家(可复用 Qwen/DeepSeek 的 embedding 接口,或用开源 bge)。

---

## 7. 部署方案

**阶段一 — 本地(Docker Compose)**
一份 `docker-compose.yml` 起全套:`frontend`(Next.js)、`backend`(FastAPI/uvicorn)、`mysql`、`chroma`、`redis`。本地 `docker compose up` 即可跑通完整闭环。

**阶段二 — 多服务器扩容**
- 前端、后端各自镜像化,可分别多实例部署(后端用 `uvicorn --workers` + 多副本)。
- 数据层(MySQL/Chroma/Redis)独立部署或用云托管。
- 前置 Nginx / 负载均衡;会话与限流状态统一放 Redis,保证应用无状态、可任意水平扩展。
- 长耗时生成任务走 Redis 队列 + 独立 Worker(M2 引入)。

---

## 8. 数据模型(初稿)

### 8.1 MySQL
```
User      { id, name, email, passwordHash, quotaUsed, quotaLimit, createdAt }
Project   { id, userId, title, shareId(可空), createdAt, updatedAt }
Message   { id, projectId, role(user/assistant), content, modelId, createdAt }
Artifact  { id, projectId, version, files(JSON), createdAt }   // 每次生成产物快照
UsageLog  { id, userId, modelId, tokensIn, tokensOut, createdAt } // 计量/限流依据
```

### 8.2 Chroma Collections
```
components { embedding, metadata:{type, tags, code, description} }
memory     { embedding, metadata:{userId, projectId, summary, createdAt} }
```

### 8.3 Redis Keys(约定)
```
session:<token>           会话
ratelimit:<userId>        限流计数(令牌桶/滑动窗口)
cache:models              模型列表缓存
queue:generate            生成任务队列(M2)
```

---

## 9. 核心技术难点:生成结果预览

| 生成内容 | 预览方案 | 复杂度 |
|---------|---------|--------|
| 单文件 HTML(内联 CSS/JS + CDN) | `<iframe srcdoc>` 直接注入 | ⭐ 低(MVP 首选) |
| 含 React/多文件/需构建 | Sandpack(浏览器内打包) | ⭐⭐ 中(增强) |
| 需真实 Node 运行 | WebContainer / 服务端沙箱 | ⭐⭐⭐ 高(暂不做) |

**MVP 决策**:先生成单文件 HTML,iframe srcdoc 预览,0 构建成本先跑通闭环;增强阶段再上 Sandpack 支持多文件工程。

---

## 10. 开发路线图

- **M0 打通闭环**:前端对话 UI + 模型选择器 → 后端 FastAPI 模型抽象层 → DeepSeek/Qwen/HY3 流式生成单文件 HTML → iframe 预览。Docker Compose 本地起 MySQL/Redis/Chroma。
- **M1 可用**:登录鉴权 + 用量限流(Redis) + 项目/对话持久化(MySQL) + 基础 RAG(components 检索)。
- **M2 可分享 + 记忆**:分享只读链接 + memory 记忆回写与检索 + 生成队列/Worker + 迭代修改。
- **M3 增强**:Sandpack 多文件、版本历史、导出 zip、模板库运营、多服务器扩容上线。

---

## 11. ⬜ 开放问题汇总(待拍板)

1. ORM:SQLAlchemy(推荐)还是 Prisma(可选)?
2. Embedding 模型用哪家?(§6)
3. HY3 的 API 文档/鉴权方式?(写 Provider 需要)
4. 视觉风格 / 配色参考?(影响前端 UI 起手)
5. 模型调用月度预算?(影响限流与默认模型策略)

---

## 附:下一步
确认 §11 剩余问题(尤其 HY3 API)后,把文档升到 v1.0,然后从 M0 开始:先搭 `docker-compose.yml` + 前后端骨架 + 模型抽象层,跑通"对话→生成→预览"最小闭环。
