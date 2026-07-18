# 贡献与代码质量约定（质量基线 v0.3.0）

> 本文档定义团队代码质量门禁、本地校验命令与评审约定。所有改动须先过本地校验，再提 PR 由资深开发评审。

## 1. 质量门禁（CI 自动执行）

| 层 | 工具 | 检查项 | 本地命令 |
| --- | --- | --- | --- |
| 后端 lint | ruff | 风格/未用导入/正确性(B/SIM/C4) | `python -m ruff check backend/business/app backend/ai_service/app` |
| 后端 format | ruff | 格式化一致性 | `python -m ruff format backend/business/app`（写） / `--check`（查） |
| 后端 类型 | mypy | 类型错误（目标 3.10+） | `python -m mypy backend/business/app` |
| 后端 测试 | pytest | 单元/集成测试 | `cd backend/business && python -m pytest -q` |
| 前端 lint | eslint | Vue/TS 规范 | `cd frontend && npm run lint` |
| 前端 format | prettier | 格式化一致性 | `npm run format:check` |
| 前端 测试 | vitest | 单元测试 | `npm run test` |
| 前端 构建 | vue-tsc+vite | 类型检查+打包 | `npm run build` |

CI（`.github/workflows/ci.yml`）任一环节失败即阻断合入。**本地提交前请先跑一遍对应命令。**

## 2. 本地环境

- 后端：`pip install -r backend/requirements-dev.txt`（含 ruff/mypy/pytest）。
- 前端：`cd frontend && npm install`（含 eslint/prettier/vitest）。

## 3. 评审约定（mentoring）

- 任何改动走 PR，禁止直接 push 主干。
- 资深开发对每处改动给 **带严重级的 finding**（🔴 高 / 🟠 中 / 🟡 低），🔴 必须修。
- 评审重点（也是团队当前的技术短板，刻意练习）：
  - **错误处理的完整性**：上游异常/超时是否污染了已落库的数据？
  - **鉴权单一真相源**：不要有两份"判断当前用户"的逻辑（易漂移）。
  - **边界与并发**：并发注册、取消、资源归属校验。
  - **安全**：用户输入（含 AI 输出）渲染前务必清洗（如 DOMPurify）；密钥不入仓。
  - **可测试性**：新逻辑尽量抽纯函数，便于无外部依赖的单测。

## 4. 已知技术债（团队练兵 backlog）

> 这些是已识别、尚未修复的项。适合作为 PR 练手或结对课题。

- 🔴 **H1**：`backend/business/app/proxy.py` 的 `publisher()` 在 upstream 返回 4xx/5xx 时仍于 `finally` 落库，会写入"只有用户消息、无 AI 回复"的悬空记录。应仅当流走到 `done` 才落库。
- 🔴 **H2**：鉴权逻辑写了两份（`security.get_current_user` 与 `proxy._resolve_user`）。抽一个 `_authenticate(request) -> Optional[CurrentUser]` 共用。
- 🟠 **M1**：`auth.register` 并发重复会 500（唯一约束）。catch `IntegrityError` 回 409。
- 🟠 **M2**：SSE 无生成时长上限（`read=None`），AI 假死会挂连接。加服务端最大时长。
- 🟠 **M3**：`/api/cancel` 未鉴权且 `request.json()` 失败后又读 `request.body()`（body 可能已消费）；改为 user 作用域。
- 🟠 **M4**：`/api/models` 在 AI 服务宕机时直接 500，无兜底。
- 🟠 **M5**：前端 `api/projects.ts` 的 `j()` 直接 `throw new Error(rawText)`，网关 HTML 错误页会原样透出；归一化错误展示，区分 401/429/5xx。
- 🟡 **D1**：`main.py` 使用已弃用的 `@app.on_event("startup")`，迁移到 `lifespan` 处理器。
- 🟡 **D2**：`ai_service` 的 `Optional[X]→X|None` 等现代化注解（UP 规则）暂放行，后续统一。
- 🟡 **D3**：自动化测试覆盖仍薄，优先补齐 `auth` 全链路（register/login/refresh/me）与前端 `api/projects` 的 401 分支。

## 5. 生产部署

> **线上根因教训（2026-07-18）**：裸跑 `vite dev` 当生产用 + 数据栈（Redis）未起，导致 AI 核心 `/generate` 返回 500、业务端把上游错误裸透传成 `Internal Server Error`。**正确部署必须用 `docker compose` 全套**——它会自动拉起数据栈（redis/mysql/chroma），并以生产模式运行前后端（前端 nginx 托管 build 产物，后端 uvicorn 无 `--reload`）。

- 一键部署：`bash scripts/deploy-prod.sh` —— 拉最新代码 + `docker compose up -d --build` 全套（含数据栈）。
- 仅起数据栈：`bash scripts/deploy-prod.sh data`（redis/mysql/chroma，不动前后端）。
- 状态 / 日志：`bash scripts/deploy-prod.sh status` / `logs`。
- 部署后验证：`/api/chat` 无登录应返回 SSE error 帧（`v0.3.1+`，`code=AUTH_REQUIRED`）；`/generate` 应返回 `200/202`（说明 Redis 已通）。
- 前置条件：服务器需装 **Docker Engine（含 compose v2）**，且存在 `.env`（从 `.env.example` 复制并填真实连接串）。
- 端口冲突：部署前若 `7100/7101/7102` 被裸跑进程占用，先 `pkill -f vite; pkill -f 'uvicorn app.main:app'`，避免端口争用。
