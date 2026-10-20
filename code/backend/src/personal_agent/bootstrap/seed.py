"""Idempotent import of the two phase-1 seed workflows.

Only the registry may publish definitions, so seeding goes through validation and
the executable rule instead of writing rows directly. Running the command twice
creates nothing the second time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.workflow_models import Workflow
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.bootstrap.clock import SystemClock
from personal_agent.bootstrap.config import load_config
from personal_agent.bootstrap.lifespan import migration_current, migration_head
from personal_agent.bootstrap.resources import packaged_resource

BACKEND_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = packaged_resource("workflows") or (BACKEND_ROOT / "workflows")


def load_definitions(directory: Path | None = None) -> list[dict[str, Any]]:
    source = directory or WORKFLOW_DIR
    definitions: list[dict[str, Any]] = []
    for path in sorted(source.glob("*.json")):
        with path.open("r", encoding="utf-8") as handle:
            definitions.append(json.load(handle))
    return definitions


async def seed(database: Database, *, directory: Path | None = None) -> dict[str, Any]:
    registry = WorkflowRegistry(database, SystemClock())
    created: list[str] = []
    skipped: list[str] = []
    for definition in load_definitions(directory):
        workflow_id = str(definition["workflow_id"])
        async with database.session() as session:
            existing = await session.scalar(select(Workflow).where(Workflow.id == workflow_id))
        if existing is not None:
            skipped.append(workflow_id)
            continue
        outcome = await registry.create(
            definition=definition,
            change_summary="种子流程初始版本",
            confirm_executable=True,
        )
        created.append(outcome.version.id)
    return {"created": created, "skipped": skipped, "total": len(created) + len(skipped)}


async def _run(*, data_dir: str | None, directory: Path | None) -> int:
    config = load_config(mode="mock", data_dir=data_dir)
    config.ensure_directories()
    database = Database(config.db_path)
    try:
        current = await migration_current(database)
        head = migration_head()
        if head is not None and current != head:
            print(
                f"database is at {current!r}, expected {head!r}; "
                "run: alembic -c code/backend/alembic.ini upgrade head",
                file=sys.stderr,
            )
            return 1
        result = await seed(database, directory=directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        await database.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="personal-agent-seed")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--workflow-dir", default=None)
    args = parser.parse_args(argv)
    directory = Path(args.workflow_dir) if args.workflow_dir else None
    return asyncio.run(_run(data_dir=args.data_dir, directory=directory))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
