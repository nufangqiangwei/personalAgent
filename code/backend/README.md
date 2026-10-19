# Personal-Agent backend (phase 1)

FastAPI + Pydantic v2 + SQLAlchemy 2.0 + Alembic backend for the phase-1
workflow research assistant. Business rules live in `src/personal_agent/domain`,
orchestration in `application`, adapters in `adapters`, and the local Web
boundary in `api` / `bootstrap`.

Run everything through `uv` with CPython 3.12:

```powershell
uv sync --project code/backend --python 3.12 --extra dev
uv run --project code/backend python -m personal_agent.bootstrap.run --mode mock
```

Default tests are offline (fixed clock, seeded random, fixture model/HTTP
responses, temporary data directories). Tests marked `live` require explicit
user authorization and real credentials.

See `specs/001-workflow-research-assistant/` for spec, plan, contracts and data
model, and `.specify/memory/constitution.md` for the non-negotiable rules.
