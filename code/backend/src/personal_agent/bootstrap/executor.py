"""In-process persistent executor for research runs.

The HTTP request only creates the run; a managed asyncio task advances it. Tests
can await ``wait`` to make the outcome deterministic. Shutdown cancels tasks and
leaves interrupted runs for startup diagnostics to mark.
"""

from __future__ import annotations

import asyncio
from typing import Any


class RunExecutor:
    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._tasks: dict[str, asyncio.Task[str]] = {}

    def submit(self, run_id: str) -> None:
        if run_id in self._tasks and not self._tasks[run_id].done():
            return
        task = asyncio.create_task(self._engine.run(run_id), name=f"run:{run_id}")
        self._tasks[run_id] = task

    async def wait(self, run_id: str, max_wait: float = 30.0) -> str:
        task = self._tasks.get(run_id)
        if task is None:
            return "UNKNOWN"
        return await asyncio.wait_for(task, timeout=max_wait)

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
