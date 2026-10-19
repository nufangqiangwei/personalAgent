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


def evaluate_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    qualified: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        constraints = [
            item for item in candidate.get("constraints") or [] if isinstance(item, dict)
        ]
        results = {str(item.get("result")) for item in constraints}
        entry = {
            "id": str(candidate.get("id", "")),
            "name": str(candidate.get("name", "")),
            "constraints": constraints,
            "source_ids": [str(source) for source in candidate.get("source_ids") or []],
        }
        if not constraints:
            unknown.append({**entry, "reason": "没有硬约束判断依据"})
        elif results == {"SATISFIED"}:
            qualified.append(entry)
        elif "VIOLATED" in results:
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
    ) -> dict[str, Any]:
        source_rows = await self._persist_sources(session, run_id=run_id, sources=sources)
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
                source_ids = [
                    str(source)
                    for source in finding.get("source_ids") or direction["source_ids"]
                ]
                classification = str(finding.get("classification", "FACT")).upper()
                validity = str(finding.get("validity", "VERIFIED")).upper()
                if classification not in VALID_CLASSIFICATIONS:
                    classification = "INFERENCE"
                if validity not in VALID_VALIDITY:
                    validity = "UNVERIFIED"
                if classification == "FACT" and not source_ids:
                    validity = "UNVERIFIED"
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

        candidates = evaluate_candidates(list(output.get("candidates") or []))
        qualified = order_by_priority(candidates["qualified"], confirmed_priority)
        return {
            "directions": directions,
            "source_ids": list(source_rows.keys()),
            "source_rows": source_rows,
            "qualified": qualified,
            "unknown_candidates": candidates["unknown"],
            "rejected_candidates": candidates["rejected"],
            "gaps": [gap for direction in directions for gap in direction["gaps"]],
            "unverified": [
                finding
                for direction in directions
                for finding in direction["findings"]
                if str(finding.get("validity", "")).upper() == "UNVERIFIED"
                or (
                    str(finding.get("classification", "FACT")).upper() == "FACT"
                    and not finding.get("source_ids")
                )
            ],
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
                normalized_url=url,
                title=str(source.get("title", "")),
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



