# 记忆模块 v2 落地方案（MySQL Source-of-Truth × Vector Semantic Index）

> 背景：当前实现（2026-08-04 盘点）存在 3 处与原则冲突——
> ① 向量库直接存了原文（`s7` 写 `clean_message` 全文 + `site/service.py` 写 spec 摘要原文），且 `s1` 召回后不回 MySQL 取全文、直接用向量副本；
> ② 双向关联缺失（`metadatas` 只有 `user_id/project_id/conversation_id/kind`，无 `source_type/source_id` 反链；`memory_storage_log` 是空壳）；
> ③ 优先级只靠 prompt 摆放顺序，无代码级裁决，且"强事实"层不存在（城市/禁忌/权限混在 SIR slots 与向量偏好里）。
>
> 本方案将上述 3 件修复 + 用户 6 条新需求一次性收口。

---

## 0. 设计铁律（贯穿全方案）

| 原则 | 落地规则 |
|---|---|
| MySQL = Source of Truth（**主**） | 原文、顺序、事务、强事实（KV 零容错喜好/技术栈）、过程事件全部在 MySQL；向量不可作为原文真相 |
| 向量 = Semantic Index（**辅**） | **只存「嵌入索引 + 元数据」**，不存原文；`documents` 仅放极短索引串/一行摘要；命中后**必须回 MySQL 取原文/摘要** |
| 双向关联 | 向量 `metadata.{source_type, source_id}` ⟷ MySQL 行（`memories`/`project_events`/`user_soft_preferences` 的 `id`）；MySQL 行 `source_message_id` ⟷ `messages.id`（反向溯源原文） |
| 写入 MySQL 为主、向量为辅 | 同步双写 MySQL 事实/记忆/事件行（同事务，不丢）；异步提炼向量（事件驱动，fail-soft，可丢可补） |
| 召回 MySQL 压向量 | L2 MySQL 强事实永远渲染在 L5 向量之前且 prompt 禁止向量覆盖；所有召回最终以 MySQL 事实为准 |
| 优先级代码级裁决 | L2 强事实压 L5；L3 短期顺序精确、不被相似度重排；`user_soft_preference` 仅用于向量召回 rerank，不进 prompt；`project_event` 不进 prompt（先摘要再间接入 L5） |
| LLM 不直接落库（失控防护） | LLM **只输出**固定 Schema 的 JSON（`user_facts/user_prefs/project_facts/project_exps/session_summary`）；MySQL UPSERT 与向量 upsert **全部由代码执行**，禁 LLM 生成 SQL、禁 LLM 直连向量写接口 |
| 抽取不在流内 / 仅稳定结论后 | 记忆写入 LLM 仅在 `s7` 之后异步触发（轮完整结束 / 任务成功 / 显式声明 / 会话压缩），**绝不**在 token 流内做；失败轮、打断轮一律不写入 |
| 压缩提取结构化（标题/正文分离） | 原文在「总计 / Memory Extraction」时经 LLM **压缩提炼为「精简标题 + 正文」两段**；**向量库只存精简标题**（不存正文），MySQL `memories.summary`/`messages.summary` 存「标题+正文」；**多意图按意图分段、分开落库**（每段独立成 memories 行 + 各自一个向量标题，互不打散） |
| **Token 预算封顶（防爆）** | 拼进 prompt 的上下文总量与历史长度**脱钩**：每读层独立硬上限（L1 常量 / L2 按 unique 键不随发言膨胀 / L3 `LIMIT N` 滑动窗口 / L5 `top_k` 常数召回），`serialize_for_llm` 封顶预算信封；超限按 **L5→L3（最旧）** 顺序淘汰，**绝不动 L2**；见 §8 |

---

## 1. 数据模型

### 1.1 新增 MySQL 表（SQLAlchemy 2.0 ORM，与现有 `Mapped`/`enum_type` 风格一致）

> 表名复数（与 `messages`/`projects`/`sir_snapshots` 一致）。枚举用项目统一的
> `enum_type(name, *values)`（native_enum=False + validate_strings）。时间列由
> `TimestampMixin` 自动加 `created_at`/`updated_at`，**不要**手写。外键 `ondelete` 与
> 现有模型保持一致（强归属 CASCADE，可空外键 SET NULL）。

**新增文件 `app/models/memory.py`：**

> `TimestampMixin` 自动为每张表加 `created_at` / `updated_at`（DATETIME），因此
> `UserFact` / `ProjectFact` 已满足"要求#1 必须带 created_at/updated_at"——**不手写**，
> 由 mixin 注入。下方各表注释中 `[TS]` 标记即表示该字段来源于此。

