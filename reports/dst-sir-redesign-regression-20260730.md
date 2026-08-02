# DST / SIR 重构落地验证报告（Steps 1–6）

> 生成时间：2026-07-30 09:56
> 方案依据：`docs/DST_SIR_REDESIGN_PLAN.md` §10 落地序列（Steps 1–6，Step 7 文档已前置完成）
> 重构内核：LLM 只写 `SIR_delta`（本轮变更），代码用 4 标准操作 + 4 冲突规则合并 —— **LLM 是 writer，代码是 editor + referee**

---

## 〇、测试账号（登录复查用）

| 项 | 值 |
|---|---|
| 后端地址 | `http://localhost:7101`（业务 + AI 核心合并单进程） |
| 前端复查 | `http://localhost:7100`（nginx 静态托管 `dist`） |
| 账号 / 密码 | **`huzhen` / `huzhen189`**（role = super_admin） |
| 账号来源 | `FORCE=1 python scripts/reset_all.py` 清库后自动注入 |

> 用上述账号登录前端 → 在「项目管理 / 对话」可直接查看本轮回归产生的项目与对话。

---

## 一、状态总览

| 验证项 | 结论 | 说明 |
|---|---|---|
| DST 引擎单测 | ✅ 39 / 39 通过 | `scripts/dst_regression.py`，离线无需后端 |
| 环境重置 + 重载 | ✅ 完成 | reset 建超管 + 起重 7101 加载新代码，`/ready=200` |
| 前端「确认框却报超时」修复 | ✅ 代码 + 构建完成 | 待 git commit |
| 10 条集成冒烟脚本 | ✅ 已就绪 | `scripts/run_tests.py`（唯一集成入口） |
| 10 条集成回归实跑 | ⏳ 待跑 | 被本轮前端修复插队，重跑即可 |

---

## 二、改动范围

### Step 1–4（核心引擎，已验证）

| 文件 | 关键内容 |
|---|---|
| `app/agent/intent/dst.py` | 纯函数 DST 引擎：4 标准操作 + 4 冲突规则、`SIRDelta` Pydantic、解析/合并/缺失/`derive_decision`、`normalize_sir`、`build_sir_for_shortcut` |
| `app/agent/intent/store.py` | `load_sir / save_sir / reset_sir` + 旧扁平结构自动升级 |
| `app/agent/intent/sir_prompt.py` | `SIR_SYSTEM` 提示 + `_extract_sir_delta`（LLM 仅写 delta，失败优雅降级） |
| `app/agent/intent/cascade.py` | 级联核心统一走 `apply_delta` + `derive_decision`，删 ad-hoc merge |

### Step 5（Worker / runner 执行前闸门）

- `app/agent/core/router.py`：`detect_intent_v2` 透传 `sir_pending`，供 Worker 复用。
- `app/agent/core/queue.py`：在 `[4.5]` 强制续跑之后、执行 skill 之前新增 **`[4.6] SIR pending 最终闸门**。

**闸门触发条件**（全部满足才降级为 clarify）：

```
decision == route
  且 非 clarified 续跑（_skip_classify）
  且 非 fallback 降级
  且 sir_pending 非空
  且 level1 != chat
  且 非「建站且需求文档已具备」
  且 追问轮次 < CLARIFY_MAX_ROUNDS
