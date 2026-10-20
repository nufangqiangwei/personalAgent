"""Workflow versioning contract tests (T028)."""

from __future__ import annotations

import copy
from typing import Any

import httpx

from personal_agent.application.workflow_registry import WorkflowRegistry


async def create_workflow(api_context: Any, definition: dict[str, Any]) -> str:
    _, context = api_context
    registry = WorkflowRegistry(context.database, context.clock)
    outcome = await registry.create(
        definition=definition, change_summary="初始版本", confirm_executable=True
    )
    return outcome.version.workflow_id


def headers(key: str, **extra: str) -> dict[str, str]:
    return {"Idempotency-Key": key, **extra}


async def test_empty_library_returns_no_invented_workflows(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/workflows")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_created_workflow_is_listed_with_its_latest_executable(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    workflow_id = await create_workflow(api_context, workflow_definition)

    listing = await client.get("/api/v1/workflows")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    summary = items[0]
    assert summary["id"] == workflow_id
    assert summary["name"] == workflow_definition["name"]
    assert summary["latest_version"]["version_no"] == 1
    assert summary["required_inputs"] == ["topic"]
    assert summary["disabled"] is False

    detail = await client.get(f"/api/v1/workflows/{workflow_id}")
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"2"'

    versions = await client.get(f"/api/v1/workflows/{workflow_id}/versions")
    assert [item["version_no"] for item in versions.json()["items"]] == [1]
    version_id = versions.json()["items"][0]["id"]
    single = await client.get(f"/api/v1/workflows/{workflow_id}/versions/{version_id}")
    assert single.status_code == 200
    assert single.json()["executable"] is True
    assert single.json()["validation_report"]["valid"] is True


async def test_changed_content_creates_a_new_version_and_reuses_unchanged(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    workflow_id = await create_workflow(api_context, workflow_definition)
    versions = (await client.get(f"/api/v1/workflows/{workflow_id}/versions")).json()["items"]
    base_id = versions[0]["id"]

    changed = copy.deepcopy(workflow_definition)
    changed["description"] = "更新后的描述"
    created = await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={
            "base_version_id": base_id,
            "definition": changed,
            "change_summary": "更新描述",
            "confirm_executable": True,
        },
        headers=headers("save-1"),
    )
    assert created.status_code == 201
    assert created.json()["version_no"] == 2

    unchanged = await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={
            "base_version_id": created.json()["id"],
            "definition": changed,
            "change_summary": "没有实际变化",
            "confirm_executable": True,
        },
        headers=headers("save-2"),
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["created"] is False
    assert unchanged.json()["id"] == created.json()["id"]

    history = (await client.get(f"/api/v1/workflows/{workflow_id}/versions")).json()["items"]
    assert len(history) == 2


async def test_returning_to_historical_content_creates_a_new_version(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    workflow_id = await create_workflow(api_context, workflow_definition)
    versions = (await client.get(f"/api/v1/workflows/{workflow_id}/versions")).json()["items"]
    base_id = versions[0]["id"]

    changed = copy.deepcopy(workflow_definition)
    changed["description"] = "B 内容"
    saved_b = await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={
            "base_version_id": base_id,
            "definition": changed,
            "change_summary": "改为 B",
            "confirm_executable": True,
        },
        headers=headers("save-history-b"),
    )
    assert saved_b.status_code == 201, saved_b.text
    version_b = saved_b.json()
    assert version_b["version_no"] == 2

    # A -> B -> A: content equal to the *historical* first version, but the
    # current baseline is B. This must be a new version, not the old row.
    returned = await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={
            "base_version_id": version_b["id"],
            "definition": copy.deepcopy(workflow_definition),
            "change_summary": "回退到 A 的内容",
            "confirm_executable": True,
        },
        headers=headers("save-history-a"),
    )
    assert returned.status_code == 201, returned.text
    version_a = returned.json()
    assert version_a["version_no"] == 3
    assert version_a["id"] not in {base_id, version_b["id"]}

    history = (await client.get(f"/api/v1/workflows/{workflow_id}/versions")).json()["items"]
    assert [item["version_no"] for item in history] == [3, 2, 1]