```python
from __future__ import annotations
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, LongText, TimestampMixin, UnsignedBigInt, enum_type


class UserFact(Base, TimestampMixin):
    """[TS] created_at/updated_at 由 TimestampMixin 注入（满足要求#1）。

    用户强事实（KV 表，结构化、强一致、零容错）：喜好/禁忌/权限/地理。
    VARCHAR(512) 只装短事实（如 '城市=深圳'/'禁忌=不要红色'）；模糊经验走
    UserSoftPreference / memories 的 summary/payload。
    """

    __tablename__ = "user_facts"
    __table_args__ = (
        UniqueConstraint("user_id", "category", "key_name", name="uq_user_facts_user_cat_key"),
        Index("ix_user_facts_user", "user_id"),
        Index("ix_user_facts_user_cat", "user_id", "category"),
        CheckConstraint("confidence BETWEEN 0 AND 100", name="ck_user_fact_conf"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(
        enum_type("user_fact_category", "preference", "taboo", "permission", "geo"), nullable=False
    )
    key_name: Mapped[str] = mapped_column(String(64), nullable=False)        # 如 brand_color / city / forbidden_red
    value: Mapped[str] = mapped_column(String(512), nullable=False)          # 如 '#2D8CF0' / '深圳' / 'true'
    source: Mapped[str] = mapped_column(
        enum_type("user_fact_source", "stated", "extracted", "imported"), default="extracted", nullable=False
    )
    confidence: Mapped[int] = mapped_column(Integer, default=90, nullable=False)  # 0-100，低可信度降权


class ProjectFact(Base, TimestampMixin):
    """[TS] created_at/updated_at 由 TimestampMixin 注入（满足要求#1）。

    项目事实（KV 表，结构化、零容错）：技术栈/版本/域名/约束/状态。语义同 UserFact。
    """

    __tablename__ = "project_facts"
    __table_args__ = (
        UniqueConstraint("project_id", "category", "key_name", name="uq_project_facts_proj_cat_key"),
        Index("ix_project_facts_project", "project_id"),
        Index("ix_project_facts_proj_cat", "project_id", "category"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(
        enum_type("project_fact_category", "stack", "version", "domain", "constraint", "status"), nullable=False
    )
    key_name: Mapped[str] = mapped_column(String(64), nullable=False)        # 如 framework / node_version / requires_login
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(
        enum_type("project_fact_source", "stated", "extracted", "imported"), default="extracted", nullable=False
    )


class ProjectEvent(Base, TimestampMixin):
    """[TS] 项目过程记忆 / 审计事件（不进 prompt）。

    记录"发生了什么"（建站/改版/发布/报错/调 API 等），偏审计。事件本身不直接进
    prompt，而是被异步摘要后写进 memories(kind=proj_summary) 再间接入 L5 向量召回。
    这样既能保留可审计的过程轨迹，又不会把噪声事件直接喂给 LLM。
    """

    __tablename__ = "project_events"
    __table_args__ = (
        Index("ix_project_events_project_time", "project_id", "created_at"),
        Index("ix_project_events_project_kind", "project_id", "kind"),
        Index("ix_project_events_source_message", "source_message_id"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(
        enum_type("project_event_kind", "create", "edit", "publish", "error", "api_call", "other"), nullable=False
    )
    detail: Mapped[str] = mapped_column(LongText(), nullable=False)          # 事件原文/结构化载荷
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("messages.id", ondelete="SET NULL")
    )                                                                       # ⟵ 溯源到触发该事件的消息
    embedding_status: Mapped[str] = mapped_column(
        enum_type("event_embedding_status", "pending", "ready", "failed", "skipped"),
        default="pending", nullable=False
    )                                                                       # 经摘要入 memories 后标记 ready


class UserSoftPreference(Base, TimestampMixin):
    """[TS] 用户软偏好（不进 prompt，仅用于向量召回 rerank）。

    场景化经验、跨会话语义偏好（如"做科技风时偏好深色背景""常用三段式结构"）。与
    UserFact 的硬事实区分：软偏好不进入 prompt 强事实段、不参与零容错断言，而是作为
    向量召回命中后的重排序信号（相似度命中后按软偏好匹配度调整顺序/加权）。
    """

    __tablename__ = "user_soft_preferences"
    __table_args__ = (
        Index("ix_user_soft_pref_user", "user_id"),
        Index("ix_user_soft_pref_user_tag", "user_id", "tag"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tag: Mapped[str] = mapped_column(String(64), nullable=False)             # 场景标签，如 style/structure/tone
    content: Mapped[str] = mapped_column(LongText(), nullable=False)         # 软偏好描述
    weight: Mapped[int] = mapped_column(Integer, default=50, nullable=False) # rerank 权重 0-100
    embedding_status: Mapped[str] = mapped_column(
        enum_type("soft_pref_embedding_status", "pending", "ready", "failed"),
        default="pending", nullable=False
    )


class Memory(Base, TimestampMixin):
    """[TS] 长期语义记忆元数据（MySQL 真相行）。

    向量库只持 (source_type, source_id) + 摘要/索引串；命中后回查本行取原文/摘要。
    双向关联：向量 metadata.(source_type, source_id) ⟷ 本行（或 project_events /
             user_soft_preferences 行）；
             本行 source_message_id ⟷ messages.id（反向溯源"这条记忆来自哪条消息"）。
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_user_kind", "user_id", "kind"),
        Index("ix_memories_project_kind", "project_id", "kind"),
        Index("ix_memories_conversation", "conversation_id"),
        Index("ix_memories_source_message", "source_message_id"),
        Index("ix_memories_embedding_status", "embedding_status"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("users.id", ondelete="CASCADE")
    )                                                                       # 用户画像记忆非空；项目记忆可空
    project_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("projects.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(
        enum_type("memory_kind", "preference", "proj_exp", "proj_summary", "conv_summary", "soft_pref"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(
        enum_type("memory_source_type", "message", "project_event", "user_soft_pref"), default="message", nullable=False
    )                                                                       # ⟵ 与向量 metadata.source_type 对齐
    source_message_id: Mapped[int | None] = mapped_column(
        UnsignedBigInt, ForeignKey("messages.id", ondelete="SET NULL")
    )                                                                       # ⟵ 双向关联反向链
    summary: Mapped[str] = mapped_column(LongText(), nullable=False)         # 提炼摘要/原文索引串；向量 document 写同一份
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)  # 可选结构化补充
    embedding_status: Mapped[str] = mapped_column(
        enum_type("memory_embedding_status", "pending", "ready", "failed"), default="pending", nullable=False
    )                                                                       # 事件驱动提炼的状态机
```

