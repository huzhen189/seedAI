# 诊断：『按照我刚刚的要求给我生成一个网站』未达到预期

> 日期：2026-07-24 ｜ trace=`t19f93bb21a4096ccb60eb7ed` ｜ conv=4 ｜ 模型 deepseek
> 结论：本次失败与上次『死亡路由』是**不同的 4 个新 bug 叠加**，根因是**系统完全无视了对话里用户真实写出的需求**，转而用第一条闲聊消息凭空编造。

## 一、对话真实还原（来自 business.log，conv=4 共 4 条消息）

| # | 角色 | 内容 | 性质 |
|---|------|------|------|
| msg1 | user | 今天深圳天气怎么样？晚上有没有啥好吃的推荐给我？并且给我规划一下路径 | 闲聊（天气/美食） |
| **msg2** | user | **本地生活指南，首页天气，然后列表有附近美食推荐，然后点开可以看到地图定位。风格要热闹活力** | **真实需求（用户口述的建站规格）** |
| msg3 | user | 按照我刚刚的要求给我生成一个网站 | 建站指令 |

用户的"刚刚的要求"= **msg2 那句『本地生活指南…』**。但系统一条都没用上。

## 二、实际链路证据（ai_service.log / business.log）

```
[规则] 命中: build 关键词=['网站']                       ← 规则识别正确
[语义] LLM返回 build/site(90%) checkpoint_relation=resume ← 语义识别正确(网站)
[汇总] 上下文修正 build/site → build/page (原因: 上条在讨论网页) ← ✗ 错误降级
[工具] 技能=agent_build conf=76% → 路由(中置信)          ← ✗ 进了单页建站
[Worker] 路由执行 skill=agent_build doc=无 status=draft   ← doc=无
[gen] Planner 开始 rag=0chars                             ← ✗ 需求零注入
[gen] Planner 完成 title=深圳天气美食 steps=5             ← ✗ 用 msg1 编造
[chat] 流结束 状态=paused events=8 ... output=0字符 preview=False ← ✗ 什么都没生成
```

## 三、根因（4 处）

### RC1 — 建站 Planner 根本没读需求（最致命）
`agent_build.py:generate_stream` 的 Planner 只取 `first_user_msg`（第 1 条用户消息 = msg1 天气闲聊）：
- 第 417-421 行：`for m in messages: if m.get("role")=="user": first_user_msg=...; break` → 永远取 **msg1**。
- `requirement_doc` 形参被签名 `**kwargs`（第 321 行）吞掉，**从未使用**；`conversation_summary` 同理被忽略。
- 结果：`rag=0chars`，Planner 拿 msg1「深圳天气」编出"深圳天气美食"，**完全无视 msg2 的真实需求**。

### RC2 — `build/site` 被错误降级为 `build/page`（单页）
`intent/context.py:105,161` 关键词兜底把对话里 msg2 的"首页/列表/页"匹配成"页面制作"→ 上下文修正 `build/site → build/page`（日志原因"上条在讨论网页"）。
但用户当前消息明确说"网站"，应保留 `build/site` → 走完整建站 `agent_generate_site`，却被降级成单页 `agent_build`。

### RC3 — v1.0.7 死亡路由会二次拦截
`intent/tools.py`（v1.0.7）：`agent_generate_site` 且 `not has_requirement_doc` → 改道回 `agent_requirement`。
即便修好 RC2 让它走 `agent_generate_site`，只要 `doc=无` 又会被打回重做需求。需放宽：当对话里已有可读需求（msg2）时不再拦截。

### RC4 — Planner 之后直接 paused 不生成，用户看到"啥也没发生"
`agent_build.py:463-470`：Planner 出方案后 `yield paused(await_confirm)` 并 `return`，**不进入 Coder**。
于是 `output=0字符 / preview=False`。用户发"生成网站"只收到一个暂停的方案卡，没有网站 → 感觉"没生效"。
（此设计本意是"方案确认"，但：①方案是错的 ②用户语义是"直接生成"而非"先规划"）

## 四、修复方案（待确认后实施）

| # | 修复 | 位置 | 做法 |
|---|------|------|------|
| F1 | Planner/Coder 真正使用需求 | `agent_build.py` + `agent_generate_site.py` | `requirement_doc`/`conversation_summary` 作为显式参数注入 Planner 与 Coder 提示词；无正式文档时用**最近的用户需求消息**（msg2）而非首条消息 |
| F2 | 不降级"网站"→单页 | `intent/pipeline.py` `_aggregate` | 当前消息含『网站/官网/站点/建站』等 site 关键词时，跳过 page 降级修正，保留 `build/site` |
| F3 | 放宽死亡路由 | `intent/tools.py` | 仅当 `not has_requirement_doc` **且** 对话无可读需求时才改道回需求分析；有对话需求则放行建站 |
| F4 | 建站直达 or 明确确认 | `agent_build.py` 的 paused 逻辑 | 见下方待确认项 |

## 五、待确认（行为决策，影响 F4）

用户对"生成网站"的预期是**直接出网站**，还是接受"先看方案再点确认"？
- **方案 A（推荐）**：当指令是"生成/做一个网站"且已有需求时，**跳过 plan 暂停直接生成并预览**，过程中仍展示 plan 节点与文字汇总。
- **方案 B**：保留 plan 确认，但修正方案内容（F1/F2/F3 保证方案正确），并强化"确认并生成"CTA。

> 注：F1/F2/F3 必须做；F4 取决于上面选择。无论哪种，修复后"深圳天气美食"会被正确的"本地生活指南（首页天气+美食列表+地图定位，热闹活力）"取代。
