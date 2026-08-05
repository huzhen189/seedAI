from __future__ import annotations

import builtins
import logging
from collections.abc import Mapping
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import Select, delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base


logger = logging.getLogger("app.db.repositories.base")

class RepositoryError(RuntimeError):
    def __init__(self, operation: str, model_name: str, detail: str) -> None:
        super().__init__(f"{model_name} 仓储操作 {operation} 失败: {detail}")
        self.operation = operation
        self.model_name = model_name


ModelT = TypeVar("ModelT", bound=Base)


class BaseRepo(Generic[ModelT]):
    model: ClassVar[type[Base]]

    def _model(self) -> type[ModelT]:
        model = self.model
        if not isinstance(model, type) or not issubclass(model, Base):
            raise RepositoryError("配置", self.__class__.__name__, "model 必须是 SQLAlchemy ORM 类型")
        return model

    def _validated_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        model = self._model()
        columns = set(model.__table__.columns.keys())
        unknown = set(values) - columns
        if unknown:
            raise ValueError(f"{model.__name__} 包含未知字段: {', '.join(sorted(unknown))}")
        if "id" in values:
            raise ValueError("不允许通过仓储写入或修改自增主键 id")
        return dict(values)

    def _record_id(self, record: int | ModelT) -> int:
        record_id: object = record if isinstance(record, int) else getattr(record, "id", None)
        if not isinstance(record_id, int) or record_id <= 0:
            raise ValueError("记录必须带有效的正整数 id")
        return record_id

    def _filtered_statement(self, filters: Mapping[str, Any]) -> Select[tuple[ModelT]]:
        model = self._model()
        statement = select(model)
        columns = set(model.__table__.columns.keys())
        unknown = set(filters) - columns
        if unknown:
            raise ValueError(f"{model.__name__} 包含未知查询字段: {', '.join(sorted(unknown))}")
        for name, value in filters.items():
            statement = statement.where(getattr(model, name) == value)
        return statement

    async def get(self, session: AsyncSession, record_id: int) -> ModelT | None:
        if record_id <= 0:
            raise ValueError("record_id 必须为正整数")
        model = self._model()
        try:
            return await session.get(model, record_id)
        except SQLAlchemyError as exc:
            logger.exception("读取 %s id=%s 失败", model.__name__, record_id)
            raise RepositoryError("get", model.__name__, str(exc)) from exc

    async def get_by_id(self, session: AsyncSession, record_id: int) -> ModelT | None:
        return await self.get(session, record_id)

    async def get_by(self, session: AsyncSession, **filters: Any) -> ModelT | None:
        model = self._model()
        try:
            result = await session.execute(self._filtered_statement(filters).limit(2))
            rows = list(result.scalars())
            if len(rows) > 1:
                raise RepositoryError("get_by", model.__name__, "查询返回多于一行")
            return rows[0] if rows else None
        except RepositoryError:
            raise
        except (SQLAlchemyError, ValueError) as exc:
            logger.exception("按条件读取 %s 失败", model.__name__)
            raise RepositoryError("get_by", model.__name__, str(exc)) from exc

    async def list(
        self,
        session: AsyncSession,
        *,
        offset: int = 0,
        limit: int = 100,
        **filters: Any,
    ) -> builtins.list[ModelT]:
        if offset < 0:
            raise ValueError("offset 不得为负数")
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1..1000 之间")
        model = self._model()
        try:
            statement = self._filtered_statement(filters).order_by(model.id.asc())
            result = await session.execute(statement.offset(offset).limit(limit))
            return list(result.scalars().all())
        except (SQLAlchemyError, ValueError) as exc:
            logger.exception("列表查询 %s 失败", model.__name__)
            raise RepositoryError("list", model.__name__, str(exc)) from exc

    async def list_by(self, session: AsyncSession, **filters: Any) -> builtins.list[ModelT]:
        return await self.list(session, **filters)

    async def insert(self, session: AsyncSession, **values: Any) -> ModelT:
        model = self._model()
        try:
            obj = model(**self._validated_values(values))
            session.add(obj)
            await session.flush()
            await session.refresh(obj)
            return obj
        except (SQLAlchemyError, TypeError, ValueError) as exc:
            await self._rollback_after_error(session, "insert", model.__name__, exc)
            raise RepositoryError("insert", model.__name__, str(exc)) from exc

    async def create(self, session: AsyncSession, **values: Any) -> ModelT:
        return await self.insert(session, **values)

    async def update(
        self,
        session: AsyncSession,
        record: int | ModelT,
        *,
        expected_version: int | None = None,
        **values: Any,
    ) -> ModelT:
        record_id = self._record_id(record)
        if not values:
            existing = await self.get(session, record_id)
            if existing is None:
                raise LookupError(f"记录 id={record_id} 不存在")
            return existing
        model = self._model()
        try:
            validated = self._validated_values(values)
            statement = update(model).where(model.id == record_id)
            if expected_version is not None:
                if "version" not in model.__table__.columns:
                    raise ValueError(f"{model.__name__} 不支持乐观锁 version")
                statement = statement.where(model.version == expected_version)
                validated["version"] = expected_version + 1
            result = await session.execute(statement.values(**validated))
            if result.rowcount != 1:
                if expected_version is None:
                    raise LookupError(f"记录 id={record_id} 不存在")
                raise RepositoryError("update", model.__name__, "乐观锁冲突或记录不存在")
            await session.flush()
            # 移除 session.expire_all()：它会把同会话内所有对象（含无关的 project）一并置失效，
            # 导致后续在同步上下文访问其惰性属性时触发 MissingGreenlet（site 发布 / 研究检索同受其害）。
            # 改用 session.refresh 只刷新刚更新的这一条记录，既保证返回值是 DB 最新值，又不误伤其它对象。
            updated = await self.get(session, record_id)
            if updated is not None:
                await session.refresh(updated)
            if updated is None:
                raise RepositoryError("update", model.__name__, "更新后记录不可见")
            return updated
        except RepositoryError:
            raise
        except (SQLAlchemyError, LookupError, ValueError) as exc:
            await self._rollback_after_error(session, "update", model.__name__, exc)
            raise RepositoryError("update", model.__name__, str(exc)) from exc

    async def hard_delete(self, session: AsyncSession, record: int | ModelT) -> bool:
        record_id = self._record_id(record)
        model = self._model()
        try:
            result = await session.execute(delete(model).where(model.id == record_id))
            await session.flush()
            return result.rowcount == 1
        except SQLAlchemyError as exc:
            await self._rollback_after_error(session, "hard_delete", model.__name__, exc)
            raise RepositoryError("hard_delete", model.__name__, str(exc)) from exc

    async def _rollback_after_error(
        self,
        session: AsyncSession,
        operation: str,
        model_name: str,
        error: BaseException,
    ) -> None:
        logger.exception("%s %s 失败: %s", model_name, operation, error)
        try:
            await session.rollback()
        except SQLAlchemyError as rollback_error:
            logger.exception("%s %s 失败后的回滚也失败", model_name, operation)
            raise RepositoryError(operation, model_name, f"{error}; 回滚失败: {rollback_error}") from rollback_error