```

**行为**：降级 `decision = clarify`，把 pending 槽翻译成自然语言追问追加进 `clarify_questions`（不覆盖 cascade 既有高质量问题）。
**设计取舍**：cascade 已有 3 个 business override 会强制 route（build+req / chat / max-rounds 耗尽），本闸门作最后安全网；`max_rounds` 上限防 clarify 死循环。

---

## 三、验证结果

### 3.1 单元 / 引擎回归（离线，无需后端）

`scripts/dst_regression.py` —— **PASS = 39，FAIL = 0**

覆盖：4 标准操作 × 4 冲突规则、`normalize_sir`、compute_missing / derive_decision、parse_sir_delta，以及「低置信 pending 不覆盖已 confirmed 槽」。

### 3.2 环境重置与启动

- `FORCE=1 scripts/reset_all.py`：DROP 12 表 + Redis 清 + Chroma 清运行集 + 重建意图向量索引 + 创建超管 `huzhen/huzhen189`，EXIT = 0。
- `scripts/start-local.sh`：清 pycache + 起重单进程 7101，新代码加载无 import / 启动错误。

### 3.3 前端「确认框却显示超时」修复（07-30 用户反馈）

**现象**：后端弹了澄清卡 / 二次确认框（用户理解的「确认框」），但前端同时显示
「⚠ 模型响应超时，本次未能生成内容」——矛盾 UI。

**根因**：`frontend/src/views/ChatView.vue` 的 `onDone` 空结果检测只判断
正文 / 产物 / 子任务是否为空；而后端所有暂停态（block / confirm / clarify）
**都是先发暂停事件、再补发 `done` 收尾**，这些轮次本就无正文 / 产物，于是被误判成超时。

**修复**：`onDone` 增加暂停态守卫 —— 若本轮已激活 `clarifyData / pendingConfirm /
blockReason / v4Pause` 任一（说明后端主动暂停等用户输入），跳过超时误判，仅收尾轨迹。
真超时轮次不带任何暂停态，照常报超时。

已 `vite build` 重建 `dist` 生效（nginx 7100 静态托管，硬刷 Ctrl+F5 即可）。

> **伴随发现（独立议题，未改）**：「帮我做一个企业官网」（缺规格建站）实际被 cascade
> 判成 `decision=route → agent_requirement`（无需求文档先转 PM 采集），不进 SIR pending、
> 不弹 clarify。即新建站缺槽优先走「转 PM」而非 SIR pending 澄清。若你希望缺规格直接弹
> 澄清卡，需另调 cascade 优先级（本次未动）。

---

## 四、集成测试脚本收敛（唯一入口）

用户要求「保留一个测试脚本就行，其他的不要了」+「测一条改一条」+「目标改 localhost」。

- `scripts/run_tests.py` 重写为 **10 条精简集成冒烟**：
  - 1 闲聊 + 9 建站（需求采集 → 单意图生成 → 带规格 → 缺槽 clarify 验证 `[4.6]` 闸 → 明确单意图 → 修改 → 多意图 ×3）
  - `BASE` 默认 `http://localhost:7101`，`MODEL` 默认 `deepseek`
  - 固化规则：**失败即停**（便于测一条改一条）、报告顶部写死账号密码、开头提示重置、自动报告
  - 加 case 只需往 `TEST_CASES` 追加
- 删除冗余脚本（保留 `run_tests.py` + `dst_regression.py` 引擎单测）：
  `scripts/{complex_test, e2e_*, multi_intent_regression}`、`probes/probe_phase1_5cases.py`、
  `backend/_e2e_*` 及 json 产物、`backend/_e2e_harify.py`、`backend/scripts/test_vector_*`、`_gen_v8.py`

---

## 五、风险与未决项

- **`[4.6]` 闸门仅拦「route 但仍有 pending」**：chat / build-with-req / max-rounds 三种 escape 与 cascade 一致。若后续要求「建站也必须先补齐 pending 槽」，需同步调整 cascade 的 business override。
- **集成回归实跑待补**：10 条脚本就绪，因本轮前端修复插队未实跑；重跑即出结论。
- **commit 状态**：`dst.py / store.py / sir_prompt.py / cascade.py`（Step1–4）+ `router.py / queue.py`（Step5）+ 前端 `ChatView.vue`（超时修复）均已落地但**本地未 commit、未 push**（按项目约定）。

---

## 六、下一步

1. 硬刷前端（Ctrl+F5）确认澄清卡 / 确认框不再误报超时。
2. 重跑 10 条集成冒烟（`python scripts/run_tests.py`），按「测一条改一条」处理失败项。
3. 全部通过后本地 `git commit`（不 push）。
