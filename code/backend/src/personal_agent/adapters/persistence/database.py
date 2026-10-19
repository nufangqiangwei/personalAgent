"""Async SQLite engine with the required durability and locking pragmas.

* WAL + ``synchronous=FULL`` + ``foreign_keys=ON`` + 5000 ms busy timeout;
* one short transaction per unit of work (never held across network calls);
* no shared AsyncSession between tasks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    def __init__(self, path: Path, *, echo: bool = False) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{path.as_posix()}",
            echo=echo,
            future=True,
            pool_pre_ping=True,
        )
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            autoflush=False,
        )
        _install_sqlite_pragmas(self.engine)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A fresh session per transaction boundary; caller commits explicitly."""
        session = self.session_factory()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def integrity_check(self) -> str:
        async with self.session() as session:
            result = await session.execute(text("PRAGMA integrity_check"))
            return str(result.scalar_one())

    async def foreign_key_violations(self) -> list[tuple[Any, ...]]:
        async with self.session() as session:
            result = await session.execute(text("PRAGMA foreign_key_check"))
            return [tuple(row) for row in result.fetchall()]

    async def journal_mode(self) -> str:
        async with self.session() as session:
            result = await session.execute(text("PRAGMA journal_mode"))
            return str(result.scalar_one())


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()
