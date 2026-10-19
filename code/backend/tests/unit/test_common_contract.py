from __future__ import annotations

import pytest

from personal_agent.domain.common import (
    AppError,
    ErrorCategory,
    canonical_json,
    etag,
    iso_z,
    paginate,
    parse_utc,
    stable_hash,
    validate_document,
)


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [2, 3]}) == canonical_json({"a": [2, 3], "b": 1})


def test_stable_hash_is_prefixed_and_reproducible() -> None:
    first = stable_hash({"x": 1})
    second = stable_hash({"x": 1})
    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


def test_utc_round_trip_and_naive_rejection() -> None:
    moment = parse_utc("2026-10-19T12:00:00Z")
    assert iso_z(moment) == "2026-10-19T12:00:00.000000Z"
    assert parse_utc(iso_z(moment)) == moment
    with pytest.raises(ValueError):
        parse_utc("2026-10-19T12:00:00")


def test_error_payload_redacts_secrets_and_reasoning() -> None:
    error = AppError(
        code="DEMO",
        message="failed",
        category=ErrorCategory.VALIDATION,
        details={
            "api_key": "sk-live-secret",
            "nested": {"reasoning": "internal chain", "safe": "ok"},
            "items": [{"password": "p"}],
        },
    )
    payload = error.payload("req-1")
    body = payload["error"]
    assert body["request_id"] == "req-1"
    assert body["details"]["api_key"] == "<redacted>"
    assert body["details"]["nested"]["reasoning"] == "<redacted>"
    assert body["details"]["nested"]["safe"] == "ok"
    assert body["details"]["items"][0]["password"] == "<redacted>"


def test_pagination_is_stable_and_rejects_bad_cursor() -> None:
    items = list(range(5))
    page = paginate(items, cursor=None, limit=2)
    assert page.items == [0, 1]
    assert page.next_cursor == "2"
    assert paginate(items, cursor=page.next_cursor, limit=2).items == [2, 3]
    assert paginate(items, cursor="4", limit=100).next_cursor is None
    with pytest.raises(AppError):
        paginate(items, cursor="not-a-number", limit=2)


def test_etag_is_a_quoted_revision() -> None:
    assert etag(3) == '"3"'


def test_json_documents_are_version_checked_on_read() -> None:
    document = {"schema_version": 1, "payload": {"a": 1}}
    assert validate_document(document, expected_version=1)["payload"] == {"a": 1}
    with pytest.raises(AppError) as error:
        validate_document({"payload": {}}, expected_version=1)
    assert error.value.code == "JSON_SCHEMA_VERSION_UNSUPPORTED"
    with pytest.raises(AppError):
        validate_document({"schema_version": 2}, expected_version=1)
    with pytest.raises(AppError):
        validate_document(["not", "an", "object"], expected_version=1)
