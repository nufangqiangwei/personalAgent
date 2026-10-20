"""Synthetic-secret redaction across text, URLs, events and persisted payloads."""

from __future__ import annotations

from datetime import UTC, datetime

from personal_agent.adapters.harness.events import normalize_event, to_wire
from personal_agent.domain.redaction import (
    REDACTED,
    redact_text,
    redact_url,
    sanitize,
    strip_reasoning,
)
from personal_agent.ports.contracts import Owner

SECRET = "sk-live-abc123456789"


def test_token_shapes_are_redacted() -> None:
    assert SECRET not in redact_text(f"key={SECRET}")
    assert SECRET not in redact_text(f"Authorization: Bearer {SECRET}")
    assert SECRET not in redact_text(f"api_key={SECRET}")
    assert REDACTED in redact_text(f"api_key={SECRET}")


def test_url_userinfo_and_sensitive_query_are_removed() -> None:
    redacted = redact_url(
        "https://user:password@example.org/a?token=abc123&ok=1&api_key=zzz"
    )
    assert "user" not in redacted
    assert "password" not in redacted
    assert "abc123" not in redacted
    assert "zzz" not in redacted
    assert "ok=1" in redacted


def test_nested_keys_and_text_are_sanitized() -> None:
    sanitized = sanitize({
        "api_key": SECRET,
        "token": "abc",
        "nested": {"authorization": "Bearer x", "note": f"secret={SECRET}"},
        "list": [SECRET],
    })
    assert sanitized["api_key"] == REDACTED
    assert sanitized["token"] == REDACTED
    assert sanitized["nested"]["authorization"] == REDACTED
    assert SECRET not in str(sanitized)


def test_reasoning_is_dropped_but_content_kept() -> None:
    stripped = strip_reasoning(sanitize({
        "reasoning": "internal",
        "thinking": "more",
        "output": {"analysis": "a", "value": 1},
    }))
    assert stripped == {"output": {"analysis": REDACTED, "value": 1}}


def test_event_payload_and_wire_never_carry_the_secret() -> None:
    event = normalize_event(
        raw={
            "kind": "result.available",
            "event_id": "e1",
            "payload": {
                "note": f"api_key={SECRET}",
                "token": SECRET,
                "reasoning": "internal",
            },
        },
        owner=Owner(run_id="run-1", attempt_id="attempt-1"),
        session_id="session-1",
        sequence=1,
        occurred_at=datetime(2026, 10, 21, tzinfo=UTC),
    )
    assert event is not None
    assert SECRET not in str(event.payload)
    wire = to_wire(event, run_id="run-1")
    assert SECRET not in str(wire)
    assert wire["payload"]["token"] == REDACTED
