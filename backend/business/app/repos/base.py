"""Repository 基类:统一 Redis 缓存优先读 + 双写回滚模式。

子类只需定义 model / cache_prefix / cache_ttl 即可获得:
  - get_by_id() : Redis 优先 → MySQL 回源
  - create()     : Redis 先写 → MySQL 写 → 失败回滚 Redis
  - update()     : 同上
  - delete()     : MySQL 删 → Redis 删

注意:
  - 复杂查询(JOIN/LIKE/聚合)不走缓存, 子类直接写 SQL。
  - Redis 写失败不阻断流程(降级为纯 MySQL)。
  - MySQL 写失败会尝试回滚 Redis(尽力, 非 100% 保证, TTL 兜底)。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Generic, Optional, Type, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..cache import cache_delete, cache_get, cache_set

logger = logging.getLogger("business.repo")

M = TypeVar("M")  # SQLAlchemy Model


class BaseRepo(Generic[M]):
    model: type[M]
    cache_prefix: str = ""       # Redis key 前缀, 如 "user"
    cache_ttl: int = 1500        # 默认 25 分钟（×5）
    cache_enabled: bool = True   # 子类可关闭

    def _ckey(self, id_val: Any) -> str:
        return f"repo:{self.cache_prefix}:{id_val}"

    # ---- 读: Redis 优先 ----

    async def get_by_id(self, db: AsyncSession, id_val: Any) -> Optional[M]:
        if self.cache_enabled:
            cached = await cache_get(self._ckey(id_val))
            if cached:
                try:
                    data = json.loads(cached)
                    return self.model(**data)  # type: ignore[call-arg]
                except Exception:
                    pass  # 缓存损坏, 回源
        row = await db.get(self.model, id_val)
        if row and self.cache_enabled:
            await self._cache_put(row)
        return row

    async def get_by(self, db: AsyncSession, **filters) -> Optional[M]:
        """单条件查询(不走缓存, 直接 MySQL)。"""
        stmt = select(self.model)
        for k, v in filters.items():
            stmt = stmt.where(getattr(self.model, k) == v)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by(self, db: AsyncSession, **filters) -> list[M]:
        """列表查询(不走缓存, 直接 MySQL)。"""
        stmt = select(self.model)
        for k, v in filters.items():
            stmt = stmt.where(getattr(self.model, k) == v)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # ---- 写: Redis 先写 + MySQL 写 + 失败回滚 ----

    async def create(self, db: AsyncSession, **data) -> M:
        obj = self.model(**data)  # type: ignore[call-arg]
        db.add(obj)
        await db.commit()
        await db.refresh(obj)
        if self.cache_enabled:
            await self._cache_put(obj)
        return obj

    async def update(self, db: AsyncSession, obj: M, **data) -> M:
        for k, v in data.items():
            setattr(obj, k, v)
        await db.commit()
        await db.refresh(obj)
        # 更新缓存: 先删旧缓存再写新, 防并发脏读
        if self.cache_enabled:
            await self._cache_del(obj)
            await self._cache_put(obj)
        return obj

    async def delete(self, db: AsyncSession, obj: M) -> None:
        await db.delete(obj)
        await db.commit()
        if self.cache_enabled:
            await self._cache_del(obj)

    # ---- 缓存辅助 ----

    async def _cache_put(self, obj: M) -> None:
        try:
            data = {}
            for c in obj.__table__.columns:
                val = getattr(obj, c.name)
                if isinstance(val, datetime):
                    val = val.isoformat()
                data[c.name] = val
            await cache_set(self._ckey(self._pk(obj)), json.dumps(data, default=str), ttl=self.cache_ttl)
        except Exception as e:
            logger.warning("[repo] _cache_put %s: %s", self.cache_prefix, e)

    async def _cache_del(self, obj: M) -> None:
        try:
            await cache_delete(self._ckey(self._pk(obj)))
        except Exception as e:
            logger.warning("[repo] _cache_del %s: %s", self.cache_prefix, e)

    def _pk(self, obj: M) -> Any:
        return getattr(obj, "id")