**落库必须注册**：五个模型写进 `app/models/memory.py` 后，**必须在 `app/models/__init__.py` 注册**（import + `__all__`），否则 `reset_all` 的 `Base.metadata.create_all` 静默不建这些表（血泪教训，见 MEMORY.md）。`memory`/`project_event`/`user_soft_preference` 表名贴合现有复数约定。

### 1.2 改造现有表 + 向量集合重划分

**A. `messages.content`（保留原文，不强行改摘要）**

现状 `Message.content` 是 `LongText()`（已能装原文）。方案第 6 条（用户需求#3）说
"message 存总结后的文案"，但**短期记忆要 100% 衔接相邻上一句**——若把原文改成 LLM
摘要，相邻轮次衔接语义会失真。取折中：

- **保留 `content` 存原文**（短期记忆保真，这是之前"坪洲接不上"的教训根因侧）；
- **新增 `summary` 列**（可空 `LongText`）单独存本轮 LLM 总结（由 S-extraction 产出），
  供 `memories.source_message_id` 反链回读原文 + 取摘要两件事分离；
- `content_refs`（已有 JSON）继续存 artifact_id 反链，不动。

> 即：`messages` 是"原文真相（短期）+ 摘要（供长期提炼）"双轨，互不干扰。向量只引
> `memories.summary`，不直接碰 `messages.content`。

**B. 给 `messages` 补一列摘要：**

```python
# app/models/content.py · Message 追加
summary: Mapped[str | None] = mapped_column(LongText(), nullable=True)   # L-extraction 产出：f"{title}\n\n{body}"（标题+压缩正文）
```

**C. 向量集合重划分（保留 RUNTIME 白名单，语义去原文化）**

`reset_all.PRESERVED/RUNTIME` 白名单（`RUNTIME_COLLECTIONS`）现有四类：
`project_memory / project_code / conversation_context / user_preferences / memory`。
保持这五个 key 不删（避免破坏 reset 逻辑），但写入语义收紧：

| 集合 | 现在写什么 | v2 写什么（**只索引+元数据，原文在 MySQL**） | 双向关联 |
|---|---|---|---|
| `user_preferences` | 原文偏好句子 | 仅 `user_facts` 行拼成的短摘要（索引串） | `metadata.(source_type,source_id)` ⟷ `memories.id`（fact 派生） |
| `project_memory` | 项目原始上下文 | 仅 `project_facts` + `memories(kind=proj_*)` 摘要（索引串） | 同上 |
| `conversation_context` | 会话轮次原文 | 仅 `memories(kind=conv_summary)` 摘要（索引串） | 同上 |
| `project_code` | 代码快照 | 保持（代码语义不在本次范围内） | 无变化 |
| `memory` | 尚未启用 | 主载体：所有 `memories` 行的 **`title`（精简标题）** 统一写入（索引串），`body` 留 MySQL；按 `kind` 在 metadata 区分 | `metadata.(source_type,source_id)` 必填 |

> 决策：**主载体用 `memory` 集合**（一个集合覆盖所有 kind），其余三类仅作"按业务隔离"
> 的可选镜像。读取 `s1` 命中后统一经 `(source_type, source_id)` 回查 MySQL（`memory_repo.get_by_source`），
> **不再用 `h.text`/`h.document` 当原文**。这样既满足"向量只存索引+元数据"，原文/摘要永远在 MySQL，又能 `source_message_id` 溯源。

### 1.3 向量 `metadatas` 新约定（**只索引 + 元数据，无原文**）