async def test_stale_editor_baseline_is_rejected(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    workflow_id = await create_workflow(api_context, workflow_definition)
    versions = (await client.get(f"/api/v1/workflows/{workflow_id}/versions")).json()["items"]
    first_id = versions[0]["id"]

    changed = copy.deepcopy(workflow_definition)
    changed["name"] = "技术选型对比 v2"
    await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={
            "base_version_id": first_id,
            "definition": changed,
            "change_summary": "改名",
            "confirm_executable": True,
        },
        headers=headers("save-3"),
    )

    stale = copy.deepcopy(workflow_definition)
    stale["name"] = "落后编辑器的保存"
    conflict = await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={
            "base_version_id": first_id,
            "definition": stale,
            "change_summary": "旧基线",
            "confirm_executable": True,
        },
        headers=headers("save-4"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CHANGED"


async def test_unconfirmed_draft_does_not_become_the_latest(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    workflow_id = await create_workflow(api_context, workflow_definition)
    versions = (await client.get(f"/api/v1/workflows/{workflow_id}/versions")).json()["items"]
    base_id = versions[0]["id"]

    draft = copy.deepcopy(workflow_definition)
    draft["description"] = "只是草稿"
    response = await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={
            "base_version_id": base_id,
            "definition": draft,
            "change_summary": "草稿",
            "confirm_executable": False,
        },
        headers=headers("save-draft"),
    )
    assert response.status_code == 201
    assert response.json()["executable"] is False

    detail = (await client.get(f"/api/v1/workflows/{workflow_id}")).json()
    assert detail["latest_version"]["version_no"] == 1


async def test_invalid_definition_is_rejected(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    workflow_id = await create_workflow(api_context, workflow_definition)
    versions = (await client.get(f"/api/v1/workflows/{workflow_id}/versions")).json()["items"]

    broken = copy.deepcopy(workflow_definition)
    broken["steps"][0]["policy"]["allowed_tools"] = ["shell.exec"]
    response = await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={
            "base_version_id": versions[0]["id"],
            "definition": broken,
            "change_summary": "非法定义",
            "confirm_executable": True,
        },
        headers=headers("save-invalid"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DEFINITION_INVALID"


async def test_disable_requires_if_match_and_blocks_new_versions(
    client: httpx.AsyncClient, api_context: Any, workflow_definition: dict[str, Any]
) -> None:
    workflow_id = await create_workflow(api_context, workflow_definition)
    detail = (await client.get(f"/api/v1/workflows/{workflow_id}")).json()

    missing = await client.post(
        f"/api/v1/workflows/{workflow_id}/disable",
        json={"reason": "不再需要"},
        headers=headers("disable-1"),
    )
    assert missing.status_code == 428

    stale = await client.post(
        f"/api/v1/workflows/{workflow_id}/disable",
        json={"reason": "不再需要"},
        headers=headers("disable-2", **{"If-Match": '"1"'}),
    )
    assert stale.status_code == 412

    disabled = await client.post(
        f"/api/v1/workflows/{workflow_id}/disable",
        json={"reason": "不再需要"},
        headers=headers("disable-3", **{"If-Match": f'"{detail["revision"]}"'}),
    )
    assert disabled.status_code == 200
    assert disabled.json()["disabled"] is True

    versions = (await client.get(f"/api/v1/workflows/{workflow_id}/versions")).json()["items"]
    changed = copy.deepcopy(workflow_definition)
    changed["name"] = "停用后的修改"
    blocked = await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={
            "base_version_id": versions[0]["id"],
            "definition": changed,
            "change_summary": "停用后保存",
            "confirm_executable": True,
        },
        headers=headers("save-disabled"),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "WORKFLOW_DISABLED"

    listing = (await client.get("/api/v1/workflows")).json()
    assert listing["items"] == []


async def test_missing_workflow_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/workflows/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"
