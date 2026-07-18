"""SQLAlchemy 模型。M0 仅 User;Project/Message/UsageLog 后续扩。"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(128), unique=True, index=True, default="")
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin
    plan: Mapped[str] = mapped_column(String(16), default="free")  # 收费预留
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