向量库只负责 ANN 检索，承载「嵌入索引 + 元数据」，原文/摘要永远在 MySQL。

```python
# 写入：documents 仅放精简标题（title），禁止放正文/原文长文本
embeddings = [...]                                   # 1024 维
documents  = [row.title or "" for row in rows]       # 只索引 title（≤40 字），body 永不在向量
metadatas  = [{
    "source_type": "message|project_event|user_soft_pref",  # 与 Memory.source_type 对齐
    "source_id":    <对应 MySQL 行 id>,                        # ⟵ 双向关联主键（message→memories.id / event→project_events.id / soft_pref→user_soft_preferences.id）
    "user_id": uid, "project_id": pid, "conversation_id": cid,
    "kind": "preference|proj_exp|proj_summary|conv_summary|soft_pref",
    "source_message_id": <messages.id>,                     # 若有，反向溯源原文
    "embedding_status": "ready",
}]
```

**召回必回 MySQL（要求#5 铁律）：**
```python
for h in hits:
    st, sid = h.metadata["source_type"], int(h.metadata["source_id"])
    # 统一经 source_id 回查 MySQL 取原文/摘要，绝不直接用 h.document 当真相
    if st == "message":          row = memory_repo.get(sid)
    elif st == "project_event":  row = project_event_repo.get(sid)  # 事件原文→摘要后再进 L5
    elif st == "user_soft_pref": row = soft_pref_repo.get(sid)      # 仅用于 rerank，不进 prompt
    text = row.summary  # 或在确实需要原文时 row.source_message_id → messages.content
```

**双向关联闭环校验（启动自检）：** 向量命中返回的 `(source_type, source_id)` 必须在对应
MySQL 表存在，否则记为孤儿向量、记日志（不崩）、跳过。MySQL 行的 `source_message_id`
必须能 `JOIN messages` 取原文，否则降级用 `summary`。

**Rerank 锚点：** 软偏好召回命中后，用 `UserSoftPreference.tag/weight` 对同源 `user_id` 的命中做加权重排（在 `s1` 内完成），但**软偏好文本永不进入 prompt**。

---

## 2. 写入路径（同步双写 + 事件驱动异步提炼）

### 2.1 同步双写（MySQL 为主，保证不丢）

在 `S7PersistStateStage` 内，与本轮回填**同一事务**完成（写入以 MySQL 为主、向量为辅）：

1. `messages` 行（原文 + `summary` 列）— 已存在，补 `summary`。
2. `sir_snapshots`（SIR 状态机）— 已存在。
3. **新增**：`user_facts` / `project_facts` 的结构化行（来自本次提取的结构化事实，UPSERT by unique key，零容错）。
4. **新增**：`project_events` 过程事件行（本轮发生的事，审计，`source_message_id` 反链）。
5. **新增**：`user_soft_preferences` 软偏好行（若提取到场景化经验）。
6. **新增**：`memories` 元数据行（`embedding_status=pending`，`source_type=message` / `project_event` / `user_soft_pref`，`source_message_id` 反链）。
   - 以上 1–6 全在同一 SQLAlchemy session → 要么全成要么全回滚，满足"不丢"。

### 2.2 事件驱动异步提炼（长期记忆维护，向量为辅）

记忆写入只在**本轮回填完全结束、响应已交付之后**触发（见 §2.3 时机）。`s7` 写完 MySQL
同步事实后派发后台任务（沿用现有 `safe_upsert_bg` fail-soft 模式，数据源改为 MySQL 行），
这一步即「记忆写入 LLM」。

#### 固定输出 Schema（要求#2 —— LLM 必须按此结构化 JSON 输出，且只输出 JSON）

```python
# app/llm/extract.py · EXTRACTION_SCHEMA（LLM 输出契约，非 DB 模型）
EXTRACTION_SCHEMA = {
    "user_facts":   [                                        # 用户强事实 → MySQL user_facts
        {"category": "preference|taboo|permission|geo",
         "key_name": str, "value": str, "confidence": int}   # confidence 0-100
    ],
    "user_prefs":   [                                        # 用户软偏好 → MySQL user_soft_preferences（仅 rerank）
        {"tag": str, "content": str, "weight": int}          # weight 0-100
    ],
    "project_facts":[                                        # 项目强事实 → MySQL project_facts
        {"category": "stack|version|domain|constraint|status",
         "key_name": str, "value": str}
    ],
    "project_exps": [                                        # 项目经验/过程记忆 → 语义层（每段：标题+正文）
        {"kind": str, "title": str, "body": str, "payload": dict}
        # title: 精简标题（≤40 字，作为向量索引串）；body: 压缩正文（存 MySQL，不进向量）
    ],
    "session_summary": {                                     # 本轮会话摘要 → messages.summary + 语义层（标题+正文）
        "title": str,                                        # 精简标题（≤40 字，写入向量的 documents）
        "body": str,                                         # 压缩正文（存 MySQL messages.summary / memories.summary）
        "highlights": [str]                                  # 可选要点
    },
}
```

