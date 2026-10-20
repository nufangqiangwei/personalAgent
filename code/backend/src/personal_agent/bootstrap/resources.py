"""Locate resources shipped inside the installed wheel.

A source checkout keeps workflows, mock observations and Alembic files at the
backend root; an installed wheel carries the same files under
``personal_agent/resources``. Packaged resources win so an installation without
the repository (and without ``tests/fixtures``) still runs.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def packaged_resource(*parts: str) -> Path | None:
    candidate = files("personal_agent").joinpath("resources")
    for part in parts:
        candidate = candidate.joinpath(part)
    try:
        path = Path(str(candidate))
    except (TypeError, ValueError):
        return None
    return path if path.exists() else None
