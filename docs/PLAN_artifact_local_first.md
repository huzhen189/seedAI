# 产物「本地优先 + nginx 直出 + DB 只存路径」实施方案

> 状态：方案讨论稿（待确认后进入 P1 实施）
> 关联：对话历史 msg1–msg4（本地落盘 → 点发布才上 COS → message 只存路径 → 前端拼接预览域名）

## 一、已确认决策（来自你的指令 + AskUserQuestion）

| 项 | 决策 |
|----|------|
| 本地 dev 入口 | 走 `seedai.huzhen.net.cn`（nginx 反代 7100 前端 + 7101 业务） |
| 产物落盘 | 先存本地，点「部署发布」才上 COS |
| message 存储 | 只存**文件路径**，不内联文件内容（解决 1406 超长根因，顺带） |
| 目录位置 | 与 `backend` 平级（`artifacts/` 本就在仓库根），nginx 直接静态托管，后端不代理 |
| 历史版本 | 复用 `Artifact.version`（每次生成自增，天然保留历史） |
| 版本快照 | git tag 快照（同一站点的 git 仓库，发布/改版前打 tag 可回滚） |
| 多页面 | 显式 pages 清单（生成请求带 pages 字段，意图层抽取，强制多文件产出 `<!-- FILE: x -->`） |
| 加页语义 | (A) 同版本覆盖（modify/append 改写同一 `vN` 目录，version 仅在「新建版本」时自增） |
| 老数据 | 你一会跑 `reset_all` 重置，前端保留 path→url 最小兜底，不做历史迁移 |

---

## 二、目录布局（本地 & COS 对齐同一规则）

```
artifacts/                       # 仓库根，与 backend 平级
└── {user_id}/{project_id}/
    ├── .git/                    # 一个站点一个 git 仓库（version 快照 / tag 回滚）
    ├── v1/
    │   ├── index.html
    │   └── about.html          # 多页面：同一版本的多个文件
    ├── v2/
    │   └── index.html
    └── ...
```

- **本地路径模板**：`{ARTIFACT_DIR}/{uid}/{pid}/v{ver}/{fname}`
- **COS key 模板**（发布时）：`previews/{uid}/{pid}/v{ver}/{fname}`（与现有 `cos_upload` key 规则一致，发布即直传同 key，无需路径换算）
- **git 仓库根** = `artifacts/{uid}/{pid}`；每次生成完 `git add -A && commit`，新版本 commit 后 `git tag v{ver}` 固定快照。
- `ARTIFACT_DIR` 锁**绝对路径** `E:/work/myTencentYunHome/seedAI/artifacts`（消除 `./artifacts` 相对 cwd 的歧义），写入 `.env`。

---

## 三、后端改动清单（P1 基础设施）

### 1. `shared/config.py` + `.env`
- `artifact_dir` 改绝对路径；`.env` 增加 `ARTIFACT_DIR=E:/work/myTencentYunHome/seedAI/artifacts`。

### 2. `agent_generate_site._deliver()`（核心改造）
- **本地优先**：始终写 `artifacts/{uid}/{pid}/v{ver}/{fname}`（不再写 `anon/<trace>/`）。
- **关闭生成时 COS**：不再调 `cos_upload`，去掉逐文件 `cos_upload`/`progress` 事件。
- `deliver_done` 回传**相对路径字典**：`{"files": {fname: "user_3/proj_5/v2/index.html"}}`（相对 `ARTIFACT_DIR`）。
- 生成阶段 `preview` 事件：不再带 `url`，改带 `path`（相对路径）+ `files` 相对路径字典；`content` 兜底（COS 不可用才需要）可保留为本地读取以减少 SSE 体积，**或前端直接走 `/artifacts/{path}` 拉取**（推荐后者，零兜底内容下发）。

### 3. `proxy.py`（捕获 + 落库只存路径）
- 捕获 `deliver_done`/`preview` 的 `path` 字段 → `preview_path`（替代 `preview_url`）。
- `_persist_conversation` 建站分支：
  - `Artifact.files = {fname: {name, size, path}}`（`path` = 相对 `ARTIFACT_DIR` 的路径，**不内联 content**）。
  - `content_obj = {type:"plain", text: refined_summary, artifact_id, preview_path, deployed:false, files:{fname:{name,size,path}}}`。
  - `Artifact.preview_url` 留空（发布后回填 COS 直链）。
  - 删除 `is_html` 分支里内联 `content`/`deliver_fallback_content` 的逻辑（DB 不再存文件体）。

### 4. `agent/core/git_site.py`（仓库粒度重构）
- `_repo_path` 从 `anon/<trace>` 改为 `artifacts/{uid}/{pid}`（需传入 `project_id`；`queue._commit_after_done` 改传 `project_id`）。
- `commit_site_for_trace` 在 commit 后对新版本 `git tag v{ver}`。
- `bundle_to_cos` 保持不变（发布镜像，优雅跳过）。

