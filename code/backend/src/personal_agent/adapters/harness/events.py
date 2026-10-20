"""Normalisation and redaction of runtime events.

Vendor payloads are untrusted: only whitelisted kinds survive, sensitive keys are
redacted, and model reasoning is always dropped (it may travel in memory for
protocol continuation but is never persisted or emitted).
"""

from __future__ import annotations

import json
from typing import Any, Final

from personal_agent.domain.common import iso_z
from personal_agent.domain.redaction import sanitize
from personal_agent.ports.contracts import Owner, RuntimeEvent

ALLOWED_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "session.started",
        "action.proposed",
        "tool.started",
        "tool.finished",
        "usage.updated",
        "result.available",
        "runtime.failed",
        "runtime.stopped",
    }
)

SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "reasoning",
        "chain_of_thought",
        "thinking",
        "internal_reasoning",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "cookie",
        "set-cookie",
    }
)

MAX_EVENT_PAYLOAD_BYTES: Final = 64 * 1024


def redact(value: Any, *, depth: int = 0) -> Any:
    """Key and text redaction, shared with logs, errors and evidence."""
    return sanitize(value, depth=depth)


def payload_within_limit(payload: dict[str, Any]) -> bool:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return len(encoded) <= MAX_EVENT_PAYLOAD_BYTES


def normalize_event(
    *,
    raw: dict[str, Any],
    owner: Owner,
    session_id: str,
    sequence: int,
    occurred_at: Any,
    submission_id: str | None = None,
    attempt_id: str | None = None,
    epoch: int | None = None,
) -> RuntimeEvent | None:
    """Return a normalised event, or ``None`` for a non-whitelisted kind.

    Returning ``None`` is deliberate: an unknown vendor event is dropped rather
    than stored verbatim.
    """
    kind = str(raw.get("kind", ""))
    if kind not in ALLOWED_EVENT_KINDS:
        return None
    payload = redact(raw.get("payload") or {})
    if not isinstance(payload, dict):
        payload = {"value": payload}
    if not payload_within_limit(payload):
        payload = {"truncated": True, "reason": "payload exceeded the 64 KiB event limit"}
    return RuntimeEvent(
        schema_version=1,
        event_id=str(raw.get("event_id") or f"{session_id}:{sequence}"),
        session_id=session_id,
        sequence=sequence,
        occurred_at=occurred_at,
        kind=kind,
        owner=owner,
        attempt_id=attempt_id,
        submission_id=submission_id,
        epoch=epoch,
        provider_event_key=raw.get("provider_event_key"),
        payload=payload,
    )


def to_wire(
    event: RuntimeEvent,
    *,
    run_id: str,
    workflow_version_id: str | None = None,
) -> dict[str, Any]:
    """SSE data frame body. Never contains reasoning or credentials."""
    return {
        "schema_version": 1,
        "event_id": event.event_id,
        "run_id": run_id,
        "step_run_id": event.payload.get("step_run_id"),
        "attempt_id": event.attempt_id,
        "workflow_version_id": workflow_version_id,
        "sequence": event.sequence,
        "occurred_at": iso_z(event.occurred_at),
        "status": event.payload.get("status"),
        "reason_code": event.payload.get("reason_code"),
        "payload": dict(event.payload),
    }