**Schema 字段 → 落库映射（要求#3 —— 代码层解析后分别落库）：**

| Schema 字段 | 落库目标 | 性质 / 层级 |
|---|---|---|
| `user_facts` | MySQL `user_facts`（UPSERT by `(user_id,category,key_name)`，零容错） | 强事实，进 **L2**（最高优先级，压 L5） |
| `user_prefs` | MySQL `user_soft_preferences` + 向量 `memory(kind=soft_pref)` | 软偏好，仅 **rerank**，**不进 prompt** |
| `project_facts` | MySQL `project_facts`（UPSERT） | 强事实，进 **L2** |
| `project_exps` | MySQL `memories(kind=proj_exp)`（`summary`=`标题+正文`）；向量 `memory(kind=proj_exp)` **只存 title**；过程事件另落 `project_events` | 语义层，进 **L5**（命中回 MySQL 取 body） |
| `session_summary` | `messages.summary`=`标题+正文`（本轮压缩摘要，短期衔接） + `memories(kind=conv_summary)`（同存标题+正文） + 向量 **只存 title** | 短期衔接(L3 取原文) + 长期召回(L5 回 MySQL 取 body) |

```python
async def _extract_and_index(context):
    # 1) 调一次 LLM，把本轮 user+assistant+tool 结果压缩提炼为固定 Schema 的 JSON（仅 JSON）
    #    摘要一律被 LLM 拆成 title（精简标题）+ body（压缩正文）两段；
    #    多意图：LLM 在 project_exps/session_summary 内按意图分段输出，每段独立落库。
    extraction = await llm_extract(context)        # 返回 EXTRACTION_SCHEMA 结构，绝不返回 SQL/向量调用
    # 2) 代码层解析后分别落库（MySQL 为主 / 向量为辅）：
    upsert_user_facts(extraction["user_facts"])            # → MySQL user_facts（L2）
    upsert_user_soft_prefs(extraction["user_prefs"])       # → MySQL user_soft_preferences（仅 rerank）
    upsert_project_facts(extraction["project_facts"])      # → MySQL project_facts（L2）
    write_memories_and_events(extraction["project_exps"])  # → MySQL memories(project_exp) + project_events，每段独立
    write_session_summary(extraction["session_summary"])   # → messages.summary(标题+正文) + memories(conv_summary)
    # 3) 每个 MySQL 行就绪后，代码把 "title（精简标题）" + metadatas(source_type,source_id...) upsert 进向量库
    #    —— 向量 documents 只放 title，body 永不进向量；召回经 source_id 回 MySQL 取 body。
    # 4) 标记 embedding_status=ready；失败标 failed
```

**标题/正文分离 & 多意图分存的代码约定：**

- **MySQL 行（`memories.summary` / `messages.summary`）**：存 `f"{title}\n\n{body}"`（标题+正文完整），这是真相，召回回查取到的是这整段。
- **向量 `documents`**：只存 `title`（精简标题，≤40 字），**不存 body**——向量只做 ANN 入口，正文永远回 MySQL 取。对应 §1.3 的 `documents = [row.title ...]`（不再用 `summary[:200]`）。
- **多意图分段**：`_extract_and_index` 对 `project_exps` 列表 / `session_summary` 多段**逐段**写独立 `memories` 行（各自 `source_type=message` / 不同 `kind`），并**逐段**生成独立向量（每个向量只挂该段 title + 自己的 `(source_type, source_id)`）。各意图段互不合并、不打散，召回时按命中段独立回查 MySQL 取对应 body。
- `write_session_summary` 在**多意图**场景下把 `session_summary` 拆为多个 `conv_summary` 行（每段一个 title/body），而非合并成一条。

- **用户画像维护**：`user_facts.category=preference/taboo/geo` → upsert `user_facts`（L2 零容错）；
  `user_prefs` → `user_soft_preferences`（仅 rerank）。
- **项目记忆维护**：`project_facts.category=stack/version/constraint` → `project_facts`（L2 零容错）；
  `project_exps` 先落 `memories(kind=proj_exp)`，过程事件另落 `project_events`，再经摘要间接进 L5
  （事件**不直接**进 prompt）。
- fail-soft：LLM/向量失败只记日志，`embedding_status=failed`，不阻塞用户响应（MySQL 事实已落，不丢）。

#### 红线（要求#4 / #5 —— 不可违反）

- **#4 LLM 不直接落库（失控防护）**：LLM **仅**返回上述结构化 JSON；真正的 MySQL UPSERT 与
  向量 upsert **全部由代码执行**。严禁让 LLM 生成 SQL、严禁让 LLM 直接调用向量库写接口——
  一旦放开，会出现误写/越权/格式漂移且无法审计，与「MySQL=SoT、代码级掌控」铁律相悖。
