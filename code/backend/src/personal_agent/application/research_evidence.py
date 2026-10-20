"""Evidence aggregation and hard-constraint semantics.

Rules enforced here:

* every declared direction appears in the result, with ``covered`` findings or an
  explicit ``missing`` gap;
* important external facts carry source ids, otherwise they are marked
  ``UNVERIFIED`` rather than silently trusted;
* candidate constraints are tri-state (SATISFIED/VIOLATED/UNKNOWN); only
  candidates with every constraint SATISFIED become qualified recommendations;
* ordering uses only the confirmed priority, never an invented numeric weight.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.research_models import (
    Evidence as EvidenceRow,
)
from personal_agent.adapters.persistence.research_models import (
    ResearchAction as ResearchActionRow,
)
from personal_agent.adapters.persistence.research_models import Source
from personal_agent.domain.common import new_id
from personal_agent.domain.redaction import redact_url, sanitize

VALID_CLASSIFICATIONS = {"FACT", "OPINION", "INFERENCE"}
VALID_VALIDITY = {"VERIFIED", "UNVERIFIED", "OUTDATED", "CONFLICTED"}


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_directions(
    defined_ids: list[str], output: dict[str, Any]
) -> list[dict[str, Any]]:
    provided = {
        str(item.get("id")): item
        for item in output.get("directions") or []
        if isinstance(item, dict)
    }
    normalized: list[dict[str, Any]] = []
    for direction_id in defined_ids:
        item = provided.get(direction_id)
        if item is None:
            normalized.append(
                {
                    "id": direction_id,
                    "status": "missing",
                    "findings": [],
                    "source_ids": [],
                    "gaps": ["该方向没有返回任何结果或缺口说明"],
                }
            )
            continue
        findings = [finding for finding in item.get("findings") or [] if isinstance(finding, dict)]
        gaps = [str(gap) for gap in item.get("gaps") or []]
        status = "covered" if findings else "missing"
        if status == "missing" and not gaps:
            gaps = ["该方向没有可用证据"]
        normalized.append(
            {
                "id": direction_id,
                "status": status,
                "findings": findings,
                "source_ids": [str(source) for source in item.get("source_ids") or []],
                "gaps": gaps,
            }
        )
    return normalized


def merge_evidence(
    base: dict[str, Any] | None, incoming: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge every Agent/Tool node's evidence instead of keeping the last one."""
    if not base:
        return dict(incoming or {})
    if not incoming:
        return dict(base)
    merged = dict(base)
    directions: dict[str, dict[str, Any]] = {}
    for direction in list(base.get("directions") or []) + list(incoming.get("directions") or []):
        if not isinstance(direction, dict):
            continue
        direction_id = str(direction.get("id", ""))
        existing = directions.get(direction_id)
        if existing is None:
            directions[direction_id] = {
                **direction,
                "findings": list(direction.get("findings") or []),
                "source_ids": list(direction.get("source_ids") or []),
                "gaps": list(direction.get("gaps") or []),
            }
            continue
        seen = {str(item.get("claim", "")) for item in existing["findings"]}
        existing["findings"].extend(
            item for item in direction.get("findings") or []
            if str(item.get("claim", "")) not in seen
        )
        for source_id in direction.get("source_ids") or []:
            if source_id not in existing["source_ids"]:
                existing["source_ids"].append(source_id)
        for gap in direction.get("gaps") or []:
            if gap not in existing["gaps"]:
                existing["gaps"].append(gap)
        if direction.get("status") == "covered":
            existing["status"] = "covered"
    merged["directions"] = list(directions.values())
    for key in ("qualified", "unknown_candidates", "rejected_candidates"):
        items: dict[str, dict[str, Any]] = {}
        for item in list(base.get(key) or []) + list(incoming.get(key) or []):
            if isinstance(item, dict):
                items[str(item.get("id", ""))] = item
        merged[key] = list(items.values())
    merged["gaps"] = sorted(
        set(base.get("gaps") or [])
        | set(incoming.get("gaps") or [])
        | {
            str(gap)
            for direction in directions.values()
            for gap in direction.get("gaps") or []
        }
    )
    merged["invalid_source_ids"] = sorted(
        set(base.get("invalid_source_ids") or []) | set(incoming.get("invalid_source_ids") or [])
    )
    unverified: dict[str, dict[str, Any]] = {}
    for item in list(base.get("unverified") or []) + list(incoming.get("unverified") or []):
        if isinstance(item, dict):
            unverified[str(item.get("claim", ""))] = item
    merged["unverified"] = list(unverified.values())
    source_rows = dict(base.get("source_rows") or {})
    source_rows.update(incoming.get("source_rows") or {})
    merged["source_rows"] = source_rows
    merged["source_ids"] = sorted(
        set(base.get("source_ids") or []) | set(incoming.get("source_ids") or [])
    )
    return merged


