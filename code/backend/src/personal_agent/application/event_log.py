"""Serialized transactions for per-run event sequence allocation.

``max(sequence) + 1`` is read inside a transaction, so two concurrent writers can
pick the same value and one commit loses to the unique ``(run_id, sequence)``
constraint. The application is a single-instance deployment with one execution
slot, so a process-wide lock around the short event transaction is sufficient and
does not span any network call. It must cover allocation *and* commit; acquiring
it only around the read would not help.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from weakref import WeakKeyDictionary

from personal_agent.adapters.persistence.database import Database

# One lock per running event loop: tests create a loop per test, and a lock must
# never be bound to a loop it is not used in.
_LOCKS: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()


def _lock_for_current_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[loop] = lock
    return lock


@asynccontextmanager
async def event_session(database: Database) -> AsyncIterator[Any]:
    async with _lock_for_current_loop():
        async with database.session() as session:
            yield session
