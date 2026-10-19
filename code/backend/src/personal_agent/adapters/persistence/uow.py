"""Unit of work and revision-based CAS helpers.

Every mutable aggregate carries ``revision``; an update uses
``UPDATE ... WHERE id = ? AND revision = ?`` and reports a stale revision as 412
instead of overwriting a concurrent change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.database import Database
from personal_agent.domain.common import AppError, ErrorCategory


class UnitOfWork:
    """One short transaction. Network/model calls must happen outside it."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work is not entered")
        return self._session

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self._database.session() as session:
            self._session = session
            try:
                yield session
            finally:
                self._session = None


async def cas_update[T](
    session: AsyncSession,
    model: type[T],
    *,
    identity: Mapping[str, Any],
    expected_revision: int,
    values: Mapping[str, Any],
    stale_code: str = "REVISION_STALE",
    stale_status: int = 412,
) -> int:
    """Conditional update that increments ``revision`` atomically.

    Returns the new revision. A zero-row update means the row is gone or the
    revision moved; both must be surfaced to the caller, never overwritten.
    """
    conditions = [getattr(model, key) == value for key, value in identity.items()]
    conditions.append(cast(Any, model).revision == expected_revision)
    payload = dict(values)
    payload["revision"] = expected_revision + 1
    statement = (
        update(model)
        .where(*conditions)
        .values(**payload)
        .execution_options(synchronize_session=False)
    )
    result = cast(CursorResult[Any], await session.execute(statement))
    if result.rowcount != 1:
        raise AppError(
            code=stale_code,
            message="the resource changed since it was read; reload and retry",
            category=ErrorCategory.VALIDATION,
            status=stale_status,
            details={"expected_revision": expected_revision},
        )
    return expected_revision + 1


async def get_or_404[T](
    session: AsyncSession,
    model: type[T],
    identifier: str,
    *,
    code: str,
    message: str,
) -> T:
    instance = await session.get(model, identifier)
    if instance is None:
        raise AppError(code=code, message=message, status=404)
    return instance
