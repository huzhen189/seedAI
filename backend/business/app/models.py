"""SQLAlchemy 模型。M0 仅 User;M1 增补 Project/Conversation/Message 支撑对话持久化。"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, JSON
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # 邮箱可空: 未提供邮箱的用户存 NULL(唯一索引允许多个 NULL); 去掉 default='' 避免 None 被替换成 ''
    email: Mapped[Optional[str]] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin | super_admin
    plan: Mapped[str] = mapped_column(String(16), default="free")  # 收费预留
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Project(Base):
    """项目:用户创建的网站生成项目,1—N 会话。"""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(128), default="未命名项目")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    # 分享(⑤-b):share_id 为公开访问令牌(UUID,可空);is_public 控制是否允许公开访问;
    # preview_url 缓存最新一次生成的 COS 预览直链,供「复制预览链接」按钮使用。
    share_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    preview_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 项目级 System Prompt(动态积累: 每次对话后 LLM 提取关键信息追加)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 建站状态: draft → planning → planned → building → built
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
    # 需求文档 JSON(requirement_agent 产出)
    requirement_doc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Conversation(Base):
    """会话:归属某项目,1—N 消息;左栏按 updated_at 排序。"""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 断点续跑(§7): status=paused 表示用户断开但 Worker 已完成当前阶段;
    # checkpoint_stage 标记停在哪个阶段; checkpoint_data 为 JSON 快照;
    # progress_pct 0~100 供前端进度条。
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    # active | paused | completed | aborted | error
    checkpoint_stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # planner_done | coder_done | reviewer_r1 | reviewer_r2 | reviewer_r3
    checkpoint_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # JSON: {"plan":{...}, "html":"...", "attempt":0, "messages":[...]}
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 0 ~ 100
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Message(Base):
    """消息:单条对话内容(user/assistant);SSE 结束双写落库。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | assistant
    content: Mapped[str] = mapped_column(MEDIUMTEXT)  # 16MB, 建站产物可能很大
    model_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 链路 id:同一 trace_id 的多次(重连/续传)SSE 落库据此幂等 —— 用户消息只插一次,
    # assistant 消息按 trace_id upsert,避免刷新/重连导致重复行(§15.3 / 重连机制)。
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------- 对话追踪 / 反馈 / 用量(③-a · 文档 §3.13) ----------
class Trace(Base):
    """一次 SSE 生成会话 = 一个 Trace(可回放 / 质量统计)。"""

    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    model_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|error|aborted
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class TraceEvent(Base):
    """Trace 的结构化事件序列(按 seq 追加),用于前端回放与质量指标。

    token 事件量大,不逐条落库,仅记录聚合 token 数(见 Trace.total_tokens);
    其余结构化事件(node/think/plan/error/done/aborted/degraded)逐条落库。
    """

    __tablename__ = "trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String(16))  # node|think|plan|token|error|done|aborted|degraded
    stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON 字符串
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Feedback(Base):
    """用户对一次生成的评价(1—10 分 + 评论 + 6 维细分);统计 + 回归数据集(文档 §3.11/#36)。"""

    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    rating: Mapped[int] = mapped_column(Integer)  # 1-10 整体评分
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 6 维细分(气泡内多维度星级): {"correctness": int(1-10), ..., "safety": int}
    # 缺省为 None(旧评价 / 未展开评价)
    dimensions: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QcScore(Base):
    """后置 QC 三裁判评分(v0.8.5 M1): 以 trace_id 串联生成, 供后台雷达图复盘。"""

    __tablename__ = "qc_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    model_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # 被评判生成的模型
    overall: Mapped[float] = mapped_column(default=0.0)  # 整体评分(6 维均值平均)
    # 完整 QC 聚合结果(JSON): judges / dimensions(每维 mean+variance+scores) / overall /
    # needs_review / safety_risk / partial
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    safety_risk: Mapped[str] = mapped_column(String(16), default="low")
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UsageLog(Base):
    """每次生成的用量账本(成本归集 / 运营统计,文档 §3.12 / D1)。"""

    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Artifact(Base):
    """生成产物:每次成功生成的最终交付物(文件列表+预览链接),关联到项目。

    同项目每次生成一条 Artifact 记录,右侧产物面板按 project_id 列出所有版本。
    """

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    repo: Mapped[Optional[str]] = mapped_column(String(32), default="site")  # site | code | image | doc
    # files: JSON 数组 [{name, size, url}], 后续多文件生成可扩展
    files: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    preview_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    download_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="uploading")  # uploading | done | failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
