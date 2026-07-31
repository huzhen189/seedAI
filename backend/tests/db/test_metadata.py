from __future__ import annotations

import asyncio

from app.db.schema_check import REQUIRED_TABLES
from app.models import Base

from .conftest import isolated_database


def test_all_terminal_metadata_creates_on_sqlite() -> None:
    async def scenario() -> None:
        async with isolated_database() as (engine, _):
            assert set(Base.metadata.tables) >= REQUIRED_TABLES
            assert "frontend_events" not in Base.metadata.tables
            assert engine.dialect.name == "sqlite"

    asyncio.run(scenario())


def test_statistics_have_no_content_fk_but_usage_ledger_does() -> None:
    statistics = {
        "metrics_daily",
        "metrics_events",
        "qc_scores",
        "flow_checks",
        "output_guard_log",
        "degradations",
        "intent_decisions",
        "model_calls",
        "kb_change_log",
    }
    for table_name in statistics:
        assert not Base.metadata.tables[table_name].foreign_keys

    usage_fk_targets = {
        foreign_key.target_fullname
        for foreign_key in Base.metadata.tables["usage_ledger"].foreign_keys
    }
    assert usage_fk_targets == {"users.id", "conversations.id"}


def test_mysql_engine_pool_guards_are_enabled() -> None:
    from app.db.session import create_engine

    engine = create_engine("mysql+aiomysql://user:password@localhost/seed_ai")
    try:
        assert engine.pool._pre_ping is True
        assert engine.pool._recycle == 1800
    finally:
        asyncio.run(engine.dispose())