def resolve_source_ids(
    source_ids: list[str],
    *,
    persisted: set[str],
    unavailable: set[str],
) -> tuple[list[str], list[str]]:
    """Split citations into usable ids and invented/unavailable ones."""
    valid: list[str] = []
    invalid: list[str] = []
    for source_id in source_ids:
        if source_id in persisted and source_id not in unavailable:
            valid.append(source_id)
        else:
            invalid.append(source_id)
    return valid, invalid


def _constraint_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(
            item.get("constraint") or item.get("name") or item.get("id") or ""
        ).strip()
    return str(item).strip()


def _constraint_result(item: Any) -> str:
    return str(item.get("result", "UNKNOWN")).upper() if isinstance(item, dict) else "UNKNOWN"


def evaluate_candidates(
    candidates: list[dict[str, Any]],
    required_constraints: list[str] | None = None,
) -> dict[str, Any]:
    """Tri-state constraints aligned to every confirmed hard constraint.

    A candidate is qualified only when every required hard constraint is present
    and SATISFIED; a model that reports a single SATISFIED item, omits a
    constraint, or marks it UNKNOWN stays unqualified.
    """
    required = [str(item).strip() for item in required_constraints or [] if str(item).strip()]
    qualified: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        constraints = list(candidate.get("constraints") or [])
        by_name = {_constraint_name(item): item for item in constraints if _constraint_name(item)}
        entry = {
            "id": str(candidate.get("id", "")),
            "name": str(candidate.get("name", "")),
            "constraints": constraints,
            "source_ids": [str(source) for source in candidate.get("source_ids") or []],
        }
        if required:
            missing = [name for name in required if name not in by_name]
            results = {
                name: _constraint_result(by_name[name])
                for name in required
                if name in by_name
            }
            entry["missing_constraints"] = missing
            if missing:
                unknown.append({
                    **entry,
                    "reason": "缺少硬约束判断依据：" + "、".join(missing),
                })
                continue
            if any(result == "VIOLATED" for result in results.values()):
                rejected.append(entry)
            elif all(result == "SATISFIED" for result in results.values()):
                qualified.append(entry)
            else:
                unknown.append({**entry, "reason": "存在未知硬约束，需人工确认"})
            continue
        if not constraints:
            unknown.append({**entry, "reason": "没有硬约束判断依据"})
        elif all(_constraint_result(item) == "SATISFIED" for item in constraints):
            qualified.append(entry)
        elif any(_constraint_result(item) == "VIOLATED" for item in constraints):
            rejected.append(entry)
        else:
            unknown.append({**entry, "reason": "存在未知硬约束，需人工确认"})
    return {"qualified": qualified, "unknown": unknown, "rejected": rejected}


def order_by_priority(
    candidates: list[dict[str, Any]], confirmed_priority: list[str]
) -> list[dict[str, Any]]:
    """Order only by the confirmed preference list; never invent weights."""
    if not confirmed_priority:
        return list(candidates)

    def key(candidate: dict[str, Any]) -> tuple[int, int]:
        attributes = candidate.get("attributes")
        if not isinstance(attributes, dict):
            return (len(confirmed_priority), 0)
        for index, path in enumerate(confirmed_priority):
            value = attributes.get(path)
            if value is not None:
                # A higher stated priority sorts first; ties keep source order.
                return (index, 0)
        return (len(confirmed_priority), 0)

    return sorted(candidates, key=key)


