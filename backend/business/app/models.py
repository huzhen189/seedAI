"""SQLAlchemy 模型。M0 仅 User;Project/Message/UsageLog 后续扩。"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # 邮箱可空: 未提供邮箱的用户存 NULL(唯一索引允许多个 NULL); 去掉 default='' 避免 None 被替换成 ''
    email: Mapped[Optional[str]] = mapped_column(String(128), unique=True, index=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin
    plan: Mapped[str] = mapped_column(String(16), default="free")  # 收费预留
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
