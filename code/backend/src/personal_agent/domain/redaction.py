"""Shared redaction for logs, events, errors, evidence and documents.

Two rules apply everywhere: keys that name a credential or model reasoning are
replaced wholesale, and string values are scrubbed for token-shaped text, URL
userinfo and credential-looking query parameters. Redaction is deterministic so
the same secret never reaches the database, SSE, a document or a backup.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "<redacted>"

SENSITIVE_KEY_MARKERS = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "cookie",
        "set-cookie",
        "private_key",
        "client_secret",
        "reasoning",
        "thinking",
        "chain_of_thought",
        "internal_reasoning",
        "analysis",
    }
)

_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
        r"\s*[=:]\s*[^\s,;'\"&]+"
    ),
    re.compile(r"(?i)([?&](?:key|token|secret|password|apikey|api_key)=)[^&\s]+"),
)

_SENSITIVE_QUERY = frozenset(
    {"key", "token", "secret", "password", "apikey", "api_key", "access_token"}
)


def redact_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    redacted = text
    for pattern in _CREDENTIAL_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                match.group(1) + REDACTED if match.groups() else REDACTED
            ),
            redacted,
        )
    return redacted


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_text(url)
    if not parts.scheme or not parts.netloc:
        return redact_text(url)
    host = parts.hostname or ""
    hostpart = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        hostpart = f"{hostpart}:{parts.port}"
    query = [
        (name, REDACTED if name.lower() in _SENSITIVE_QUERY else value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((
        parts.scheme, hostpart, redact_text(parts.path),
        urlencode(query), parts.fragment,
    ))


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in SENSITIVE_KEY_MARKERS


def sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(key): (
                REDACTED
                if _is_sensitive_key(key)
                else sanitize(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _is_reasoning_key(key: Any) -> bool:
    normalized = str(key).strip().lower()
    return any(
        marker in normalized
        for marker in ("reason", "thinking", "chain_of_thought")
    )


def strip_reasoning(value: Any, *, depth: int = 0) -> Any:
    """Drop reasoning/thinking fields while keeping ordinary content."""
    if depth > 10:
        return value
    if isinstance(value, dict):
        return {
            str(key): strip_reasoning(item, depth=depth + 1)
            for key, item in value.items()
            if not _is_reasoning_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [strip_reasoning(item, depth=depth + 1) for item in value]
    return value