class EvidenceService:
    def __init__(self, clock: Any) -> None:
        self._clock = clock

    async def record(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        attempt_id: str | None,
        defined_direction_ids: list[str],
        output: dict[str, Any],
        sources: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        confirmed_priority: list[str],
        required_constraints: list[str] | None = None,
    ) -> dict[str, Any]:
        source_rows = await self._persist_sources(session, run_id=run_id, sources=sources)
        persisted_sources = set(source_rows.keys())
        unavailable_sources = {
            str(item.get("source_id") or item.get("url") or "")
            for item in sources
            if isinstance(item, dict)
            and str(item.get("availability", "AVAILABLE")).upper() != "AVAILABLE"
        }
        invalid_source_ids: list[str] = []
        unverified_findings: list[dict[str, Any]] = []
        directions = normalize_directions(defined_direction_ids, output)
        now = self._clock.now()
        for action in actions:
            session.add(
                ResearchActionRow(
                    id=new_id(),
                    run_id=run_id,
                    attempt_id=attempt_id,
                    direction_id=str(action.get("direction_id", "")),
                    purpose=str(action.get("purpose", "research")),
                    tool_name=str(action.get("tool_name", "agent")),
                    arguments_ref=str(action.get("arguments_ref", "args:redacted")),
                    started_at=now,
                    result_ref=action.get("result_ref"),
                    scope_hash=str(action.get("scope_hash", "")),
                )
            )
        for direction in directions:
            for finding in direction["findings"]:
                raw_ids = [
                    str(source)
                    for source in finding.get("source_ids") or direction["source_ids"]
                ]
                # A citation must resolve to a stored, available source. Invented or
                # empty ids leave the claim unverified instead of silently passing.
                source_ids, invalid = resolve_source_ids(
                    raw_ids,
                    persisted=persisted_sources,
                    unavailable=unavailable_sources,
                )
                invalid_source_ids.extend(invalid)
                classification = str(finding.get("classification", "FACT")).upper()
                validity = str(finding.get("validity", "VERIFIED")).upper()
                if classification not in VALID_CLASSIFICATIONS:
                    classification = "INFERENCE"
                if validity not in VALID_VALIDITY:
                    validity = "UNVERIFIED"
                if classification == "FACT" and not source_ids:
                    validity = "UNVERIFIED"
                if validity == "UNVERIFIED":
                    unverified_findings.append(finding)
                session.add(
                    EvidenceRow(
                        id=new_id(),
                        run_id=run_id,
                        direction_id=direction["id"],
                        claim=str(finding.get("claim", "")),
                        source_ids=source_ids,
                        excerpt_refs=[str(item) for item in finding.get("excerpt_refs") or []],
                        classification=classification,
                        confidence_reason=str(finding.get("confidence_reason", "")),
                        conflicts=[str(item) for item in finding.get("conflicts") or []],
                        validity=validity,
                        created_at=now,
                        revision=1,
                    )
                )

        candidates = evaluate_candidates(
            list(output.get("candidates") or []), required_constraints
        )
        qualified = order_by_priority(candidates["qualified"], confirmed_priority)
        return {
            "directions": directions,
            "source_ids": list(source_rows.keys()),
            "source_rows": source_rows,
            "qualified": qualified,
            "unknown_candidates": candidates["unknown"],
            "rejected_candidates": candidates["rejected"],
            "gaps": [gap for direction in directions for gap in direction["gaps"]],
            "invalid_source_ids": sorted(set(invalid_source_ids)),
            "unverified": unverified_findings,
        }

    async def _persist_sources(
        self, session: AsyncSession, *, run_id: str, sources: list[dict[str, Any]]
    ) -> dict[str, str]:
        id_map: dict[str, str] = {}
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_key = str(source.get("source_id") or source.get("url") or "")
            url = str(source.get("normalized_url") or source.get("url") or "")
            if not url:
                continue
            fetched_at = _parse_time(source.get("fetched_at")) or self._clock.now()
            content_hash = str(source.get("content_hash") or "")
            existing = await session.scalar(
                select(Source).where(
                    Source.run_id == run_id,
                    Source.normalized_url == url,
                    Source.content_hash == content_hash,
                    Source.fetched_at == fetched_at,
                )
            )
            if existing is not None:
                id_map[source_key] = existing.id
                continue
            row = Source(
                id=new_id(),
                run_id=run_id,
                normalized_url=redact_url(url),
                title=sanitize(str(source.get("title", ""))),
                fetched_at=fetched_at,
                published_at=_parse_time(source.get("published_at")),
                updated_at=_parse_time(source.get("updated_at")),
                content_hash=content_hash,
                blob_id=source.get("blob_id"),
                availability=str(source.get("availability", "AVAILABLE")),
                deleted_at=None,
            )
            session.add(row)
            id_map[source_key] = row.id
        return id_map



