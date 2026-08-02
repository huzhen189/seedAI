# M7 收口：真实 SiteWorkflow 生成 + 全链路实跑通过

> 状态：已完成并本地提交（`aa9f517`，未 push）
> 范围：SeedAI 全链路重构规范 v1.1 — M7（S6 领域执行：Chat/Site/Research/ProjectOps + Artifact/Deployment/Purge Saga）

## 本轮交付

### 1. 真实 SiteWorkflow（替换占位渲染）
- 新增 `backend/app/domains/site/workflow.py`，实现规范 §8.2 固定子流程 **Spec → Produce → Verify → Preview**。
- `service.create_or_edit` 改为编排该流程。
- 生成的是**真实可用、premium 质感**的静态站点：
  - 语义化 HTML5 + 无障碍；
  - **暗 / 亮 / 跟随系统** 三态主题切换（`localStorage` 持久 + `prefers-color-scheme` 兜底）；
  - 玻璃拟态卡片、渐变主色、流体排版（`clamp`）、滚动 `IntersectionObserver` 渐显；
  - **零外部 CDN 请求**（CSS/JS 全部内联，满足 §11.3 隔离与确定性）；
  - 全部用户内容经 `html.escape` 转义，构造上杜绝 `<script>` 注入；`verify()` 做结构与危险 token 末道闸门。
- 需求文本合入 `projects.site_spec`（含 `history` 累积）；产物原子写入 `v{n}/index.html`（tmp→fsync→rename）+ manifest/checksum。

### 2. M7 全链路（既有，本次一并提交）
- `ProjectOps`：publish/trash/restore/purge 真实执行；purge 分步幂等 job（一步一事务，可重入）。
- `S6` 接入 ProjectOps 领域分发：低危直落、高危拒直执行。
- `S5` 审批闸门 + Gate 决策后真实执行并收口 Turn。
- `intent` 补 RESTORE 分支；`workspace` 补 §10.4 指针字段。

## 验证证据

### 实跑：`scripts/smoke_project_ops.py` → **28/28 全绿**（后端 `:7101` 真实 MySQL/Redis/Chroma）
| 验收 | 结果 |
|---|---|
| REQ-SITE-001 自然语言「做一个极简风格的咖啡店官网」走真实 S0→S9 → completed → head_artifact_id 落位 | ✅ |
| REQ-DEPLOY-001 发布经 S5 审批→approved→Deployment Saga→指针切换 + `published/` 落盘 | ✅ |
| 审批重放单次消费（409） | ✅ |
| REQ-DATA-001 purge 物理清空 published+previews + 会话级联消失 | ✅ |
| trash/restore 全周期 | ✅ |

测试账号（供复查）：`e2e20_seedai_test` / `testpass123`
后端：`http://127.0.0.1:7101` 项目：24

### 质量门禁
- `mypy app/domains/site/` 0 告警（新增文件类型干净）。
- 注入安全：恶意 `<script>` 输入被转义，产物不经过 `verify()`。

## 已知遗留（非本轮引入）
- `tests/core/test_m2_pipeline.py::test_skeleton_pipeline_*` 仍失败：该 M2 骨架测试期望 `session=None` 时全 stage NO_OP，与现已完成的真实 Pipeline 冲突；待 M11c 删旧链时一并处置。
- `mypy` 仅余 2 处历史告警（`s2_understand`、`entities.rowcount`），非本次改动引入。
- 完整 LLM 驱动生成（M6 Tool 平台：模型 Harness / BYOK / 16 个原子 Tool）尚未接入新链路；当前 SiteWorkflow 为确定性 premium 模板生成。

## 下一步（规范后续里程碑）
- **M8** S7–S9 finalize：代码已落地，需补充 normal `pass` 闲聊/建造路径的契约测试与效果验证。
- **M9** 前端闭环：五阶段 Stage Rail、单一 StreamReducer、审批卡、离线队列、独立预览 Origin。
- **M10/M11** 运维安全与旧链删除（M11c 删 `app/agent/*`、`proxy.py`、`projects.py` 旧链路）。
