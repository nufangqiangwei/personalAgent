"""Framework-free primitives shared by every layer.

Rules encoded here (see ``specs/001-workflow-research-assistant/data-model.md``):

* business identifiers are UUIDs;
* persisted timestamps are UTC RFC3339 and the UI localises them;
* JSON columns are validated on write *and* read against a schema version;
* every mutable aggregate carries ``revision`` and is updated with CAS;
* errors are structured and never carry credentials or raw chat content.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final


class ErrorCategory(StrEnum):
    """Fixed error taxonomy from the runtime/tool contract."""

    VALIDATION = "VALIDATION"
    PERMISSION = "PERMISSION"
    BUDGET = "BUDGET"
    TIMEOUT = "TIMEOUT"
    DEPENDENCY = "DEPENDENCY"
    INTERRUPTED = "INTERRUPTED"
    INTEGRITY = "INTEGRITY"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


# Error codes published by the REST contract. Keeping them here stops each
# route from inventing a new spelling.
INPUT_INCOMPLETE: Final = "INPUT_INCOMPLETE"
UNACCEPTED_SUGGESTION: Final = "UNACCEPTED_SUGGESTION"
CONFIRMATION_STALE: Final = "CONFIRMATION_STALE"
VERSION_CHANGED: Final = "VERSION_CHANGED"
RUN_BUSY: Final = "RUN_BUSY"
MODEL_BUSY: Final = "MODEL_BUSY"
MODEL_OUTCOME_UNKNOWN: Final = "MODEL_OUTCOME_UNKNOWN"
PREREQUISITE_INVALID: Final = "PREREQUISITE_INVALID"
START_POINT_REQUIRES_CONFIRMATION: Final = "START_POINT_REQUIRES_CONFIRMATION"
BUDGET_ASSISTANCE_REQUIRED: Final = "BUDGET_ASSISTANCE_REQUIRED"
DRAFT_BUDGET_NOT_EXHAUSTED: Final = "DRAFT_BUDGET_NOT_EXHAUSTED"
DRAFT_BUDGET_GRANT_INVALID: Final = "DRAFT_BUDGET_GRANT_INVALID"
DRAFT_BUDGET_CONFLICT: Final = "DRAFT_BUDGET_CONFLICT"
DRAFT_STATE_INVALID: Final = "DRAFT_STATE_INVALID"
DRAFT_NOT_FOUND: Final = "DRAFT_NOT_FOUND"
DRAFT_REVISION_STALE: Final = "DRAFT_REVISION_STALE"
SETUP_STATE_STALE: Final = "SETUP_STATE_STALE"
SIDE_EFFECT_UNRESOLVED: Final = "SIDE_EFFECT_UNRESOLVED"
RUNTIME_INCOMPATIBLE: Final = "RUNTIME_INCOMPATIBLE"
ARTIFACT_UNAVAILABLE: Final = "ARTIFACT_UNAVAILABLE"
DEFINITION_INVALID: Final = "DEFINITION_INVALID"
SETUP_REQUIRED: Final = "SETUP_REQUIRED"
SETUP_INCOMPLETE: Final = "SETUP_INCOMPLETE"
POLICY_CHANGED: Final = "POLICY_CHANGED"
POLICY_ACKNOWLEDGEMENT_REQUIRED: Final = "POLICY_ACKNOWLEDGEMENT_REQUIRED"
CREDENTIAL_INVALID: Final = "CREDENTIAL_INVALID"
CREDENTIAL_UNAVAILABLE: Final = "CREDENTIAL_UNAVAILABLE"
MAINTENANCE_REQUIRED: Final = "MAINTENANCE_REQUIRED"
MAINTENANCE_BUSY: Final = "MAINTENANCE_BUSY"
MAINTENANCE_TRANSITION_BLOCKED: Final = "MAINTENANCE_TRANSITION_BLOCKED"
MAINTENANCE_REVISION_STALE: Final = "MAINTENANCE_REVISION_STALE"
BACKUP_NOT_FOUND: Final = "BACKUP_NOT_FOUND"
BACKUP_INCOMPATIBLE: Final = "BACKUP_INCOMPATIBLE"
BACKUP_VERIFICATION_FAILED: Final = "BACKUP_VERIFICATION_FAILED"
BACKUP_EXPIRED: Final = "BACKUP_EXPIRED"
BACKUP_DELETED: Final = "BACKUP_DELETED"
MANIFEST_HASH_MISMATCH: Final = "MANIFEST_HASH_MISMATCH"
RESTORE_NOT_FOUND: Final = "RESTORE_NOT_FOUND"
RESTORE_BLOCKED: Final = "RESTORE_BLOCKED"
RESTORE_CONFLICT: Final = "RESTORE_CONFLICT"
IDEMPOTENCY_CONFLICT: Final = "IDEMPOTENCY_CONFLICT"


def new_id() -> str:
    """Business identifier: always a UUID, never a sequential integer."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalise any aware/naive datetime to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_z(value: datetime) -> str:
    """UTC RFC3339 representation; the only timestamp format we persist/emit."""
    return to_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Parse an RFC3339 string; naive input is rejected, not guessed."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must carry an explicit UTC offset")
    return parsed.astimezone(UTC)


