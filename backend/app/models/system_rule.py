"""系统规则双轨模型（MySQL Source-of-Truth × Vector Semantic Index）。

设计（见对话需求 2026-08-04）：
  - **MySQL = 唯一真相源（SoT）**：存规则完整原文 content，刚性、零容错、可审计、可回滚。
    rule_key 是稳定锚（幂等 UPSERT + 审计/回滚）；version 每次内容变更 +1。
  - **向量库 = 语义索引**：只存「摘要 + 关键词」(summary + keywords)，metadata 持 rule_id /
    scope / rule_type / priority / is_active，命中后回查 MySQL 取全文（与记忆 v2 同范式，
    但嵌入文本更丰富——摘要+关键词，召回更准）。
  - 召回链路：语义 ANN(按 scope $in 过滤) → 回 MySQL 取 content → 按 scope 优先级与
    rule_type 仲裁去重 → 字符预算封顶 → 注入系统 Prompt。既避免 prompt 膨胀，又确保规则
    不被遗忘/篡改（向量里根本没有原文）。

scope 取值：global / domain / user / project / session
  - 全局规则 scope=global，scope_ref 空；
  - 域规则 scope=domain，scope_ref=域名(如 "chat"/"site")；
  - 用户规则 scope=user，scope_ref=用户 id；
  - 项目规则 scope=project，scope_ref=项目 id。
rule_type 取值：constraint(硬约束) / guardrail(护栏) / policy(策略) / preference(软偏好)。
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, LongText, TimestampMixin, UnsignedBigInt, enum_type


class SystemRule(Base, TimestampMixin):
    """[TS] created_at/updated_at 由 TimestampMixin 注入。

    系统刚性规则（MySQL 真相行）。向量库只持 (summary+keywords) 索引 + 元数据，命中后
    回查本行取 content 全文。rule_key 唯一锚用于幂等 UPSERT 与审计/回滚。
    """

    __tablename__ = "system_rules"
    __table_args__ = (
        UniqueConstraint("rule_key", name="uq_system_rules_key"),
        Index("ix_system_rules_scope_type", "scope", "rule_type"),
        Index("ix_system_rules_active", "is_active"),
        CheckConstraint("priority BETWEEN 0 AND 100", name="ck_system_rule_priority"),
    )

    id: Mapped[int] = mapped_column(UnsignedBigInt, primary_key=True, autoincrement=True)
    # 稳定锚：幂等 UPSERT / 审计 / 回滚。形如 "global.no_secret_leak" / "domain:site.responsive"。
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 作用域：global / domain / user / project / session
    scope: Mapped[str] = mapped_column(
        enum_type("system_rule_scope", "global", "domain", "user", "project", "session"),
        nullable=False,
    )
    # 作用域引用：global 为空；domain=域名；user=用户 id；project=项目 id。
    scope_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # 规则类型：硬约束 / 护栏 / 策略 / 软偏好（仲裁用）。
    rule_type: Mapped[str] = mapped_column(
        enum_type("system_rule_type", "constraint", "guardrail", "policy", "preference"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # 全文（SoT）：注入 Prompt 用的完整规则文本。
    content: Mapped[str] = mapped_column(LongText(), nullable=False, default="")
    # 摘要（向量嵌入文本之一）：一句话讲清这条规则。
    summary: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # 关键词（向量嵌入文本之一，用 "|" 分隔）：增强语义召回的命中面。
    keywords: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # 同 scope 内排序（越大越靠前）；仲裁时使用。
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    # 审计/回滚：内容变更时 +1（seed 幂等，未变更不升）。
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 软启用：False 则不进召回/不进 Prompt（禁用而非删除，保留审计）。
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


__all__ = ["SystemRule"]
