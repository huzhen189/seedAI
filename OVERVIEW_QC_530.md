# 概览：QC 全意图覆盖 + per-sub-task 打分（#524→#530）落地与部署

## 已完成
1. **前端适配 #530 全部落地并通过类型校验**
   - `frontend/src/types.ts`：`QcResult.sub_task_id?`（区分整体/子任务 QC）、`SubTaskView.qc?`（子任务自身 QC）。
   - `frontend/src/views/ChatView.vue`：`STAGE_LABELS` 补 `qc_checking = '系统正在核对本次对话生成质量...'`；`onQc` 按 `data.sub_task_id` 分流——带 id 写入对应子任务 `st.qc`（泳道展示），不带则按 trace_id 存 `qcMap`（气泡徽标）。
   - `frontend/src/components/ThoughtTrail.vue`：`qc_checking` 标签 + ⚖️ 图标。
   - `frontend/src/components/SubTaskTrack.vue`：子任务 lane 内渲染 per-sub-task 质量分徽标（绿 ≥7 / 黄 <7，需复核红标）。
2. **后端联调（前序 #527/#528/#529，本次重启生效）**
   - `queue.py`：单意图路径先发 `node(qc_checking)` 再 `run_qc` 再发 `qc`（无 sub_task_id）；编排 `_run_one` 每子任务完成发 `qc`（注入 `sub_task_id`）；qc 事件带 `sub_task_id` 时走 `_persist_qc_score(sub_task_id=_sid)` 复合键落库。
   - `run_safety` 错误导入路径已修（#527，QC 静默失败根因）。
   - `models.py` `QcScore.sub_task_id` 列 + `trace_repos.py` 落库分流。
3. **DB 迁移自动补齐（无需手写 Alembic）**
   - 重启触发 `init_db()`→`_add_missing_columns`，启动日志实证：`数据库 schema 已自动补齐缺失列: qc_scores.sub_task_id (VARCHAR(64))`。
4. **`scripts/start-local.sh` 加固（顺手修）**
   - 本 Bash 工具 shell 缺 `seq`/`sleep`/`nohup`，原脚本会**静默失败、旧进程持续用旧码服务**。已改为 POSIX 循环 + `psleep()`（venv python 兜底）+ `nohup` 或 `&`+`disown` 启动；`bash -n` 通过。

## 验证结果
- 后端 `py_compile` 全绿（前序）；前端 `vue-tsc -b` 退出 0（前序）。
- 后端重启：`/health` 返回 200，新进程 PID **45464** 加载新码 + 自动补列生效。
- 端口 7101 监听正常，单进程 v2.0.0 启动完成（Worker 池、意图向量索引、Chroma 集合均就绪）。

## 当前状态
- 后端：**运行中（PID 45464，新代码 + 新列已生效）**。
- 任务：#524 / #525 / #526 / #530 已标记 completed。

## 下一步（非阻塞建议）
- 跑一次含**建站/代码类**意图的端到端测试，确证 per-sub-task QC 实时流式推送 + `qc_scores.sub_task_id` 非空落库（闲聊/单意图 QC 同步验证）。
- 若需再次重启，使用加固后的 `scripts/start-local.sh`；本工具 shell 下若仍缺 `nohup`，可直接用后台任务方式起 uvicorn（已验证可靠）。
