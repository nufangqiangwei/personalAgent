"""SSE and event-history contract tests (T086, backend half).

Only committed events are published, ordered by the run-wide sequence. A reconnect
replays instead of re-executing, a paged backfill matches the stream shape, and a
cleaned cursor produces a ``stream.reset`` control frame rather than a silent gap.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import delete, select

from personal_agent.adapters.persistence.execution_models import ExecutionEvent
from personal_agent.api.events import HEARTBEAT_SECONDS, MAX_BUFFER
from personal_agent.bootstrap.seed import seed


async def _completed_run(client: httpx.AsyncClient, api_context: Any) -> str:
    from personal_agent.adapters.persistence.requirement_models import (
        Confirmation,
        RequirementDraft,
    )
    from personal_agent.application.workflow_registry import WorkflowRegistry
    from personal_agent.domain.common import new_id, stable_hash

    config, context = api_context
    await seed(context.database)
    registry = WorkflowRegistry(context.database, context.clock)
    summaries = await registry.list_summaries()
    version_id = next(
        item["latest_version"]["id"]
        for item in summaries
        if item["name"] == "公开主题资料研究"
    )
    field_values = {
        "topic": {
            "path": "topic",
            "value": "图书馆夜间开放",
            "source": "user",
            "message_id": None,
            "suggestion_id": None,
            "accepted_at": None,
            "explicit_none": False,
        }
    }
    snapshot = {"fields": field_values, "preference_order": [], "output_spec": {}}
    async with context.database.session() as session:
        draft = RequirementDraft(
            id=new_id(),
            selected_version_id=version_id,
            revision=2,
            status="CONFIRMED",
            missing_fields_json=[],
            conflicts_json=[],
            field_values_json=field_values,
            preference_order_json=[],
            output_spec_json={},
            summary_markdown="",
            summary_hash="sha256:summary",
            source_draft_id=None,
            created_at=context.clock.now(),
            updated_at=context.clock.now(),
        )
        session.add(draft)
        await session.flush()
        confirmation = Confirmation(
            id=new_id(),
            draft_id=draft.id,
            draft_revision=1,
            workflow_version_id=version_id,
            summary_hash="sha256:summary",
            input_hash=stable_hash(snapshot),
            budget_policy_hash=config.policy.policy_hash,
            data_policy_version=config.policy.version,
            confirmed_at=context.clock.now(),
            action="START",
            field_snapshot_json=snapshot,
        )
        session.add(confirmation)
        await session.commit()
        confirmation_id = confirmation.id
    response = await client.post(
        "/api/v1/runs",
        json={"confirmation_id": confirmation_id},
        headers={"Idempotency-Key": "events-fixture"},
    )
    assert response.status_code == 201, response.text
    run_id = str(response.json()["id"])
    assert await context.executor.wait(run_id, max_wait=60) == "SUCCEEDED"
    return run_id


async def _events(api_context: Any, run_id: str) -> list[ExecutionEvent]:
    _, context = api_context
    async with context.database.session() as session:
        return list(
            (
                await session.scalars(
                    select(ExecutionEvent)
                    .where(ExecutionEvent.run_id == run_id)
                    .order_by(ExecutionEvent.sequence)
                )
            ).all()
        )


async def _frames(
    api_context: Any, run_id: str, *, cursor: int | None, budget: int = 20
) -> list[str]:
    """Drive the cooperative event loop without a socket."""
    _, context = api_context
    calls = {"count": 0}

    async def is_disconnected() -> bool:
        calls["count"] += 1
        return calls["count"] > budget

    collected: list[str] = []
    from personal_agent.api.events import event_frames

    async for frame in event_frames(
        context.database,
        run_id,
        cursor=cursor,
        is_disconnected=is_disconnected,
        heartbeat_seconds=0.0,
        poll_seconds=0.0,
    ):
        collected.append(frame)
    return collected


async def test_history_is_ordered_paged_and_matches_the_stream_shape(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id = await _completed_run(client, api_context)
    rows = await _events(api_context, run_id)
    assert len(rows) > 1
    sequences = [row.sequence for row in rows]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)

    page = (await client.get(f"/api/v1/runs/{run_id}/event-history?limit=2")).json()
    assert [item["id"] for item in page["items"]] == sequences[:2]
    assert page["next_cursor"] == sequences[1]
    assert page["snapshot_url"] == f"/api/v1/runs/{run_id}"
    first = page["items"][0]["data"]
    assert first["run_id"] == run_id
    assert first["schema_version"] == 1
    assert "payload" in first

    rest = (
        await client.get(
            f"/api/v1/runs/{run_id}/event-history?after={page['next_cursor']}"
        )
    ).json()
    assert [item["id"] for item in rest["items"]] == sequences[2:]
    assert rest["next_cursor"] is None

    # A limit above the maximum is rejected rather than silently truncated.
    too_big = await client.get(f"/api/v1/runs/{run_id}/event-history?limit=1001")
    assert too_big.status_code in {400, 422}
    assert MAX_BUFFER == 1000


async def test_replay_is_safe_and_never_repeats_domain_actions(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id = await _completed_run(client, api_context)
    before = await _events(api_context, run_id)
    snapshot_before = (await client.get(f"/api/v1/runs/{run_id}")).json()

    for _ in range(2):
        replay = (await client.get(f"/api/v1/runs/{run_id}/event-history?after=0")).json()
        assert [item["id"] for item in replay["items"]] == [
            row.sequence for row in before
        ]

    after = await _events(api_context, run_id)
    snapshot_after = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert [row.id for row in after] == [row.id for row in before]
    assert snapshot_after["revision"] == snapshot_before["revision"]
    assert snapshot_after["budget"] == snapshot_before["budget"]


async def test_stream_starts_after_the_snapshot_sequence(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id = await _completed_run(client, api_context)
    rows = await _events(api_context, run_id)
    snapshot = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert snapshot["last_event_sequence"] == rows[-1].sequence

    # Subscribing after the snapshot sequence delivers no duplicate of it.
    after_snapshot = await _frames(
        api_context, run_id, cursor=rows[-1].sequence, budget=3
    )
    assert all(not frame.startswith("id: ") for frame in after_snapshot), after_snapshot
    assert any(frame == ": heartbeat\n\n" for frame in after_snapshot)

    # From an earlier cursor the same committed events are replayed in order.
    replayed = await _frames(api_context, run_id, cursor=rows[0].sequence - 1, budget=3)
    ids = [frame.split("\n", 1)[0] for frame in replayed if frame.startswith("id: ")]
    assert ids == [f"id: {row.sequence}" for row in rows]

    # A header cursor that is not a sequence number is rejected by the route.
    response = await client.get(
        f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "not-a-number"}
    )
    assert response.status_code == 400


async def test_cleaned_history_sends_a_reset_frame(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    run_id = await _completed_run(client, api_context)
    rows = await _events(api_context, run_id)
    assert len(rows) >= 3
    _, context = api_context
    async with context.database.session() as session:
        await session.execute(
            delete(ExecutionEvent).where(
                ExecutionEvent.run_id == run_id,
                ExecutionEvent.sequence < rows[2].sequence,
            )
        )
        await session.commit()

    frames = await _frames(api_context, run_id, cursor=0, budget=3)
    assert frames[0].startswith("event: stream.reset")
    assert f"/api/v1/runs/{run_id}" in frames[0]


async def test_unknown_run_and_bad_cursor_are_rejected(
    client: httpx.AsyncClient, api_context: Any
) -> None:
    unknown = "00000000-0000-4000-8000-000000000000"
    assert (await client.get(f"/api/v1/runs/{unknown}/event-history")).status_code == 404
    run_id = await _completed_run(client, api_context)
    bad = await client.get(
        f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "not-a-number"}
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "EVENT_CURSOR_INVALID"


def test_heartbeat_window_is_fifteen_seconds() -> None:
    assert HEARTBEAT_SECONDS == 15.0
