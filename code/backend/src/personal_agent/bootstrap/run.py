"""Process entrypoint: single instance, Proactor loop, no reload.

The production process serves the API (and, once built, the same-origin frontend).
A second instance is refused by an OS-level lock; execution never starts during
boot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from personal_agent.api.app import create_app
from personal_agent.bootstrap.config import load_config
from personal_agent.bootstrap.logging import configure_logging


class InstanceLock:
    """Exclusive byte-range lock on a lock file, held for the process lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - non-Windows developer machine
                import importlib

                fcntl = importlib.import_module("fcntl")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None


def _proactor_policy() -> None:
    if os.name == "nt":
        policy = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
        if policy is not None:
            asyncio.set_event_loop_policy(policy())


async def _diagnose(config: Any) -> dict[str, Any]:
    from personal_agent.bootstrap.lifespan import build_context, mark_interrupted_runs

    context = await build_context(config)
    try:
        interrupted = await mark_interrupted_runs(context.database, context.clock)
        return {
            "mode": config.mode,
            "data_dir": str(config.data_dir),
            "migration_current": context.migration_current,
            "migration_head": context.migration_head,
            "migrations_up_to_date": context.migrations_up_to_date,
            "execution_available": context.execution_available,
            "runtime": context.runtime_capabilities,
            "interrupted_runs": interrupted,
            "unknown_model_request": context.unknown_model_request_id,
        }
    finally:
        await context.database.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="personal-agent")
    parser.add_argument("--mode", choices=["mock", "live"], default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="run startup diagnostics and exit without serving",
    )
    args = parser.parse_args(argv)

    config = load_config(
        mode=args.mode, host=args.host, port=args.port, data_dir=args.data_dir
    )
    configure_logging(level="INFO", log_dir=config.logs_dir)
    _proactor_policy()

    from personal_agent.bootstrap.acl import secure_data_directories
    from personal_agent.domain.common import AppError

    try:
        config.ensure_directories()
        secure_data_directories(config)
    except AppError as error:
        print(f"ACL self-check failed: {error.message}", file=sys.stderr)
        print(json.dumps(error.details, ensure_ascii=False), file=sys.stderr)
        return 4

    if args.check:
        report = asyncio.run(_diagnose(config))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["migrations_up_to_date"]:
            print(
                "migrations are not at head; run: alembic -c code/backend/alembic.ini upgrade head",
                file=sys.stderr,
            )
            return 1
        return 0

    from personal_agent.adapters.harness.runtime import adoption_evidence

    if config.is_live and adoption_evidence(profile_path=config.profile_path) is None:
        print(
            "live mode is disabled: the runtime adoption gate has not passed "
            "(see docs/runtime-adoption.md)",
            file=sys.stderr,
        )
        return 3

    lock = InstanceLock(config.runtime_dir / "instance.lock")
    if not lock.acquire():
        print(
            "another Personal-Agent instance is already running for this data directory",
            file=sys.stderr,
        )
        return 2

    import uvicorn

    try:
        app = create_app(config)
        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_config=None,
            access_log=False,
            loop="asyncio",
        )
    finally:
        lock.release()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
