"""First-run setup requires both policy acknowledgements (T135)."""

from __future__ import annotations

from typing import Any

import httpx

from personal_agent.domain.common import new_id


def _headers() -> dict[str, str]:
    return {"Idempotency-Key": new_id()}


async def test_both_policy_acknowledgements_are_required(
    client: httpx.AsyncClient, api_context: Any,
) -> None:
    status = (await client.get("/api/v1/setup/status")).json()
    policy = status["policy"]
    payload = {
        "policy_version": policy["version"],
        "policy_hash": policy["hash"],
        "explicit_confirm": True,
    }

    rejected = await client.post(
        "/api/v1/setup/acknowledgements",
        json={**payload, "data_policy_acknowledged": True, "budget_policy_acknowledged": False},
        headers=_headers(),
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "POLICY_ACKNOWLEDGEMENT_REQUIRED"
    after = (await client.get("/api/v1/setup/status")).json()
    assert after["policy"]["acknowledged"] is False
    assert after["policy"]["data_policy_acknowledged"] is False
    assert after["policy"]["budget_policy_acknowledged"] is False

    accepted = await client.post(
        "/api/v1/setup/acknowledgements",
        json={**payload, "data_policy_acknowledged": True, "budget_policy_acknowledged": True},
        headers=_headers(),
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["policy"]["acknowledged"] is True
    assert body["policy"]["data_policy_acknowledged"] is True
    assert body["policy"]["budget_policy_acknowledged"] is True
    assert "data_dir" in body and "protected" in body["data_dir"]
