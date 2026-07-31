from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from sqlalchemy import BigInteger, DateTime, Enum, Integer, MetaData, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Text, TypeDecorator


logger = logging.getLogger("app.models.base")

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

UnsignedBigInt = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql").with_variant(
    Integer(), "sqlite"
)
UnsignedSmallInt = Integer().with_variant(mysql.SMALLINT(unsigned=True), "mysql")
UnsignedTinyInt = Integer().with_variant(mysql.TINYINT(unsigned=True), "mysql")

ContentKind = Literal["html", "css", "js", "md", "image", "json", "other"]
ContentStatus = Literal["active", "deleted", "pending"]
ToolName = Literal[
    "web_search",
    "web_fetch",
    "rag_query",
    "html_validate",
    "fs_read",
    "mem_recall",
    "img_generate",
    "fs_write",
    "site_publish",
    "browser_capture",
    "mem_store",
    "site_delete",
    "project_recycle",
    "project_purge",
    "site_deploy",
]


class LongText(TypeDecorator[str]):
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.LONGTEXT())
        return dialect.type_descriptor(Text())


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class ContentPathItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1024)
    uri: HttpUrl | None = None
    kind: ContentKind
    source_tool: ToolName
    status: ContentStatus = "active"
    version: str = Field(pattern=r"^v[1-9][0-9]*$")
    size_bytes: int = Field(ge=0)
    created_at: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        if normalized.startswith(("/", "../")) or "/../" in normalized:
            raise ValueError("content_path.path 必须是安全的相对路径")
        if "://" in normalized:
            raise ValueError("content_path.path 不得包含 URL")
        return normalized


def validate_content_path(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("content_path 必须是 JSON 数组")
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        try:
            model = item if isinstance(item, ContentPathItem) else ContentPathItem.model_validate(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"content_path[{index}] 无效: {exc}") from exc
        validated.append(model.model_dump(mode="json"))
    return validated


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