def canonical_json(value: Any) -> str:
    """Stable serialisation used for hashes and idempotency request hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def content_hash(raw: bytes | str) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return hashlib.sha256(data).hexdigest()


def etag(revision: int) -> str:
    return f'"{revision}"'


@dataclass(slots=True)
class AppError(Exception):
    """A structured, redaction-safe error.

    ``details`` must only ever contain public fields: no secrets, no raw model
    reasoning and no unredacted chat text.
    """

    code: str
    message: str
    category: ErrorCategory = ErrorCategory.VALIDATION
    retryable: bool = False
    status: int = 400
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"

    def payload(self, request_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": _redact(self.details),
            "request_id": request_id,
        }
        return {"error": body}


_SECRET_KEYS = {
    "secret",
    "api_key",
    "apikey",
    "token",
    "authorization",
    "password",
    "credential",
    "reasoning",
}


def _redact(value: Any, depth: int = 0) -> Any:
    """Defensive redaction for arbitrary nested detail payloads."""
    if depth > 6:
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>" if str(key).lower() in _SECRET_KEYS else _redact(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value]
    return value


def validate_document(
    document: Mapping[str, Any],
    *,
    expected_version: int,
    code: str = "JSON_SCHEMA_VERSION_UNSUPPORTED",
) -> dict[str, Any]:
    """Validate a persisted JSON document on read *and* write.

    A document without an explicit `schema_version` or with an unknown one is
    rejected instead of being interpreted optimistically.
    """
    if not isinstance(document, Mapping):
        raise unprocessable(code, "a JSON document must be an object")
    version = document.get("schema_version")
    if version != expected_version:
        raise unprocessable(
            code,
            "the JSON document has an unsupported schema version",
            {"expected": expected_version, "found": version},
        )
    return dict(document)


def not_found(code: str, message: str) -> AppError:
    return AppError(code=code, message=message, status=404)


def conflict(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    category: ErrorCategory = ErrorCategory.VALIDATION,
) -> AppError:
    return AppError(
        code=code,
        message=message,
        status=409,
        details=details or {},
        category=category,
    )


def unprocessable(code: str, message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(code=code, message=message, status=422, details=details or {})


def precondition_required(code: str = "IF_MATCH_REQUIRED") -> AppError:
    return AppError(
        code=code,
        message="If-Match with the current revision is required",
        status=428,
    )


def precondition_failed(code: str, *, status: int = 412) -> AppError:
    return AppError(code=code, message="the provided revision is stale", status=status)


def forbidden(code: str, message: str) -> AppError:
    return AppError(code=code, message=message, category=ErrorCategory.PERMISSION, status=403)


def unavailable(code: str, message: str) -> AppError:
    return AppError(code=code, message=message, category=ErrorCategory.DEPENDENCY, status=503)


def check_revision(expected: int, actual: int, code: str) -> None:
    """CAS guard used by services before a conditional update."""
    if expected != actual:
        raise precondition_failed(code)


@dataclass(frozen=True, slots=True)
class Actor:
    """The local single-user session actor; no accounts, roles or tenants."""

    session_id: str


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None = None


def paginate[T](items: list[T], *, cursor: str | None, limit: int) -> Page[T]:
    """Stable ``created_at + id`` pagination over an already sorted sequence."""
    limit = max(1, min(limit, 100))
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError as exc:
            raise AppError(
                code="INVALID_CURSOR",
                message="cursor is not valid",
                status=400,
            ) from exc
    window = items[start : start + limit]
    next_cursor = str(start + limit) if start + limit < len(items) else None
    return Page(items=window, next_cursor=next_cursor)