- **#5 不进 token 流**：记忆抽取**绝不**在每轮流式回答（`chat_completion_stream`）内部做；
  只在 `s7` 之后异步触发。在流里抽会污染实时响应、重复抽取、抬升首 token 延迟，且不易保证
  "稳定结论"前提。

### 2.3 触发时机（何时真正调用「记忆写入 LLM」，要求#1）

记忆写入 LLM 只在**「本轮对话结束 / 工具执行完 / 产生稳定结论」**之后触发。具体四类：

1. **一轮完整结束**：user + assistant + 全部 tool 结果均已收齐（turn 进入 `s7` 且非 partial/中断）。
2. **任务执行成功**：S6 的 action 状态 = `succeeded`（闲聊 / 改代码 / 查资料成功落库）。
   失败 / 超时 / 报错**不触发**——避免把错误结论写进长期记忆。
3. **显式声明**：意图识别命中「记住我习惯… / 记一下… / 以后都…」类指令 → 立即触发；
   被声明的**硬事实**可同步强写 `user_facts`（`source=stated`、高 `confidence`），不等异步。
4. **会话切分 / 摘要**：会话超阈值（消息数 > `chat_recent_limit` 或触及可压缩窗口）触发
   压缩 pass，把旧轮次折叠成 `session_summary` 写入 `memories(kind=conv_summary)` 并裁剪 L3 保序窗口。

> **负向守卫**：用户中途打断、任务失败、异常轮次 → **一律不触发**记忆写入。保证写进去的
> 都是"稳定结论"，呼应要求#1 的「产生稳定结论」前提，且天然满足 #5「不在流内抽」。

---

## 3. 读取路径（跨轮召回 = 三层拼接）

`build_cross_turn_context(user_id, session_id, project_id, query)`：

| 层 | 源 | 方式 | 优先级 |
|---|---|---|---|
| L1 System（静态） | code constant | 拼接 | 静态 |
| **L2 强事实** | **MySQL** `user_facts`+`project_facts` | **精确匹配 / 全量取**，零容错 | 最高，压向量 |
| L3 短期记忆 | **MySQL** `messages` | `ORDER BY id DESC LIMIT N` 再 reverse **保序** | 强（顺序不可乱） |
| L4 跨轮意图 | 本回合填 | `llm_classify_intent` 结果 | 本轮回填 |
| **L5 语义召回** | **向量库 ANN** | `embed(query+intent)` → `search(top_k, filter={user_id,project_id,kind})` → **回 MySQL 取原文/摘要** | 最弱，补充 |

- L2 与 L5 的裁决：prompt 文案明确「L2 已声明的强事实不可被 L5 覆盖」；代码结构保证 L2 段落永远渲染在 L5 之前。**最终以 MySQL 事实为准**。
- L3 顺序由 MySQL 自增 id 强保证，绝不用相似度重排（避免"坪洲"接错上一句的旧坑）。
- **`project_events` 不进 prompt**：过程事件只作为审计轨迹留存，经异步摘要写入 `memories(kind=proj_summary)` 后间接进入 L5；读取时不直接取 `project_events.detail` 拼 prompt。
- **`user_soft_preferences` 不进 prompt**：仅在 `s1` 向量命中后做 rerank 加权（按 `tag` 匹配 query / `weight` 排序），其文本永不进入 LLM 上下文，仅影响 L5 命中顺序。

SIR 最终对象（对齐用户给定伪代码）：

```python
sir = {
    "system": SYSTEM_PROMPT,
    "identity_facts": user_facts + project_facts,   # L2 MySQL（零容错）
    "short_term": recent_messages,                  # L3 MySQL 保序
    "intent": current_intent,                        # L4 本轮回填
    "long_term_recall": reranked_hits,              # L5 向量摘要（经软偏好 rerank，原文回 MySQL）
}
serialize_for_llm(sir)  # L1 包裹最外层
```

---

## 4. LLM 提取/总结阶段（新增，要求#2「按固定 Schema 输出」）

当前 **缺失** 此步骤——`s7` 仅硬编码 `"会话轮次记录：用户说…"`。需新增轻量提取：

- 入口：`S7` 之后的异步 `_extract_and_index`（§2.2），**不在 token 流内**触发（要求#5）。
- 单次 LLM 调用，输出**唯一固定 Schema**（见 §2.2 `EXTRACTION_SCHEMA`）：
  `user_facts` / `user_prefs` / `project_facts` / `project_exps` / `session_summary`，
  且 LLM **只返回 JSON，不返回 SQL / 不直连向量库**（要求#4 红线）。
- 代码层解析后分别落 MySQL（`*_facts` 强事实 → L2）与向量（`title` 精简标题 → L5，`body` 不进向量）；
  落库动作 100% 在代码侧（见 §2.2 落库映射表）。摘要被 LLM 拆成「标题+正文」，多意图分段独立落库。
