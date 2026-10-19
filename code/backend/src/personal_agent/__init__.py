"""Personal-Agent: workflow-driven information research assistant (phase 1).

Boundaries (see specs/001-workflow-research-assistant/plan.md):

* ``domain`` holds framework-free rules: states, versions, budgets, validation.
* ``application`` orchestrates use cases; only the engine advances business state.
* ``ports`` exposes explicit DTOs and protocols for runtime/tool/persistence.
* ``adapters`` implements those ports (Harness, tools, SQLAlchemy, rendering).
* ``api`` and ``bootstrap`` are the local Web boundary and process lifecycle.
"""

from __future__ import annotations

__version__ = "0.1.0"

APP_NAME = "Personal-Agent"
PACKAGE_NAME = "personal_agent"
