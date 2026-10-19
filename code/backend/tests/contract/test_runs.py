"""Run API contract tests (T060).

A confirmed snapshot starts exactly one run; duplicate requests (same key or the
same confirmation with a new key) return the same run, and the run can be read
back with its step attempts.
"""

from __future__ import annotations

from typing import Any

import httpx

from personal_agent.bootstrap.seed import seed


def keys(name: str, **extra: str) -> dict[str, str]:
    return {"Idempotency-Key": name, **extra}


async def confirmed_draft(client: httpx.AsyncClient, api_context: Any) -> tuple[str, str]:
    _, context = api_context
    await seed(context.database)
    workflows = (await client.get("/api/v1/workflows")).json()["items"]
    version = next(
        item for item in workflows if item["name"] == "公开主题资料研究"
    )["latest_version"]

    draft = (
        await client.post(
            "/api/v1/requirements",
            json={"workflow_version_id": version["id"]},
            headers=keys("run-create-draft"),
        )
    ).json()
    await client.post(
        f"/api/v1/requirements/{draft['id']}/messages",
        json={"text": "12 人团队想了解公共图书馆夜间开放"},
        headers=keys("run-message-1"),
    )
    latest = (await client.get(f"/api/v1/requirements/{draft['id']}")).json()
    policies = (await client.get("/policies")).json()
    confirmation = await client.post(
        f"/api/v1/requirements/{draft['id']}/confirm",
        json={
            "draft_revision": latest["revision"],
            "summary_hash": latest["summary_hash"],
            "workflow_version_id": latest["workflow_version_id"],
            "budget_policy_hash": policies["hash"],
            "data_policy_version": policies["version"],
            "action": "START",
            "explicit_start_confirmation": True,
        },
        headers=keys(
            "run-confirm-1", **{"If-Match": f'"{latest["revision"]}"'}
        ),
    )
    assert confirmation.status_code == 201, confirmation.text
    return confirmation.json()["confirmation_id"], latest["workflow_version_id"]


async def test_start_run_returns_a_snapshot_and_completes(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    confirmation_id, _ = await confirmed_draft(client, api_context)

    response = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation_id},
        headers=keys("start-run-1"),
    )
    assert response.status_code == 201, response.text
    snapshot = response.json()
    assert snapshot["status"] in {"RUNNING", "SUCCEEDED"}
    assert snapshot["workflow_version_id"]
    assert snapshot["root_run_id"] == snapshot["id"]
    assert snapshot["confirmed_input_hash"].startswith("sha256:")

    status = await context.executor.wait(snapshot["id"], max_wait=30)
    assert status == "SUCCEEDED"

    final = (await client.get(f"/api/v1/runs/{snapshot['id']}")).json()
    assert final["status"] == "SUCCEEDED"
    assert final["completeness"] in {"COMPLETE", "PARTIAL"}
    assert final["artifact_ids"]
    assert final["budget"]["limits"]["model_requests"] >= 1

    steps = (await client.get(f"/api/v1/runs/{snapshot['id']}/steps")).json()
    step_ids = {item["step_id"] for item in steps["items"]}
    assert {"collect_background", "write_report"} <= step_ids
    assert all(item["status"] in {
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "WAITING_USER",
        "INTERRUPTED",
        "CANCELLED",
        "SKIPPED",
    } for item in steps["items"])


async def test_same_idempotency_key_replays_the_start(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    confirmation_id, _ = await confirmed_draft(client, api_context)
    body = {"confirmation_id": confirmation_id}
    first = await client.post("/api/v1/runs", json=body, headers=keys("start-run-2"))
    second = await client.post("/api/v1/runs", json=body, headers=keys("start-run-2"))
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    await context.executor.wait(first.json()["id"], max_wait=30)


async def test_same_confirmation_with_a_new_key_reuses_the_run(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    confirmation_id, _ = await confirmed_draft(client, api_context)
    first = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation_id},
        headers=keys("start-run-3"),
    )
    second = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation_id},
        headers=keys("start-run-4"),
    )
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    await context.executor.wait(first.json()["id"], max_wait=30)


async def test_run_history_lists_the_run(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    _, context = api_context
    confirmation_id, version_id = await confirmed_draft(client, api_context)
    started = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation_id},
        headers=keys("start-run-5"),
    )
    run_id = started.json()["id"]
    await context.executor.wait(run_id, max_wait=30)

    listing = await client.get("/api/v1/runs")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert any(item["id"] == run_id for item in items)
    entry = next(item for item in items if item["id"] == run_id)
    assert entry["workflow_version_id"] == version_id


async def test_unknown_run_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/runs/00000000-0000-4000-8000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"
