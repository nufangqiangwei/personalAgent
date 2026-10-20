"""Opaque keyset cursors bound to the current filter.

A cursor names the last ``(sort_key, id)`` pair of the previous page plus a hash of
the filter it was produced for. Offset cursors silently skip or repeat rows when a
record is inserted or deleted between pages; keyset cursors do not.
"""

from __future__ import annotations

import base64
import json

from personal_agent.domain.common import AppError, stable_hash

CURSOR_VERSION = 1


def _filter_digest(filter_key: str) -> str:
    return stable_hash({"filter": filter_key})


def encode_cursor(*, sort_key: str, item_id: str, filter_key: str) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "f": _filter_digest(filter_key),
        "k": [str(sort_key), str(item_id)],
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, filter_key: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as error:
        raise AppError(
            code="INVALID_CURSOR",
            message="cursor is not valid for this list",
            status=400,
        ) from error
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise AppError(
            code="INVALID_CURSOR", message="cursor version is not supported", status=400
        )
    key = payload.get("k")
    if (
        not isinstance(key, list)
        or len(key) != 2
        or not all(isinstance(part, str) for part in key)
    ):
        raise AppError(code="INVALID_CURSOR", message="cursor key is invalid", status=400)
    if payload.get("f") != _filter_digest(filter_key):
        raise AppError(
            code="CURSOR_FILTER_MISMATCH",
            message="this cursor belongs to a different filter; reload the first page",
            status=409,
        )
    return key[0], key[1]