- 受 config 开关 `settings.memory_extraction_enabled`（默认 True）控制；关闭时降级为纯规则
  （现有 `site/service.py` 的 theme/styles 提取逻辑保留作 fallback）。
- 成本：每轮 1 次小模型调用（token 极小，仅本轮 user+assistant+tool 结果），可接受。

---

## 5. 改动文件清单（落地执行用）

| 文件 | 改动 |
|---|---|
| `app/models/memory.py`（新） | `UserFact` / `ProjectFact` / `ProjectEvent` / `UserSoftPreference` / `Memory`（复数表名） |
| `app/models/content.py` | `Message` 追加 `summary` 列（可空 `LongText`） |
| `app/models/__init__.py` | 注册 5 个新模型（**必须**，否则 `reset_all` 盲区） |
| `app/db/repositories/` | 新增 `user_facts.py` / `project_facts.py` / `project_events.py` / `user_soft_preferences.py` / `memories.py`（含 `get_by_source(source_type, source_id)`） |
| `app/core/stages/s7_persist_state.py` | 同步双写 facts + events + soft_prefs + memories 行；派发 `_extract_and_index` |
| `app/core/stages/s7_persist_state.py` | 新增 `_extract_and_index`（LLM 提取 → upsert facts/soft_prefs → 写 memories + 摘要事件 → 向量） |
| `app/core/stages/s1_recall.py` | 向量命中经 `source_id` 回查 MySQL 取原文/摘要；L2 精确取 facts；软偏好 rerank；事件不直接进 prompt；维持 fail-soft |
| `app/domains/chat/service.py` | `build_cross_turn_context` 实现 L1–L5 三层拼接 + 优先级段顺序 + MySQL 压向量裁决 |
| `app/ragstore/__init__.py` | `upsert` 的 documents 改为索引串/摘要（≤200）；metadatas 强制 `source_type`/`source_id` |
| `app/llm/extract.py`（新） | `llm_extract(context) -> (facts, soft_prefs, memos)` 结构化提取 |
| `app/config/settings.py` | 新增 `memory_extraction_enabled`；`chat_recent_limit`(L3 滑动窗口 N，复用现有)；`memory_token_budget`(§8 信封，建议 5000)；`memory_recall_top_k`(L5 常数召回，建议 5) |
| `app/core/stages/s1_recall.py` 或 `chat/service.py` 的 `serialize_for_llm` | 实现 §8.3 预算信封估算 + 优先级淘汰（L5→L3 最旧→不动 L2/L4） |
| `scripts/reset_all.py` | 新表由 `Base.metadata.create_all` 自动建（只要注册了），reset 时整库 DROP 重建会清掉；过渡期如需兼容旧库需补 Alembic 迁移（见下） |

**上线 SQL 迁移（已有库不丢）：** 五张新表 + `messages.summary` 列需 DDL 迁移，不能只靠
`create_all`（旧库不会自动加表/列）。两种走法：
1. 若项目用 Alembic → 生成 `add_memory_v2_tables` 迁移，含 5 张新表 + `ALTER
   messages ADD summary LONGTEXT`（MySQL 用 `LONGTEXT`，与 `LongText` 类型对齐）。
