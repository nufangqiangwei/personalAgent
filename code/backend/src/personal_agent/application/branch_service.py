"""Creating a branch: a new run that reuses only valid results before its start.

A proposal is derived from a parent run and its confirmed input. Changing the
requirement goes through chat on the derived draft; validating recomputes the impact
from the actual difference. Confirming needs a ``BRANCH`` confirmation for that draft,
the final start point and the matching report hash. One proposal can create exactly
one run, the new run shares the root's budget ledger and checks the whole root's side
effects, and reused results are recorded as reused evidence — never as new successful
attempts.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from personal_agent.adapters.persistence.artifact_models import (
    Artifact,
    SideEffectOperation,
)
from personal_agent.adapters.persistence.branch_models import (
    BranchProposal,
    ReusedResult,
)
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    ExecutionSlot,
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    RequirementDraft,
)
from personal_agent.adapters.persistence.workflow_models import Workflow, WorkflowVersion
from personal_agent.application.branch_analysis import BranchAnalyzer
from personal_agent.application.budget_service import BudgetService
from personal_agent.application.requirement_confirmation import RequirementService
from personal_agent.application.run_snapshots import serialize_run
from personal_agent.application.workflow_registry import WorkflowRegistry
from personal_agent.domain.branches import BranchStatus
from personal_agent.domain.common import (
    MODEL_BUSY,
    MODEL_OUTCOME_UNKNOWN,
    RUN_BUSY,
    AppError,
    ErrorCategory,
    new_id,
    stable_hash,
)
from personal_agent.domain.execution import EventKind, RunStatus
from personal_agent.domain.persisted_json import validate_input_snapshot
from personal_agent.ports.contracts import Clock

UNRESOLVED_SIDE_EFFECTS = {"INTENT", "UNKNOWN", "PARTIAL", "CONFLICT"}


class BranchService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        analyzer: BranchAnalyzer,
        registry: WorkflowRegistry,
        budget: BudgetService,
        gateway: Any,
        *,
        config_fingerprint: str,
        runtime_fingerprint: str,
        budget_limits: dict[str, int],
        draft_limits: dict[str, int],
        submit: Any = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._analyzer = analyzer
        self._registry = registry
        self._budget = budget
        self._gateway = gateway
        self._config_fingerprint = config_fingerprint
        self._runtime_fingerprint = runtime_fingerprint
        self._budget_limits = budget_limits
        self._draft_limits = draft_limits
        self._submit = submit

    # ------------------------------------------------------------------
    async def create_proposal(
        self,
        run_id: str,
        *,
        selected_step_id: str,
        reason: str | None = None,
        changed_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        async with self._database.session() as session:
            parent = await session.get(WorkflowRun, run_id)
            if parent is None:
                raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
            source = await session.get(Confirmation, parent.confirmation_id)
            if source is None:
                raise AppError(
                    code="CONFIRMATION_NOT_FOUND",
                    message="the parent confirmation is missing",
                    status=409,
                )
            # The derived draft starts from the exact confirmed input; the user edits
            # it through chat, so nothing is silently reinterpreted.
            field_values = dict(parent.input_snapshot_json.get("fields", {}))
            draft = RequirementDraft(
                id=new_id(),
                selected_version_id=parent.workflow_version_id,
                revision=1,
                status="READY",
                missing_fields_json=[],
                conflicts_json=[],
                field_values_json=field_values,
                preference_order_json=list(
                    parent.input_snapshot_json.get("preference_order", [])
                ),
                output_spec_json=dict(
                    parent.input_snapshot_json.get("output_spec", {})
                ),
                summary_markdown="",
                summary_hash="",
                source_draft_id=source.draft_id,
                created_at=self._clock.now(),
                updated_at=self._clock.now(),
            )
            session.add(draft)
            await session.flush()
            await self._budget.ensure_draft_ledger(
                session, draft_id=draft.id, limits=self._draft_limits
            )
            requirements = RequirementService(
                self._database, self._clock, self._budget,
                policy_hash=source.budget_policy_hash,
                policy_version=source.data_policy_version,
                draft_limits=self._draft_limits,
                run_limits=self._budget_limits,
            )
            await requirements.recompute(session, draft)
            proposal = BranchProposal(
                id=new_id(),
                parent_run_id=parent.id,
                root_run_id=parent.root_run_id,
                fork_step_id=selected_step_id,
                status=BranchStatus.DRAFT.value,
                draft_id=draft.id,
                source_draft_id=source.draft_id,
                changed_paths=list(changed_paths or []),
                input_reads_closure=[],
                affected_step_ids=[],
                reusable_step_ids=[],
                invalidated_artifact_ids=[],
                blocked_reasons=[],
                report_hash="",
                input_hash=None,
                confirmation_id=None,
                created_run_id=None,
                revision=1,
                created_at=self._clock.now(),
                updated_at=self._clock.now(),
            )
            session.add(proposal)
            await session.commit()
            payload = await self._serialize(session, proposal, reason=reason)
        return payload

    async def get(self, proposal_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            proposal = await self._require(session, proposal_id)
            return await self._serialize(session, proposal)

    async def validate(
        self, proposal_id: str, *, selected_step_id: str | None = None
    ) -> dict[str, Any]:
        async with self._database.session() as session:
            proposal = await self._require(session, proposal_id)
            if proposal.created_run_id is not None:
                raise AppError(
                    code="BRANCH_ALREADY_CONFIRMED",
                    message="a confirmed branch cannot be re-validated",
                    status=409,
                )
            start = selected_step_id or proposal.fork_step_id
            draft = await session.get(RequirementDraft, proposal.draft_id or "")
            if draft is None:
                raise AppError(
                    code="DRAFT_NOT_FOUND", message="derived draft is missing", status=409
                )
            parent = await session.get(WorkflowRun, proposal.parent_run_id)
            if parent is None:
                raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
            original = validate_input_snapshot(dict(parent.input_snapshot_json))
            changed = BranchAnalyzer.changed_paths(original, {
                "fields": draft.field_values_json,
                "preference_order": draft.preference_order_json,
                "output_spec": draft.output_spec_json,
            })
            analysis = await self._analyzer.analyze(
                run_id=parent.id, selected_step_id=start, changed_paths=changed
            )
            proposal.fork_step_id = start
            proposal.changed_paths = list(analysis["changed_paths"])
            proposal.input_reads_closure = list(analysis["input_reads_closure"])
            proposal.affected_step_ids = list(analysis["affected_step_ids"])
            proposal.reusable_step_ids = list(analysis["reusable_step_ids"])
            proposal.invalidated_artifact_ids = list(
                analysis["invalidated_artifact_ids"]
            )
            proposal.blocked_reasons = list(analysis["blocked_reasons"])
            proposal.status = analysis["status"]
            proposal.report_hash = str(analysis["report_hash"])
            proposal.revision += 1
            proposal.updated_at = self._clock.now()
            proposal.input_hash = parent.input_hash
            await session.commit()
            return {
                "proposal": await self._serialize(session, proposal),
                "analysis": analysis,
            }

    async def confirm(
        self,
        proposal_id: str,
        *,
        confirmation_id: str,
        effective_step_id: str,
        report_hash: str,
        explicit_start_confirmation: bool,
    ) -> dict[str, Any]:
        if not explicit_start_confirmation:
            raise AppError(
                code="START_POINT_REQUIRES_CONFIRMATION",
                message="creating a branch needs an explicit confirmation",
                category=ErrorCategory.VALIDATION,
                status=422,
            )
        async with self._database.session() as session:
            proposal = await self._require(session, proposal_id)
            if proposal.created_run_id is not None:
                existing = await session.get(WorkflowRun, proposal.created_run_id)
                if existing is not None:
                    return await serialize_run(session, existing)
                raise AppError(
                    code="BRANCH_ALREADY_CONFIRMED",
                    message="this proposal already created a run",
                    status=409,
                )
            confirmation = await session.get(Confirmation, confirmation_id)
            if confirmation is None:
                raise AppError(
                    code="CONFIRMATION_NOT_FOUND",
                    message="confirmation not found",
                    status=404,
                )
            if confirmation.action != "BRANCH":
                raise AppError(
                    code="CONFIRMATION_INVALID",
                    message="creating a branch needs a BRANCH confirmation",
                    status=409,
                )
            if confirmation.draft_id != proposal.draft_id:
                raise AppError(
                    code="CONFIRMATION_STALE",
                    message="the confirmation does not belong to the derived draft",
                    status=409,
                )
            draft = await session.get(RequirementDraft, confirmation.draft_id)
            if draft is None or draft.status != "CONFIRMED":
                raise AppError(
                    code="CONFIRMATION_STALE",
                    message="the derived draft is not confirmed",
                    status=409,
                )
            if effective_step_id != proposal.fork_step_id:
                # A different start point needs its own verification before use.
                raise AppError(
                    code="START_POINT_REQUIRES_CONFIRMATION",
                    message="the start point changed; validate it before confirming",
                    category=ErrorCategory.VALIDATION,
                    status=409,
                    details={
                        "proposal_start_step_id": proposal.fork_step_id,
                        "requested_step_id": effective_step_id,
                    },
                )
            if proposal.status != BranchStatus.READY.value:
                raise AppError(
                    code="PREREQUISITE_INVALID",
                    message="the branch is blocked; choose an earlier start point",
                    status=409,
                    details={"blocked_reasons": list(proposal.blocked_reasons)},
                )
            parent = await session.get(WorkflowRun, proposal.parent_run_id)
            if parent is None:
                raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
            confirmed_snapshot = {
                "fields": dict(draft.field_values_json),
                "preference_order": list(draft.preference_order_json),
                "output_spec": dict(draft.output_spec_json),
            }
            if (
                confirmation.input_hash != stable_hash(confirmed_snapshot)
                or confirmation.workflow_version_id != parent.workflow_version_id
            ):
                # The BRANCH confirmation is bound to one fixed version and one
                # exact field snapshot; anything else is stale, not reusable.
                raise AppError(
                    code="CONFIRMATION_STALE",
                    message=(
                        "the branch confirmation no longer matches its fixed "
                        "version and fields"
                    ),
                    status=409,
                )
            original = validate_input_snapshot(dict(parent.input_snapshot_json))
            changed = BranchAnalyzer.changed_paths(original, {
                "fields": draft.field_values_json,
                "preference_order": draft.preference_order_json,
                "output_spec": draft.output_spec_json,
            })
            analysis = await self._analyzer.analyze(
                run_id=parent.id,
                selected_step_id=effective_step_id,
                changed_paths=changed,
            )
            if analysis["status"] != BranchStatus.READY.value:
                raise AppError(
                    code="PREREQUISITE_INVALID",
                    message="the branch became blocked; validate it again",
                    status=409,
                    details={"blocked_reasons": analysis["blocked_reasons"]},
                )
            if report_hash != analysis["report_hash"]:
                raise AppError(
                    code="REPORT_HASH_MISMATCH",
                    message="the verification report changed; confirm the new report",
                    status=409,
                    details={"expected": analysis["report_hash"]},
                )

            version = await session.get(WorkflowVersion, parent.workflow_version_id)
            if version is None or not version.executable:
                raise AppError(
                    code="VERSION_CHANGED",
                    message="the confirmed workflow version is no longer executable",
                    status=409,
                )
            workflow = await session.get(Workflow, version.workflow_id)
            if workflow is None or workflow.disabled_at is not None:
                raise AppError(
                    code="WORKFLOW_DISABLED", message="the workflow is disabled", status=409
                )

            unresolved = [
                operation
                for operation in (
                    await session.scalars(
                        select(SideEffectOperation).where(
                            SideEffectOperation.root_run_id == parent.root_run_id
                        )
                    )
                ).all()
                if operation.status in UNRESOLVED_SIDE_EFFECTS
            ]
            if unresolved:
                raise AppError(
                    code="SIDE_EFFECT_UNRESOLVED",
                    message="the root has a write that still needs verification",
                    status=409,
                    details={"operation_ids": [item.id for item in unresolved]},
                )
            pending = await self._gateway.non_terminal_request()
            if pending is not None:
                if pending.status == "UNKNOWN":
                    raise AppError(
                        code=MODEL_OUTCOME_UNKNOWN,
                        message="an unresolved model request blocks a new branch",
                        status=409,
                        details={"request_id": pending.id},
                    )
                raise AppError(
                    code=MODEL_BUSY,
                    message="a model request is still in flight",
                    status=409,
                    details={"request_id": pending.id},
                )

            run_id = new_id()
            now = self._clock.now()
            new_run = WorkflowRun(
                id=run_id,
                root_run_id=parent.root_run_id,
                parent_run_id=parent.id,
                fork_step_id=effective_step_id,
                workflow_version_id=version.id,
                confirmation_id=confirmation.id,
                input_snapshot_json=validate_input_snapshot(
                    dict(confirmation.field_snapshot_json)
                ),
                input_hash=confirmation.input_hash,
                config_fingerprint=self._config_fingerprint,
                runtime_fingerprint=self._runtime_fingerprint,
                budget_policy_hash=confirmation.budget_policy_hash,
                data_policy_version=confirmation.data_policy_version,
                status=RunStatus.RUNNING.value,
                reason_code=None,
                current_step_id=None,
                epoch=1,
                revision=1,
                mode="FIXED",
                completeness=None,
                active_elapsed_ms=0,
                cancel_requested=False,
                created_at=now,
                updated_at=now,
            )
            session.add(new_run)
            # The slot references the run; claim it only after the child row exists.
            await session.flush()
            acquired = await session.execute(
                update(ExecutionSlot)
                .where(ExecutionSlot.singleton_id == 1, ExecutionSlot.run_id.is_(None))
                .values(
                    run_id=run_id,
                    epoch=ExecutionSlot.epoch + 1,
                    draining=False,
                    process_instance_id="local",
                    revision=ExecutionSlot.revision + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if acquired.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                raise AppError(
                    code=RUN_BUSY,
                    message="another execution already holds the single slot",
                    status=409,
                )
            # The root owns one ledger; a branch never gets its own budget.
            await self._budget.ensure_run_ledger(
                session, root_run_id=parent.root_run_id, limits=self._budget_limits
            )

            reusable = set(analysis["reusable_step_ids"])
            reused_artifacts = await self._copy_reused_results(
                session,
                parent=parent,
                new_run_id=run_id,
                reusable=reusable,
                invalid=set(analysis["invalidated_artifact_ids"]),
                now=now,
            )
            session.add(
                ExecutionEvent(
                    id=new_id(),
                    run_id=run_id,
                    draft_id=None,
                    sequence=1,
                    step_run_id=None,
                    attempt_id=None,
                    workflow_version_id=version.id,
                    occurred_at=now,
                    kind=EventKind.BRANCH_CREATED.value,
                    status=new_run.status,
                    reason_code=None,
                    payload_json={
                        "parent_run_id": parent.id,
                        "fork_step_id": effective_step_id,
                        "report_hash": analysis["report_hash"],
                        "reused_step_ids": sorted(reusable),
                        "reused_artifact_ids": reused_artifacts,
                        "confirmation_id": confirmation.id,
                    },
                    payload_ref=None,
                    schema_version=1,
                    provider_event_key=None,
                )
            )
            proposal.status = BranchStatus.CONFIRMED.value
            proposal.confirmation_id = confirmation.id
            proposal.created_run_id = run_id
            proposal.revision += 1
            proposal.updated_at = now
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise AppError(
                    code="BRANCH_ALREADY_CONFIRMED",
                    message=f"this proposal already created a run ({error.orig})",
                    status=409,
                ) from error
            snapshot = await serialize_run(session, new_run)
        if self._submit is not None:
            self._submit(run_id, False)
        return snapshot

    # ------------------------------------------------------------------
    async def _copy_reused_results(
        self,
        session: Any,
        *,
        parent: WorkflowRun,
        new_run_id: str,
        reusable: set[str],
        invalid: set[str],
        now: Any,
    ) -> list[str]:
        """Copy prior step facts and record artifact reuse with a traceable source."""
        prior_steps = (
            await session.scalars(select(StepRun).where(StepRun.run_id == parent.id))
        ).all()
        for step in prior_steps:
            if step.step_id not in reusable:
                continue
            if step.status != "SUCCEEDED":
                continue
            session.add(
                StepRun(
                    id=new_id(),
                    run_id=new_run_id,
                    step_id=step.step_id,
                    status="SUCCEEDED",
                    current_attempt_no=0,
                    selected_transition=step.selected_transition,
                    skip_reason="reused from parent",
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        if not reusable:
            return []
        step_of_attempt: dict[str, str] = {}
        attempts = (
            await session.execute(
                select(StepAttempt.id, StepRun.step_id)
                .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                .where(StepRun.run_id == parent.id)
            )
        ).all()
        step_of_attempt = {attempt_id: step_id for attempt_id, step_id in attempts}

        reused_ids: list[str] = []
        artifacts = (
            await session.scalars(select(Artifact).where(Artifact.run_id == parent.id))
        ).all()
        for artifact in artifacts:
            if artifact.id in invalid:
                continue
            if artifact.access_state != "AVAILABLE":
                continue
            step_id = (
                step_of_attempt.get(artifact.attempt_id)
                if artifact.attempt_id is not None
                else None
            )
            if step_id is None or step_id not in reusable:
                continue
            # Only results produced before the start point may be reused.
            session.add(
                ReusedResult(
                    id=new_id(),
                    run_id=new_run_id,
                    source_run_id=parent.id,
                    step_id=step_id,
                    artifact_id=artifact.id,
                    manifest_hash=artifact.manifest_hash,
                    created_at=now,
                )
            )
            reused_ids.append(artifact.id)
        return reused_ids

    async def _serialize(self, session: Any, proposal: BranchProposal, **_: Any) -> dict[str, Any]:
        return {
            "id": proposal.id,
            "parent_run_id": proposal.parent_run_id,
            "root_run_id": proposal.root_run_id,
            "selected_step_id": proposal.fork_step_id,
            "effective_step_id": proposal.fork_step_id,
            "draft_id": proposal.draft_id,
            "source_draft_id": proposal.source_draft_id,
            "status": proposal.status,
            "revision": proposal.revision,
            "changed_paths": list(proposal.changed_paths),
            "input_reads_closure": list(proposal.input_reads_closure),
            "affected_step_ids": list(proposal.affected_step_ids),
            "reusable_step_ids": list(proposal.reusable_step_ids),
            "invalidated_artifact_ids": list(proposal.invalidated_artifact_ids),
            "blocked_reasons": list(proposal.blocked_reasons),
            "report_hash": proposal.report_hash,
            "input_hash": proposal.input_hash,
            "confirmation_id": proposal.confirmation_id,
            "created_run_id": proposal.created_run_id,
            "created_at": proposal.created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": proposal.updated_at.isoformat().replace("+00:00", "Z"),
        }

    @staticmethod
    async def _require(session: Any, proposal_id: str) -> BranchProposal:
        proposal = await session.get(BranchProposal, proposal_id)
        if proposal is None:
            raise AppError(
                code="BRANCH_PROPOSAL_NOT_FOUND",
                message="branch proposal not found",
                status=404,
            )
        return proposal


__all__ = ["BranchService"]