### 5. 同步改动的耦合点（路径规则统一）
- `queue._qc_fix_loop`：重传 COS 的本地根改为 `{uid}/{pid}/v{ver}`。
- `queue._index_project_code`：索引根改为 `{uid}/{pid}`（建议只索引最新 `vN`，避免历史版本重复入 Chroma）。
- `main.retry-upload` 端点：本地源改为 `{uid}/{pid}/v{ver}/index.html`，并支持按 `project_id`/`version` 参数。
- `agent_build._deliver`（另一个建站 skill）：同样改本地优先 + 路径回传。

### 6. `scripts/reset_all.py`（可选但建议）
- 重置时清 `artifacts/` 下所有站点目录（保留 `.git` 无意义，全清最符合「数据重置」语义）。

---

## 四、前端改动（P1）

核心：`RightPanel` / `ChatView` 的预览地址从「COS url」改为「本地 path → `${location.origin}/artifacts/{path}`」。

- `allFiles` computed：每个文件计算 `effectiveSrc = url || ${location.origin}/artifacts/${path}`。
- `RightPanel` iframe：`src = effectiveSrc`（统一用 src，不再 srcdoc 内联；彻底消除 1406 类超长内容下发的隐患）。Markdown/图片同理由 path 拉取。
- `ChatView` 气泡站点卡片：`artifact-summary-card` 与「打开线上预览」按钮：未发布（`deployed=false`）时禁用并提示「未发布」，发布后（`url` 存在）才跳转 COS 直链。
- `loadArtifacts`：不变（后端已返回 path）。
- 多版本切换：已支持（按 artifact 行点选 vN）。

> 说明：因为本地 dev 走 `seedai.huzhen.net.cn`，浏览器 `origin` 即该域名，nginx `/artifacts/` 同源直出 → **无需后端下发预览域名**，前端用 `location.origin` 即可。未来切生产若用独立 sandbox 域名，再加 `/api/config` 暴露 `artifactBaseUrl` 即可（本次不做，留 P2）。

---

## 五、发布（上 COS）— 单独增量 P4（非本次 P1）

- 新增 `POST /api/deploy`：`{artifact_id}` → 后端读本地 `artifacts/{uid}/{pid}/v{ver}/*`，逐个 `cos_upload`（key=`previews/{uid}/{pid}/v{ver}/{fname}`），回填 `Artifact.files[fname].url` + `Artifact.preview_url`（index 直链）+ `deployed=true`。
- 前端「部署发布」按钮：调 `/api/deploy`，成功后气泡切到 COS 直链预览。
- 多页面：发布时一次上传目录下全部文件。

---

## 六、我的 senior 建议（供你拍板）

1. **git 仓库粒度改为「用户/项目」而非「trace」**：现在 `git_site` 是 `anon/<trace>` 一个仓库。改成 `{uid}/{pid}` 更自然——多版本同仓、`git tag vN` 快照即版本锚点，回滚 `git checkout vN` 一步到位。代价是要改 `git_site` + `queue` 调用点（已在 §3.4 列出）。**我推荐这么做**，否则版本快照只停留在「概念」层面，没法真正回滚。

2. **前端一律用 `src`（不再 srcdoc 内联）**：之前为兜底 COS 不可用内联了整站 HTML，这正是最初 1406 超长落库的源头之一。本地优先后，浏览器直接 `fetch /artifacts/{path}` 渲染，干净且零超长传输。

3. **未发布产物的越权风险**：`/artifacts/` 在公网（生产）下任何人拼路径可读。dev 阶段无所谓；**生产上线前**建议加一层签名/鉴权中间件（或先放独立 sandbox 域名 + 短时签名）。本次先记一笔，不阻塞 P1。

4. **多页面 pages 清单是独立大块**：需要 ①意图层抽 pages ②Planner 输出 pages ③Coder 强制多文件 ④前端已支持文件树。建议**作为 P2**，本次 P1 只做本地优先 + 路径存储 + nginx（先让「生成→本地预览→发布」闭环跑通），多页面在闭环上叠加。

---

## 七、待你确认的点（决定代码量）

- **Q1**：git 仓库粒度——按上面 §3.4 重构为「用户/项目」（推荐），还是最小改动只把 `anon/<trace>` 换成 `{uid}/{pid}/v{ver}` 路径前缀、仓库仍按 trace？
- **Q2**：本次 P1 范围——只做「本地优先 + 路径存储 + nginx」（多页面留 P2，推荐），还是连 pages 显式清单一起做？
- **Q3**：`reset_all` 重置时是否一并清空 `artifacts/` 本地产物树？

确认后我从 P1-1（锁绝对路径）→ P1-2（nginx）→ P1-3（`_deliver`）→ P1-4（落库只存 path）→ P1-5（重启验证 + 本地提交，不 push）依次执行。
