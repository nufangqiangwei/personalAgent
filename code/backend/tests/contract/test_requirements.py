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
