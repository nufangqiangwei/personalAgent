"""Migration chain and SQLite durability settings (T012).

The test runs the real Alembic environment against a throwaway database, then
checks pragmas, the singleton slot row and the foreign-key restrictions that the
data model relies on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from personal_agent.adapters.persistence.database import Database

BACKEND = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND / "alembic.ini"


def _config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


def test_alembic_upgrades_to_head_and_downgrades(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "mig" / "app.db"
    db_path.parent.mkdir(parents=True)
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("PERSONAL_AGENT_DB_URL", url)

    command.upgrade(_config(url), "head")
    command.downgrade(_config(url), "base")
    command.upgrade(_config(url), "head")

    async def inspect() -> None:
        database = Database(db_path)
        try:
            assert await database.journal_mode() == "wal"
            assert await database.integrity_check() == "ok"
            assert await database.foreign_key_violations() == []
            async with database.session() as session:
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
                from alembic.script import ScriptDirectory

                head = ScriptDirectory.from_config(_config(url)).get_current_head()
                assert revision == head
                slot = await session.scalar(
                    text("SELECT COUNT(*) FROM execution_slot WHERE singleton_id = 1")
                )
                assert slot == 1
                foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))
                assert foreign_keys == 1
        finally:
            await database.dispose()

    asyncio.run(inspect())


def test_tables_present_after_migration(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "mig2" / "app.db"
    db_path.parent.mkdir(parents=True)
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("PERSONAL_AGENT_DB_URL", url)
    command.upgrade(_config(url), "head")

    async def tables() -> set[str]:
        database = Database(db_path)
        try:
            async with database.session() as session:
                rows = await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
                return {row[0] for row in rows}
        finally:
            await database.dispose()

    names = asyncio.run(tables())
    assert {
        "workflow_runs",
        "step_runs",
        "step_attempts",
        "execution_events",
        "execution_slot",
        "budget_ledgers",
        "budget_entries",
        "model_requests",
        "session_links",
        "side_effect_operations",
        "blobs",
        "artifacts",
        "request_records",
    } <= names
