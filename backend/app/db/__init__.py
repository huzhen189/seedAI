from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.models import Base, User

from .schema_check import SchemaReport, SchemaValidationError, check_schema
from .session import SessionLocal, engine, get_db, transaction


logger = logging.getLogger("app.db")


class DestructiveResetDenied(RuntimeError):
    """数据库重建请求不满足安全前置条件。"""


async def _seed_super_admin() -> None:
    account = settings.seed_super_admin.strip()
    if not account:
        return
    try:
        async with transaction() as session:
            result = await session.execute(select(User).where(User.account == account))
            user = result.scalar_one_or_none()
            if user is None:
                logger.warning("SEED_SUPER_ADMIN=%s 对应用户不存在，跳过角色注入", account)
                return
            user.role = "super_admin"
            user.is_super_admin = True
    except Exception as exc:
        logger.exception("超级管理员种子注入失败")
        raise RuntimeError(f"超级管理员种子注入失败: {exc}") from exc


async def init_db() -> SchemaReport:
    try:
        if settings.env == "production":
            report = await check_schema(engine)
        else:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            report = await check_schema(engine)
        await _seed_super_admin()
        return report
    except SchemaValidationError:
        raise
    except Exception as exc:
        logger.exception("数据库初始化失败")
        raise RuntimeError(f"数据库初始化失败: {exc}") from exc


async def reset_db(*, allow_destructive: bool = False) -> dict[str, Any]:
    if settings.env not in {"test", "local"}:
        raise DestructiveResetDenied(
            "reset_db 仅允许 ENV=test/local；生产或 dev 环境请使用经审批的 db/reset_all.py"
        )
    if not allow_destructive:
        raise DestructiveResetDenied(
            "reset_db 默认拒绝破坏性操作；仅隔离测试库可显式传 allow_destructive=True"
        )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        report = await check_schema(engine)
        logger.warning("隔离环境数据库已显式重建，未触碰 Redis、Chroma 或文件存储")
        return {
            "success": True,
            "tables_recreated": len(Base.metadata.tables),
            "redis_cleared": False,
            "chroma_cleared": False,
            "schema": report.as_dict(),
        }
    except Exception as exc:
        logger.exception("隔离环境数据库重建失败")
        raise RuntimeError(f"隔离环境数据库重建失败: {exc}") from exc


__all__ = [
    "DestructiveResetDenied",
    "SchemaReport",
    "SchemaValidationError",
    "SessionLocal",
    "check_schema",
    "engine",
    "get_db",
    "init_db",
    "reset_db",
    "transaction",
]
