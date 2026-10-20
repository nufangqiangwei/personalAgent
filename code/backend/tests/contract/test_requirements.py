"""Requirement API contract tests (T041).

Exercises the chat-first flow: create a draft from a selected version, analyze
messages, accept a suggestion, confirm the exact summary snapshot, top up an
exhausted draft ledger, and change selection. Every write checks the local CSRF
boundary and Idempotency-Key provided by the client fixture.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select

from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)


def keys(name: str, **extra: str) -> dict[str, str]:
    return {"Idempotency-Key": name, **extra}


async def pick_version(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    listing = (await client.get("/api/v1/workflows")).json()["items"]
    item = next(entry for entry in listing if entry["name"] == name)
    return item["latest_version"]


async def create_draft(client: httpx.AsyncClient, version_id: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/requirements",
        json={"workflow_version_id": version_id},
        headers=keys(f"create-{version_id}"),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_draft_starts_collecting_without_starting_research(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, "技术选型对比")
    draft = await create_draft(client, version["id"])

    assert draft["status"] == "COLLECTING"
    assert draft["revision"] == 1
    assert draft["missing_fields"] == ["topic"]
    assert draft["summary_hash"].startswith("sha256:")
    assert "## 一、研究要求" in draft["summary_markdown"]
    assert draft["budget_summary"]["limits"] == {"model_requests": 20, "tokens": 30000}
    assert draft["confirmation_id"] is None
    assert draft["messages"] == []

    detail = await client.get(f"/api/v1/requirements/{draft['id']}")
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"1"'

    budget = await client.get(f"/api/v1/requirements/{draft['id']}/budget")
    assert budget.status_code == 200
    assert budget.json()["ledger_scope"] == "DRAFT"
    assert budget.json()["consumed"] == {"model_requests": 0, "tokens": 0}
    assert budget.json()["exhausted"] is False


async def test_chat_analysis_records_messages_and_pending_suggestions(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, "技术选型对比")
    draft = await create_draft(client, version["id"])

    response = await client.post(
        f"/api/v1/requirements/{draft['id']}/messages",
        json={"text": "我们团队想比较几个 AI 编程工具"},
        headers=keys("message-1"),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["message_id"]

    operation = await client.get(f"/api/v1/operations/{body['analysis_operation_id']}")
    assert operation.status_code == 200
    assert operation.json()["status"] == "SUCCEEDED"

    latest = (await client.get(f"/api/v1/requirements/{draft['id']}")).json()
    assert latest["fields"][0]["path"] == "topic"
    assert latest["fields"][0]["source"] == "user"
    pending = [item for item in latest["suggestions"] if item["status"] == "PENDING"]
    assert pending and pending[0]["field_path"] == "audience"
    # A suggestion is not an execution confirmation.
    assert latest["confirmation_id"] is None
    assert latest["status"] == "READY"


async def test_accepting_a_suggestion_does_not_confirm(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, "技术选型对比")
    draft = await create_draft(client, version["id"])
    await client.post(
        f"/api/v1/requirements/{draft['id']}/messages",
        json={"text": "我们团队想比较几个 AI 编程工具"},
        headers=keys("message-suggest"),
    )
    latest = (await client.get(f"/api/v1/requirements/{draft['id']}")).json()
    suggestion = next(item for item in latest["suggestions"] if item["status"] == "PENDING")

    accepted = await client.post(
        f"/api/v1/requirements/{draft['id']}/suggestions/{suggestion['id']}/accept",
        json={"draft_revision": latest["revision"]},
        headers=keys(
            "accept-1", **{"If-Match": f'"{latest["revision"]}"'}
        ),
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    fields = {item["path"]: item for item in body["fields"]}
    assert fields["audience"]["source"] == "suggestion"
    assert body["confirmation_id"] is None
    assert body["status"] in {"READY", "COLLECTING"}


async def test_confirm_binds_summary_revision_and_policies(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, "技术选型对比")
    draft = await create_draft(client, version["id"])
    await client.post(
        f"/api/v1/requirements/{draft['id']}/messages",
        json={"text": "我们团队想比较几个 AI 编程工具"},
        headers=keys("message-2"),
    )
    latest = (await client.get(f"/api/v1/requirements/{draft['id']}")).json()
    suggestion = next(item for item in latest["suggestions"] if item["status"] == "PENDING")
    accepted = await client.post(
        f"/api/v1/requirements/{draft['id']}/suggestions/{suggestion['id']}/accept",
        json={"draft_revision": latest["revision"]},
        headers=keys("accept-2", **{"If-Match": f'"{latest["revision"]}"'}),
    )
    latest = accepted.json()
    await client.post(
        f"/api/v1/requirements/{draft['id']}/messages",
        json={"text": "12 人团队，预算每人每月 40 元，需要私有化部署"},
        headers=keys("message-3"),
    )
    latest = (await client.get(f"/api/v1/requirements/{draft['id']}")).json()
    assert latest["status"] == "READY"

    base_payload = {
        "draft_revision": latest["revision"],
        "summary_hash": latest["summary_hash"],
        "workflow_version_id": latest["workflow_version_id"],
        "budget_policy_hash": (await client.get("/policies")).json()["hash"],
        "data_policy_version": (await client.get("/policies")).json()["version"],
        "action": "START",
        "explicit_start_confirmation": True,
    }
    headers = keys("confirm-1", **{"If-Match": f'"{latest["revision"]}"'})

    missing_header = await client.post(
        f"/api/v1/requirements/{draft['id']}/confirm",
        json=base_payload,
        headers=keys("confirm-no-match"),
    )
    assert missing_header.status_code == 428

    stale = await client.post(
        f"/api/v1/requirements/{draft['id']}/confirm",
        json={**base_payload, "summary_hash": "sha256:stale"},
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONFIRMATION_STALE"

    no_explicit = await client.post(
        f"/api/v1/requirements/{draft['id']}/confirm",
        json={**base_payload, "explicit_start_confirmation": False},
        headers=headers,
    )
    assert no_explicit.status_code == 422

    wrong_policy = await client.post(
        f"/api/v1/requirements/{draft['id']}/confirm",
        json={**base_payload, "budget_policy_hash": "sha256:other"},
        headers=headers,
    )
    assert wrong_policy.status_code == 409
    assert wrong_policy.json()["error"]["code"] == "POLICY_CHANGED"

    confirmed = await client.post(
        f"/api/v1/requirements/{draft['id']}/confirm",
        json=base_payload,
        headers=headers,
    )
    assert confirmed.status_code == 201, confirmed.text
    result = confirmed.json()
    assert result["confirmation_id"]
    assert result["confirmed_input_hash"].startswith("sha256:")
    assert result["action"] == "START"

    after = (await client.get(f"/api/v1/requirements/{draft['id']}")).json()
    assert after["status"] == "CONFIRMED"
    assert after["confirmation_id"] == result["confirmation_id"]


async def test_unaccepted_suggestion_blocks_confirmation(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, "技术选型对比")
    draft = await create_draft(client, version["id"])
    await client.post(
        f"/api/v1/requirements/{draft['id']}/messages",
        json={"text": "我们团队想比较几个 AI 编程工具"},
        headers=keys("message-pending"),
    )
    latest = (await client.get(f"/api/v1/requirements/{draft['id']}")).json()
    policies = (await client.get("/policies")).json()
    blocked = await client.post(
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
        headers=keys("confirm-pending", **{"If-Match": f'"{latest["revision"]}"'}),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "UNACCEPTED_SUGGESTION"


async def test_draft_budget_grant_is_explicit_and_idempotent(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, "技术选型对比")
    draft = await create_draft(client, version["id"])

    # Not exhausted yet: a preventive top-up is refused.
    premature = await client.post(
        f"/api/v1/requirements/{draft['id']}/budget-grants",
        json={
            "draft_revision": draft["revision"],
            "approved_increment": {"model_requests": 5},
            "explicit_grant": True,
            "acknowledge_cost_and_ledger": True,
        },
        headers=keys("grant-premature", **{"If-Match": '"1"'}),
    )
    assert premature.status_code == 409
    assert premature.json()["error"]["code"] == "DRAFT_BUDGET_NOT_EXHAUSTED"

    async with context.database.session() as session:
        ledger = await session.scalar(
            select(BudgetLedger).where(BudgetLedger.draft_id == draft["id"])
        )
        assert ledger is not None
        ledger.limits_json = {"model_requests": 0, "tokens": 0}
        ledger.revision += 1
        await session.commit()

    granted = await client.post(
        f"/api/v1/requirements/{draft['id']}/budget-grants",
        json={
            "draft_revision": draft["revision"],
            "approved_increment": {"model_requests": 5, "tokens": 10000},
            "explicit_grant": True,
            "acknowledge_cost_and_ledger": True,
            "note": "继续补充要求",
        },
        headers=keys("grant-1", **{"If-Match": '"1"'}),
    )
    assert granted.status_code == 201, granted.text
    body = granted.json()
    assert body["ledger_scope"] == "DRAFT"
    assert body["research_started"] is False
    assert body["confirmation_id"] is None
    assert body["run_id"] is None
    assert body["budget"]["limits"] == {"model_requests": 5, "tokens": 10000}

    replay = await client.post(
        f"/api/v1/requirements/{draft['id']}/budget-grants",
        json={
            "draft_revision": draft["revision"],
            "approved_increment": {"model_requests": 5, "tokens": 10000},
            "explicit_grant": True,
            "acknowledge_cost_and_ledger": True,
            "note": "继续补充要求",
        },
        headers=keys("grant-1", **{"If-Match": '"1"'}),
    )
    assert replay.status_code == 201
    assert replay.json()["grant_id"] == body["grant_id"]

    async with context.database.session() as session:
        stored = await session.scalar(
            select(RequirementDraft).where(RequirementDraft.id == draft["id"])
        )
        assert stored is not None
        assert stored.revision > draft["revision"]
        confirmations = (
            await session.scalars(
                select(Confirmation).where(Confirmation.draft_id == draft["id"])
            )
        ).all()
        assert confirmations == []


async def test_change_selection_marks_old_confirmation_stale(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    tech = await pick_version(client, "技术选型对比")
    public = await pick_version(client, "公开主题资料研究")
    draft = await create_draft(client, tech["id"])

    changed = await client.post(
        f"/api/v1/requirements/{draft['id']}/selection",
        json={"workflow_version_id": public["id"], "draft_revision": draft["revision"]},
        headers=keys("selection-1", **{"If-Match": f'"{draft["revision"]}"'}),
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["workflow_version_id"] == public["id"]
    assert body["status"] == "STALE"
    assert body["confirmation_id"] is None


async def _ready_draft(
    client: httpx.AsyncClient, api_context: Any, workflow_name: str = "技术选型对比",
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, workflow_name)
    draft = await create_draft(client, version["id"])
    await client.post(
        f"/api/v1/requirements/{draft['id']}/messages",
        json={"text": "我们团队想比较几个 AI 编程工具"}, headers=keys("ready-1"),
    )
    latest = (await client.get(f"/api/v1/requirements/{draft['id']}")).json()
    suggestion = next(item for item in latest["suggestions"] if item["status"] == "PENDING")
    await client.post(
        f"/api/v1/requirements/{draft['id']}/suggestions/{suggestion['id']}/accept",
        json={"draft_revision": latest["revision"]},
        headers=keys("ready-accept", **{"If-Match": f'"{latest["revision"]}"'}),
    )
    await client.post(
        f"/api/v1/requirements/{draft['id']}/messages",
        json={"text": "12 人团队，预算每人每月 40 元，需要私有化部署"},
        headers=keys("ready-2"),
    )
    latest = (await client.get(f"/api/v1/requirements/{draft['id']}")).json()
    assert latest["status"] == "READY", latest
    return version, draft["id"], latest


def _confirm_body(
    latest: dict[str, Any], policies: dict[str, Any], version_id: str,
) -> dict[str, Any]:
    return {
        "draft_revision": latest["revision"],
        "summary_hash": latest["summary_hash"],
        "workflow_version_id": version_id,
        "budget_policy_hash": policies["hash"],
        "data_policy_version": policies["version"],
        "action": "START",
        "explicit_start_confirmation": True,
    }


async def test_a_crashed_claim_is_never_reexecuted(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    from personal_agent.adapters.persistence.request_models import RequestRecord
    from personal_agent.bootstrap.seed import seed
    from personal_agent.domain.common import new_id, stable_hash

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, "技术选型对比")
    body = {"workflow_version_id": version["id"]}
    route = "/api/v1/requirements"
    raw = httpx.Request("POST", f"http://test{route}", json=body).content
    request_hash = stable_hash({
        "method": "POST", "route": route, "query": "b''",
        "body": raw.decode("utf-8", errors="replace"),
    })
    async with context.database.session() as session:
        session.add(RequestRecord(
            id=new_id(), workspace_id="local", route=route,
            idempotency_key="crash-key", request_hash=request_hash,
            status_code=None, response_json=None, resource_id=None,
            created_at=context.clock.now(), revision=1,
        ))
        await session.commit()
    response = await client.post(route, json=body, headers=keys("crash-key"))
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    async with context.database.session() as session:
        count = await session.scalar(
            select(RequirementDraft).where(
                RequirementDraft.selected_version_id == version["id"]
            )
        )
    assert count is None


async def test_same_key_concurrency_creates_one_draft(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    import asyncio

    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, "技术选型对比")
    body = {"workflow_version_id": version["id"]}
    results = await asyncio.gather(*[
        client.post("/api/v1/requirements", json=body, headers=keys("concurrent-key"))
        for _ in range(2)
    ])
    statuses = sorted(item.status_code for item in results)
    assert 201 in statuses, [item.text for item in results]
    assert all(status in {201, 409} for status in statuses)
    async with context.database.session() as session:
        drafts = (
            await session.scalars(
                select(RequirementDraft).where(
                    RequirementDraft.selected_version_id == version["id"]
                )
            )
        ).all()
    assert len(drafts) == 1


async def test_same_key_with_a_different_request_conflicts(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    tech = await pick_version(client, "技术选型对比")
    public = await pick_version(client, "公开主题资料研究")
    first = await client.post(
        "/api/v1/requirements",
        json={"workflow_version_id": tech["id"]},
        headers=keys("conflict-key"),
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        "/api/v1/requirements",
        json={"workflow_version_id": public["id"]},
        headers=keys("conflict-key"),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_replay_preserves_etag_and_resource(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    from personal_agent.bootstrap.seed import seed

    _, context = api_context
    await seed(context.database)
    version = await pick_version(client, "技术选型对比")
    body = {"workflow_version_id": version["id"]}
    first = await client.post("/api/v1/requirements", json=body, headers=keys("etag-key"))
    assert first.status_code == 201, first.text
    draft_id = first.json()["id"]
    replay = await client.post("/api/v1/requirements", json=body, headers=keys("etag-key"))
    assert replay.status_code == 201
    assert replay.json()["id"] == draft_id
    etag = replay.headers.get("etag")
    assert etag
    assert replay.headers.get("x-idempotent-replay") == "true"
    conditional = await client.post(
        "/api/v1/requirements", json=body,
        headers=keys("etag-key", **{"If-None-Match": etag}),
    )
    assert conditional.status_code == 304


async def test_double_confirmation_is_consumed_once(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    import asyncio

    from sqlalchemy import func

    _, context = api_context
    version, draft_id, latest = await _ready_draft(client, api_context)
    policies = (await client.get("/policies")).json()
    body = _confirm_body(latest, policies, version["id"])
    results = await asyncio.gather(*[
        client.post(
            f"/api/v1/requirements/{draft_id}/confirm", json=body,
            headers=keys(
                f"double-confirm-{index}",
                **{"If-Match": f'"{latest["revision"]}"'},
            ),
        )
        for index in range(2)
    ])
    statuses = sorted(item.status_code for item in results)
    assert statuses[0] == 201 and statuses[1] in {409, 412}, [
        item.text for item in results
    ]
    async with context.database.session() as session:
        count = await session.scalar(
            select(func.count()).select_from(Confirmation)
            .where(Confirmation.draft_id == draft_id)
        )
    assert count == 1


async def test_a_newer_executable_version_requires_reconfirmation(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    import copy

    from personal_agent.application.workflow_registry import WorkflowRegistry

    _, context = api_context
    version, draft_id, latest = await _ready_draft(client, api_context)
    registry = WorkflowRegistry(context.database, context.clock)
    old, _ = await registry.definition_of(version["id"])
    definition = copy.deepcopy(old.definition_json)
    definition["description"] = str(definition.get("description", "")) + "（修订）"
    saved = await registry.save_version(
        workflow_id=old.workflow_id,
        base_version_id=old.id,
        definition=definition,
        change_summary="newer version for reconfirmation",
        confirm_executable=True,
    )
    assert saved.version.id != version["id"]
    policies = (await client.get("/policies")).json()
    response = await client.post(
        f"/api/v1/requirements/{draft_id}/confirm",
        json=_confirm_body(latest, policies, version["id"]),
        headers=keys("newer-version", **{"If-Match": f'"{latest["revision"]}"'}),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "VERSION_CHANGED"
    assert response.json()["error"]["details"]["latest_version_id"] == saved.version.id
