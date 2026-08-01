"""新 SeedAI 数据模型公共基础。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import BigInteger, DateTime, Enum, Integer, MetaData, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Text, TypeDecorator


NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

UnsignedBigInt = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql").with_variant(Integer(), "sqlite")
UnsignedSmallInt = Integer().with_variant(mysql.SMALLINT(unsigned=True), "mysql")
UnsignedTinyInt = Integer().with_variant(mysql.TINYINT(unsigned=True), "mysql")


class LongText(TypeDecorator[str]):
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.LONGTEXT())
        return dialect.type_descriptor(Text())


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )


def enum_type(name: str, *values: str) -> Enum:
    if not values:
        raise ValueError(f"枚举 {name} 至少需要一个值")
    return Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=max(len(value) for value in values),
    )
