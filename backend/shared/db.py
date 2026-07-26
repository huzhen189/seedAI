"""Shared DB engine + models base.

Single facts source for both business (BFF/asset) and agent (inference).
Agent connects to the same MySQL directly via the shared engine (microservice
sharing code, not schema). SQLAlchemy 2.0 async + pre_ping/recycle guard the
cloud-NAT idle-kill that historically caused 500s.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

logger = logging.getLogger("shared.db")

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,            # SELECT 1 before checkout; drop dead conns
    pool_recycle=1800,            # recycle < firewall idle timeout
    pool_size=10,
    max_overflow=20,
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    """FastAPI dependency usable by both business and agent."""
    async with SessionLocal() as session:
        yield session


def _add_missing_columns(sync_conn) -> list[str]:
    """Schema diff: ALTER TABLE ADD COLUMN for any column present in models
    but missing in the live DB. Adds columns only (never drops/changes type)."""
    from .models import Base

    dialect = sync_conn.dialect
    preparer = dialect.identifier_preparer
    inspector = inspect(sync_conn)
    added: list[str] = []
    for tname, table in Base.metadata.tables.items():
        if not inspector.has_table(tname):
            continue
        existing = {c["name"] for c in inspector.get_columns(tname)}
        for col in table.columns:
            if col.name in existing:
                continue
            col_type = col.type.compile(dialect=dialect)
            col_name = preparer.quote(col.name)
            tbl_name = preparer.quote(tname)
            ddl = f"ALTER TABLE {tbl_name} ADD COLUMN {col_name} {col_type}"
            if not col.nullable:
                srv = col.server_default
                default = col.default
                if srv is not None and srv.arg is not None:
                    ddl += f" DEFAULT {srv.arg!r}" if isinstance(srv.arg, str) else f" DEFAULT {srv.arg}"
                elif default is not None and not callable(default.arg):
                    ddl += f" DEFAULT {default.arg!r}"
                else:
                    ddl += " NOT NULL"
            sync_conn.execute(text(ddl))
            added.append(f"{tname}.{col.name} ({col_type})")
    return added


async def init_db():
    """Safe schema bootstrap: create missing tables + add missing columns.
    Does NOT drop; reset is owned by scripts/reset_v2.py."""
    from .models import Base

    async with engine.begin() as conn:
        def _ensure(sync_conn):
            insp = inspect(sync_conn)
            existing = set(insp.get_table_names())
            missing = set(Base.metadata.tables.keys()) - existing
            if missing:
                Base.metadata.create_all(sync_conn)
                logger.info("shared.init_db: created missing tables %s", sorted(missing))
            return existing
        existing = await conn.run_sync(_ensure)
        if existing:
            added = await conn.run_sync(_add_missing_columns)
            if added:
                logger.info("shared.init_db: added missing columns %s", added)
    logger.debug("shared.init_db: schema consistent")


async def get_redis():
    """Lazy redis client (aioredis) — imported here to avoid hard dep at import time."""
    import redis.asyncio as aioredis
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def dispose_engine():
    await engine.dispose()