2. 若仅 `create_all` 兼容模式 → `reset_all` 会 DROP 整库重来（清掉旧数据），**仅限全新环境**；
   生产升级务必走 Alembic 增量迁移，避免误清空。`

---

## 6. 风险与回滚

- **R1 向量去原文化导致召回质量短期下降**：用 `memories.summary` 索引串召回、回 MySQL 取原文，初期摘要质量靠 LLM 提取保障；可双写过渡期（既存摘要也存 source_id），灰度对比。
- **R2 双写事务膨胀**：facts/memory 行写大表，需 batched upsert + 索引；已有 unique key 保证幂等。
- **R3 LLM 提取偶发失败**：fail-soft + `embedding_status=2` 重试任务；MySQL 事实不丢。
- **R4 ORM 未注册盲区**（血泪教训）：新增模型必须在 `app/models/__init__.py` 注册，否则 `reset_all` 静默不建表。
- **回滚**：config 关 `memory_extraction_enabled` 即回到纯规则写入；向量写入保留 fail-soft，单点故障不影响主链路。

---

## 7. 验证

1. 单元：构造 message → 跑 `build_cross_turn_context` 断言 L2/L3 精确、L5 为摘要、顺序保序。
2. 集成：起服务跑一轮「建站+改风格+闲聊」，查日志（已增强的全链路日志 `9cd6fef`）确认：
   - SIR 六层拼接顺序正确（L2 MySQL 强事实在最前、压 L5 向量）；
   - 向量 `metadatas` 含 `(source_type, source_id)`，且 `documents` 仅为标题、无正文/原文；MySQL `memories.summary` 为「标题+正文」；多意图拆成多个独立 `conv_summary`/`proj_exp` 行各自带向量；
   - `memories`/`project_events`/`user_soft_preferences` 表有行、`user_facts` 有结构化事实；
   - L2 facts 在下一轮精确命中且不依赖向量；`user_soft_preferences` 仅参与 rerank、文本不进 prompt。
3. 双写不丢：强制向量库宕机，确认 MySQL facts/memory 行仍落、主响应正常。

---

## 8. Token 预算与防爆（记忆越来越长怎么办）

> 问题本质：历史越长，拼进 prompt 的上下文越胀 → token 爆炸。本方案的防爆哲学是
> **写时压缩 + 读时封顶 + 定期折叠**，让"实际用于 prompt 的上下文体量"从结构上与历史长度**解耦**——
> 历史可无限增长，但每一读层都有硬上限，最终塞进 prompt 的只是一个**固定预算信封**（设计目标 ≈ 5k token）。
> 暴涨的根因（把越来越长的原文原样塞进去）从 §2.2 压缩提取第一步就被掐掉。

### 8.1 三层闸：写时压缩 / 读时封顶 / 定期折叠

| 阶段 | 机制 | 作用 |
|---|---|---|
| **写时压缩** | §2.2 LLM 提取只产出「title + body」摘要，body 已是压缩版；向量 `documents` 只存 title（≤40 字） | 仓库里不存膨胀物；embedding 便宜、召回载荷极小 |
| **读时封顶** | `serialize_for_llm(sir)` 每层独立 `LIMIT`/`top_k`，最后按预算信封裁剪（见 8.2） | 无论语料多长，拼进 prompt 的总量有常数上界 |
| **定期折叠** | 触发时机#4：会话消息数 > `chat_recent_limit` 时，旧轮次折叠成 `session_summary`（`conv_summary` 行），L3 保序窗口下滑裁剪 | 无限会话也不爆：早轮退化为 L5 几条压缩摘要，L3 永远只留最近 N 条原文 |

### 8.2 各读层硬上限（建议值，写入 config 可配）

| 层 | 来源 | 封顶方式 | 建议上限 | 是否随历史增长 |
|---|---|---|---|---|
| L1 System（静态） | 代码常量 | 固定 | ~0.3k token | 否 |
| **L2 强事实** | MySQL `user_facts`+`project_facts` | 按 `(user_id,category,key_name)` unique 键 UPSERT → 行数受"不同事实种类数"约束，**不受发言次数约束**；单值 `VARCHAR(512)` 锁死 | 行数 ≈ 事实种类数（通常 < 50） | 否（幂等） |
| L3 短期记忆 | MySQL `messages` | `ORDER BY id DESC LIMIT N` 保序窗口，窗口外滑掉 | N = `chat_recent_limit`（建议 8） | 否（滑动） |
| L4 跨轮意图 | 本轮回填 | 极小 | < 0.2k | 否 |
| **L5 语义召回** | 向量库 ANN | `search(top_k, filter)`，每条只回 title + 短 summary | top_k = 5（建议） | 否（常数召回） |

> 关键不变量：**L2 与 L5 都是有界集合**——L2 由 unique 键天然封顶，L5 由 `top_k` 封顶。
> 因此哪怕用户聊 1 万轮，L2 仍是那几十行事实、L5 仍是 5 条命中，主干 prompt 体量几乎不变。

### 8.3 统一预算信封 + 优先级淘汰（安全阀）

`serialize_for_llm(sir)` 在拼装末尾估算 token（用项目统一 tokenizer 或粗略 `len(text)//4`），
若超信封 `TOKEN_BUDGET`（建议 5_000）：

```
def serialize_for_llm(sir):
    parts = [L1, L2, L3, L4, L5]         # 按优先级顺序排列
    budget = settings.memory_token_budget
    used = estimate_tokens(parts)
    if used > budget:
        # 优先级淘汰：先丢最弱，绝不动 L2
        parts = evict_by_priority(parts, budget)   # 顺序：L5 → L3(最旧) → 不动 L2/L4
    return render(parts)
```

淘汰顺序（与 §3 优先级一致）：

1. **先丢 L5**（最弱，向量摘要只是补充）—— 砍到 0 仍不超再往下；
2. **再砍 L3 最旧轮次**（保序窗口从 N 收到更小，如 8→4）—— 但保留最近若干轮；
3. **绝不动 L2 / L4** —— 强事实与本轮意图永不因预算被牺牲。

### 8.4 防爆验证（并入 §7）

- 构造超长历史（> 200 轮）跑 `build_cross_turn_context` + `serialize_for_llm`，断言：
  - 输出 token 估算 ≤ `TOKEN_BUDGET`（信封成立，与轮数无关）；
  - L2 段落完整保留、位于最前；L3 截断后顺序仍正确（最近轮在最前）；L5 命中数 ≤ `top_k`。
- 压测：徒增 `messages` 到 1 万行，确认单轮 `serialize_for_llm` 耗时与输出体量不随总行数线性增长（L3 `LIMIT` / L5 `top_k` 生效）。
