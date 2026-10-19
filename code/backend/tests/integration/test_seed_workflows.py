"""Seed workflow integration tests (T036).

The two seed definitions must be real, publishable, different in applicability,
cover at least two directions each, and importing them twice must be idempotent.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import func, select

from personal_agent.adapters.persistence.workflow_models import Workflow, WorkflowVersion
from personal_agent.bootstrap.seed import load_definitions, seed
from personal_agent.domain.workflow_validation import validate_definition


def test_both_seed_files_are_publishable_and_distinct() -> None:
    definitions = load_definitions()
    assert len(definitions) == 2
    names = {definition["name"] for definition in definitions}
    assert names == {"技术选型对比", "公开主题资料研究"}

    applicabilities = set()
    for definition in definitions:
        report = validate_definition(definition)
        assert report.valid, report.as_dict()
        assert len(definition["directions"]) >= 2
        direction_ids = {direction["id"] for direction in definition["directions"]}
        assigned = {
            direction
            for step in definition["steps"]
            for direction in step.get("config", {}).get("direction_ids", [])
        }
        assert direction_ids <= assigned, "every direction needs a producing step"
        applicabilities.add(definition["applicability"])
    assert len(applicabilities) == 2, "the seeds must cover different scopes"

    node_types = {step["type"] for definition in definitions for step in definition["steps"]}
    expected_types = {"AgentTask", "ToolTask", "Transform", "Condition", "HumanReview", "Output"}
    assert expected_types <= node_types


async def test_seeding_is_idempotent(database: Any) -> None:
    first = await seed(database)
    assert first["total"] == 2
    assert len(first["created"]) == 2

    second = await seed(database)
    assert second["created"] == []
    assert second["skipped"] == first["created"] or len(second["skipped"]) == 2

    async with database.session() as session:
        workflows = await session.scalar(select(func.count()).select_from(Workflow))
        versions = await session.scalar(select(func.count()).select_from(WorkflowVersion))
    assert workflows == 2
    assert versions == 2


async def test_seeded_workflows_are_executable_and_listed(
    api_context: Any, client: httpx.AsyncClient
) -> None:
    _, context = api_context
    await seed(context.database)

    listing = await client.get("/api/v1/workflows")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 2
    for item in items:
        assert item["latest_version"]["executable"] is True
        assert len(item["directions"]) >= 2
        assert item["applicability"]
        assert item["required_inputs"] == ["topic"]

    names = {item["name"] for item in items}
    assert names == {"技术选型对比", "公开主题资料研究"}


async def test_empty_workspace_is_not_auto_seeded(client: httpx.AsyncClient) -> None:
    listing = await client.get("/api/v1/workflows")
    assert listing.status_code == 200
    assert listing.json()["items"] == []
