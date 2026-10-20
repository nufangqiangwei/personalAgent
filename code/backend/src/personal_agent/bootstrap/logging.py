"""Structured logging with a field whitelist and redaction.

Only reviewable facts are logged: identifiers, states, codes and timings. Prompts,
model reasoning, credentials, cookies and raw chat text are never emitted.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from personal_agent.domain.redaction import sanitize

ALLOWED_FIELDS = frozenset(
    {
        "request_id",
        "run_id",
        "root_run_id",
        "step_id",
        "attempt_id",
        "draft_id",
        "operation_id",
        "event",
        "event_sequence",
        "status",
        "reason_code",
        "error_category",
        "code",
        "route",
        "method",
        "duration_ms",
        "mode",
        "provider",
        "version",
        "count",
    }
)

SENSITIVE_KEYS = frozenset(
    {
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "token",
        "cookie",
        "reasoning",
        "messages",
        "content",
        "prompt",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in ALLOWED_FIELDS:
                payload[key] = sanitize(value)
        if record.exc_info:
            payload["error_category"] = "EXCEPTION"
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(*, level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(JsonFormatter())
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "personal-agent.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
