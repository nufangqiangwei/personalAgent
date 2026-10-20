"""Packaged resources must not depend on the source-tree tests/fixtures (T134)."""

from __future__ import annotations

from personal_agent.bootstrap.lifespan import migration_head
from personal_agent.bootstrap.resources import packaged_resource
from personal_agent.bootstrap.seed import load_definitions


def test_packaged_resources_ship_workflows_observations_and_migrations() -> None:
    workflows = packaged_resource("workflows")
    assert workflows is not None
    files = sorted(workflows.glob("*.json"))
    assert len(files) == 2
    assert packaged_resource("observations.json") is not None
    assert packaged_resource("alembic.ini") is not None
    assert packaged_resource("migrations", "env.py") is not None
    assert migration_head() is not None
    assert len(load_definitions()) == 2
