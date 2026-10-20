"""Draft lifecycle, summary rendering and immutable execution confirmation.

Editing through chat is the only way to change requirements. Every edit bumps the
draft revision and invalidates an earlier confirmation; accepting a suggestion
never starts a run, and confirmation binds the exact revision, summary hash,
workflow version, policy hash and data policy version.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.requirement_models import (
    Confirmation,
    ConversationMessage,
    RequirementDraft,
    Suggestion,
)
from personal_agent.adapters.persistence.uow import cas_update
from personal_agent.adapters.persistence.workflow_models import Workflow, WorkflowVersion
from personal_agent.adapters.rendering.requirement_summary import render_summary
from personal_agent.application.budget_service import BudgetService
from personal_agent.domain.budget import effective_run_limits
from personal_agent.domain.common import (
    CONFIRMATION_STALE,
    INPUT_INCOMPLETE,
    POLICY_CHANGED,
    UNACCEPTED_SUGGESTION,
    VERSION_CHANGED,
    AppError,
    new_id,
    stable_hash,
)
from personal_agent.domain.persisted_json import validate_input_snapshot
from personal_agent.domain.requirements import (
    ConfirmationAction,
    DraftStatus,
    FieldSource,
    FieldValue,
    assert_confirmable,
    assert_editable,
    assert_grantable,
    derive_status,
    invalidate_confirmation,
    missing_fields,
    required_paths,
    validate_field_values,
)
from personal_agent.domain.workflow_schema import WorkflowDefinition
from personal_agent.ports.contracts import Clock


class RequirementService:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        budget: BudgetService,
        *,
        policy_hash: str,
        policy_version: str,
        draft_limits: dict[str, int],
        run_limits: dict[str, int] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock
        self._budget = budget
        self._policy_hash = policy_hash
        self._policy_version = policy_version
        self._draft_limits = draft_limits
        self._run_limits = dict(run_limits or {})

    # ------------------------------------------------------------------
    # Creation and queries
    # ------------------------------------------------------------------
    async def create_draft(self, workflow_version_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            version = await session.get(WorkflowVersion, workflow_version_id)
            if version is None:
                raise AppError(
                    code="VERSION_NOT_FOUND", message="workflow version not found", status=404
                )
            if not version.executable:
                raise AppError(
                    code="VERSION_CHANGED",
                    message="only an executable workflow version can start a draft",
                    status=409,
                )
            workflow = await session.get(Workflow, version.workflow_id)
            if workflow is None or workflow.disabled_at is not None:
                raise AppError(
                    code="WORKFLOW_DISABLED",
                    message="this workflow is disabled",
                    status=409,
                )
            definition = WorkflowDefinition.model_validate(version.definition_json)
            draft = RequirementDraft(
                id=new_id(),
                selected_version_id=version.id,
                revision=1,
                status=DraftStatus.COLLECTING.value,
                missing_fields_json=required_paths(definition.input_schema),
                conflicts_json=[],
                field_values_json={},
                preference_order_json=[],
                output_spec_json={},
                summary_markdown="",
                summary_hash="",
                source_draft_id=None,
                created_at=self._clock.now(),
                updated_at=self._clock.now(),
            )
            session.add(draft)
            # The ledger references the draft; sessions are autoflush=False.
            await session.flush()
            await self._budget.ensure_draft_ledger(
                session, draft_id=draft.id, limits=self._draft_limits
            )
            await self._recompute(session, draft, definition)
            await session.commit()
            return await self.snapshot(session, draft)

    async def get(self, draft_id: str) -> dict[str, Any]:
        async with self._database.session() as session:
            draft = await self._require(session, draft_id)
            return await self.snapshot(session, draft)

    async def snapshot(self, session: AsyncSession, draft: RequirementDraft) -> dict[str, Any]:
        version = await session.get(WorkflowVersion, draft.selected_version_id)
        definition = (
            WorkflowDefinition.model_validate(version.definition_json)
            if version is not None
            else None
        )
        suggestions = list(
            (
                await session.scalars(
                    select(Suggestion)
                    .where(Suggestion.draft_id == draft.id)
                    .order_by(Suggestion.created_at, Suggestion.id)
                )
            ).all()
        )
        messages = list(
            (
                await session.scalars(
                    select(ConversationMessage)
                    .where(ConversationMessage.draft_id == draft.id)
                    .order_by(ConversationMessage.sequence)
                )
            ).all()
        )
        ledger = await session.scalar(
            select(BudgetLedger).where(BudgetLedger.draft_id == draft.id)
        )
        budget_summary = None
        if ledger is not None:
            ledger_snapshot = await self._budget.snapshot(session, ledger)
            budget_summary = {
                "limits": ledger_snapshot["limits"],
                "consumed": ledger_snapshot["consumed"],
                "reserved": ledger_snapshot["reserved"],
                "remaining": ledger_snapshot["remaining"],
                "exhausted": ledger_snapshot["exhausted"],
            }
        confirmation = await session.scalar(
            select(Confirmation)
            .where(Confirmation.draft_id == draft.id)
            .order_by(Confirmation.confirmed_at.desc())
            .limit(1)
        )
        fields = [
            {
                **value,
                "accepted": True,
            }
            for value in draft.field_values_json.values()
        ]
        return {
            "id": draft.id,
            "revision": draft.revision,
            "workflow_version_id": draft.selected_version_id,
            "workflow_name": definition.name if definition else "",
            "workflow_version_no": version.version_no if version else 0,
            "status": draft.status,
            "fields": fields,
            "missing_fields": list(draft.missing_fields_json),
            "conflicts": list(draft.conflicts_json),
            "preference_order": list(draft.preference_order_json),
            "output_spec": dict(draft.output_spec_json),
            "summary_markdown": draft.summary_markdown,
            "summary_hash": draft.summary_hash,
            "suggestions": [
                {
                    "id": item.id,
                    "field_path": item.field_path,
                    "value": item.value_json,
                    "rationale": item.rationale,
                    "status": item.status,
                }
                for item in suggestions
            ],
            "messages": [
                {
                    "id": message.id,
                    "sequence": message.sequence,
                    "role": message.role,
                    "content": message.content_text,
                    "created_at": message.created_at.isoformat().replace("+00:00", "Z"),
                }
                for message in messages
            ],
            "confirmation_id": confirmation.id if confirmation else None,
            "budget_summary": budget_summary,
            "source_draft_id": draft.source_draft_id,
            "updated_at": draft.updated_at.isoformat().replace("+00:00", "Z"),
        }

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def append_message(
        self, draft_id: str, text: str, *, role: str = "user"
    ) -> tuple[RequirementDraft, ConversationMessage]:
        if not text.strip():
            raise AppError(
                code="MESSAGE_EMPTY", message="the message must not be empty", status=422
            )
        async with self._database.session() as session:
            draft = await self._require(session, draft_id)
            assert_editable(DraftStatus(draft.status))
            sequence = (
                await session.scalar(
                    select(func.max(ConversationMessage.sequence)).where(
                        ConversationMessage.draft_id == draft_id
                    )
                )
                or 0
            ) + 1
            message = ConversationMessage(
                id=new_id(),
                draft_id=draft_id,
                sequence=sequence,
                role=role,
                content_ref=f"message:{sequence}",
                content_text=text.strip(),
                created_at=self._clock.now(),
            )
            session.add(message)
            draft.status = invalidate_confirmation(DraftStatus(draft.status)).value
            if draft.status == DraftStatus.STALE.value:
                # A chat edit after confirmation makes that confirmation unusable.
                draft.status = DraftStatus.COLLECTING.value
            draft.revision += 1
            draft.updated_at = self._clock.now()
            await session.commit()
            return draft, message

    async def accept_suggestion(
        self, draft_id: str, suggestion_id: str, *, draft_revision: int
    ) -> dict[str, Any]:
        async with self._database.session() as session:
            draft = await self._require(session, draft_id)
            assert_editable(DraftStatus(draft.status))
            self._assert_revision(draft, draft_revision)
            suggestion = await session.get(Suggestion, suggestion_id)
            if suggestion is None or suggestion.draft_id != draft_id:
                raise AppError(
                    code="SUGGESTION_NOT_FOUND", message="suggestion not found", status=404
                )
            if suggestion.status != "PENDING":
                raise AppError(
                    code="SUGGESTION_ALREADY_DECIDED",
                    message="the suggestion was already accepted or rejected",
                    status=409,
                )
            fields = dict(draft.field_values_json)
            fields[suggestion.field_path] = FieldValue(
                path=suggestion.field_path,
                value=suggestion.value_json,
                source=FieldSource.SUGGESTION,
                message_id=None,
                suggestion_id=suggestion.id,
                accepted_at=self._clock.now().isoformat().replace("+00:00", "Z"),
            ).as_dict()
            draft.field_values_json = fields
            suggestion.status = "ACCEPTED"
            suggestion.accepted_message_id = None
            suggestion.revision += 1
            draft.revision += 1
            draft.updated_at = self._clock.now()
            version = await session.get(WorkflowVersion, draft.selected_version_id)
            if version is not None:
                await self._recompute(
                    session, draft, WorkflowDefinition.model_validate(version.definition_json)
                )
            await session.commit()
            return await self.snapshot(session, draft)

    async def change_selection(
        self, draft_id: str, workflow_version_id: str, *, draft_revision: int
    ) -> dict[str, Any]:
        async with self._database.session() as session:
            draft = await self._require(session, draft_id)
            assert_editable(DraftStatus(draft.status))
            self._assert_revision(draft, draft_revision)
            version = await session.get(WorkflowVersion, workflow_version_id)
            if version is None or not version.executable:
                raise AppError(
                    code="VERSION_CHANGED",
                    message="the selected version is not executable",
                    status=409,
                )
            draft.selected_version_id = version.id
            draft.status = DraftStatus.STALE.value
            draft.revision += 1
            draft.updated_at = self._clock.now()
            await self._recompute(
                session, draft, WorkflowDefinition.model_validate(version.definition_json)
            )
            if not draft.missing_fields_json and not draft.conflicts_json:
                draft.status = DraftStatus.STALE.value
            await session.commit()
            return await self.snapshot(session, draft)

    async def confirm(
        self,
        draft_id: str,
        *,
        draft_revision: int,
        summary_hash: str,
        workflow_version_id: str,
        budget_policy_hash: str,
        data_policy_version: str,
        action: str,
        explicit_start_confirmation: bool,
    ) -> dict[str, Any]:
        if not explicit_start_confirmation:
            raise AppError(
                code="EXPLICIT_CONFIRMATION_REQUIRED",
                message="starting research requires an explicit user confirmation",
                status=422,
            )
        async with self._database.session() as session:
            draft = await self._require(session, draft_id)
            self._assert_revision(draft, draft_revision)
            status = DraftStatus(draft.status)
            if status is DraftStatus.CONFIRMED:
                raise AppError(
                    code="DRAFT_STATE_INVALID",
                    message="this draft is already confirmed",
                    status=409,
                )
            version = await session.get(WorkflowVersion, workflow_version_id)
            if version is None or not version.executable:
                raise AppError(
                    code=VERSION_CHANGED,
                    message="the confirmed workflow version is no longer executable",
                    status=409,
                )
            if version.id != draft.selected_version_id:
                raise AppError(
                    code=VERSION_CHANGED,
                    message="the workflow version changed since the summary was rendered",
                    status=409,
                )
            workflow = await session.get(Workflow, version.workflow_id)
            latest = workflow.latest_executable_version_id if workflow else None
            if latest is not None and latest != version.id:
                raise AppError(
                    code=VERSION_CHANGED,
                    message="a newer executable version exists; reselect and reconfirm",
                    status=409,
                    details={"latest_version_id": latest},
                )
            if budget_policy_hash != self._policy_hash:
                raise AppError(
                    code=POLICY_CHANGED,
                    message="the budget policy changed; reload before confirming",
                    status=409,
                )
            if data_policy_version != self._policy_version:
                raise AppError(
                    code=POLICY_CHANGED,
                    message="the data policy changed; reload before confirming",
                    status=409,
                )
            definition = WorkflowDefinition.model_validate(version.definition_json)
            await self._recompute(session, draft, definition)
            if summary_hash != draft.summary_hash:
                raise AppError(
                    code=CONFIRMATION_STALE,
                    message="the summary changed; review it again before confirming",
                    status=409,
                )
            missing = list(draft.missing_fields_json)
            if missing:
                raise AppError(
                    code=INPUT_INCOMPLETE,
                    message="required input is still missing",
                    status=409,
                    details={"missing_fields": missing},
                )
            assert_confirmable(
                status=status, missing=missing, conflicts=list(draft.conflicts_json)
            )
            pending = await session.scalar(
                select(func.count())
                .select_from(Suggestion)
                .where(Suggestion.draft_id == draft_id, Suggestion.status == "PENDING")
            )
            if pending:
                raise AppError(
                    code=UNACCEPTED_SUGGESTION,
                    message="pending suggestions must be accepted or rejected first",
                    status=409,
                    details={"pending": int(pending)},
                )
            if action == ConfirmationAction.BRANCH.value and not draft.source_draft_id:
                raise AppError(
                    code="BRANCH_CONFIRMATION_INVALID",
                    message="BRANCH confirmation must come from a branch proposal draft",
                    status=409,
                )
            input_snapshot = validate_input_snapshot({
                "schema_version": 1,
                "fields": dict(draft.field_values_json),
                "preference_order": list(draft.preference_order_json),
                "output_spec": dict(draft.output_spec_json),
            })
            self._validate_output_spec(dict(draft.output_spec_json))
            confirmation = Confirmation(
                id=new_id(),
                draft_id=draft.id,
                draft_revision=draft.revision,
                workflow_version_id=version.id,
                summary_hash=draft.summary_hash,
                input_hash=stable_hash(input_snapshot),
                budget_policy_hash=budget_policy_hash,
                data_policy_version=data_policy_version,
                confirmed_at=self._clock.now(),
                action=action,
                field_snapshot_json=input_snapshot,
            )
            session.add(confirmation)
            # Conditional update: only the transaction that still sees this exact
            # revision may consume the draft. The unique index is the backstop.
            await cas_update(
                session, RequirementDraft, identity={"id": draft.id},
                expected_revision=draft.revision,
                values={
                    "status": DraftStatus.CONFIRMED.value,
                    "updated_at": self._clock.now(),
                },
                stale_code=CONFIRMATION_STALE, stale_status=409,
            )
            draft.status = DraftStatus.CONFIRMED.value
            draft.revision += 1
            draft.updated_at = self._clock.now()
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise AppError(
                    code="CONFIRMATION_CONSUMED",
                    message="this draft revision was already confirmed",
                    status=409,
                ) from None
            return {
                "confirmation_id": confirmation.id,
                "confirmed_input_hash": confirmation.input_hash,
                "draft_revision": draft.revision,
                "workflow_version_id": version.id,
                "action": action,
            }

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------
    async def budget_snapshot(self, draft_id: str) -> dict[str, Any]:
        from personal_agent.adapters.persistence.budget_models import BudgetLedger

        async with self._database.session() as session:
            draft = await self._require(session, draft_id)
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.draft_id == draft.id)
            )
            if ledger is None:
                ledger = await self._budget.ensure_draft_ledger(
                    session, draft_id=draft.id, limits=self._draft_limits
                )
                await session.commit()
            snapshot = await self._budget.snapshot(session, ledger)
            exhausted = list(snapshot["exhausted_dimensions"])
            assistance = None
            if exhausted:
                assistance = {
                    "kind": "BUDGET_LIMIT",
                    "status": "OPEN",
                    "observed_problem": "草稿模型请求或 token 已达上限；尚未创建研究run。",
                    "unknown_model_request": False,
                    "available_actions": [
                        {"action": "GRANT_DRAFT_BUDGET", "enabled": True, "disabled_reason": None}
                    ],
                }
            return {
                "draft_id": draft.id,
                "ledger_id": ledger.id,
                "ledger_scope": "DRAFT",
                "revision": snapshot["revision"],
                "limits": snapshot["limits"],
                "consumed": snapshot["consumed"],
                "reserved": snapshot["reserved"],
                "remaining": snapshot["remaining"],
                "active_ms": snapshot["active_ms"],
                "exhausted": snapshot["exhausted"],
                "exhausted_dimensions": exhausted,
                "assistance": assistance,
                "updated_at": snapshot["updated_at"],
            }

    async def grant_budget(
        self,
        draft_id: str,
        *,
        draft_revision: int,
        increment: dict[str, int],
        operation_id: str,
        note: str | None,
        explicit_grant: bool,
        acknowledge_cost_and_ledger: bool,
    ) -> dict[str, Any]:
        from personal_agent.adapters.persistence.budget_models import BudgetLedger

        if not explicit_grant or not acknowledge_cost_and_ledger:
            raise AppError(
                code="DRAFT_BUDGET_GRANT_INVALID",
                message="a top-up requires explicit_grant and acknowledge_cost_and_ledger",
                status=422,
            )
        async with self._database.session() as session:
            draft = await self._require(session, draft_id)
            self._assert_revision(draft, draft_revision)
            assert_grantable(DraftStatus(draft.status))
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.draft_id == draft.id)
            )
            if ledger is None:
                ledger = await self._budget.ensure_draft_ledger(
                    session, draft_id=draft.id, limits=self._draft_limits
                )
                await session.flush()
            entry, _snapshot = await self._budget.grant_draft_budget(
                session,
                draft_id=draft.id,
                draft_status=draft.status,
                draft_revision=draft.revision,
                ledger=ledger,
                increment=increment,
                operation_id=operation_id,
                default_limits=self._draft_limits,
                note=note,
            )
            draft.revision += 1
            draft.updated_at = self._clock.now()
            await session.commit()
            budget = await self.budget_snapshot(draft_id)
            return {
                "grant_id": entry.id,
                "ledger_id": ledger.id,
                "ledger_scope": "DRAFT",
                "draft_id": draft.id,
                "draft_revision": draft.revision,
                "granted_increment": increment,
                "budget": budget,
                "research_started": False,
                "confirmation_id": None,
                "run_id": None,
                "allowed_actions": [
                    {"action": "RETRY_ANALYSIS", "enabled": True, "disabled_reason": None}
                ],
            }

    # ------------------------------------------------------------------
    # Public helpers used by the analysis service
    # ------------------------------------------------------------------
    async def load(self, session: AsyncSession, draft_id: str) -> RequirementDraft:
        return await self._require(session, draft_id)

    async def recompute(
        self,
        session: AsyncSession,
        draft: RequirementDraft,
        definition: WorkflowDefinition | None = None,
    ) -> None:
        if definition is None:
            version = await session.get(WorkflowVersion, draft.selected_version_id)
            if version is None:
                return
            definition = WorkflowDefinition.model_validate(version.definition_json)
        await self._recompute(session, draft, definition)

    async def reserve_analysis_budget(
        self,
        session: AsyncSession,
        *,
        ledger: BudgetLedger,
        worst_case: dict[str, int],
        operation_id: str,
    ) -> None:
        await self._budget.reserve(
            session, ledger=ledger, worst_case=worst_case, operation_id=operation_id
        )

    async def settle_analysis_budget(
        self,
        session: AsyncSession,
        *,
        ledger: BudgetLedger,
        reserved: dict[str, int],
        actual: dict[str, int],
        operation_id: str,
    ) -> None:
        await self._budget.settle(
            session,
            ledger=ledger,
            reserved=reserved,
            actual=actual,
            operation_id=operation_id,
        )

    async def release_analysis_budget(
        self,
        session: AsyncSession,
        *,
        ledger: BudgetLedger,
        reserved: dict[str, int],
        operation_id: str,
    ) -> None:
        await self._budget.release(
            session, ledger=ledger, reserved=reserved, operation_id=operation_id
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _require(self, session: AsyncSession, draft_id: str) -> RequirementDraft:
        draft = await session.get(RequirementDraft, draft_id)
        if draft is None:
            raise AppError(code="DRAFT_NOT_FOUND", message="draft not found", status=404)
        return draft

    @staticmethod
    def _validate_output_spec(output_spec: dict[str, Any]) -> None:
        allowed = {
            "format": {"markdown"},
            "structure": {"report", "checklist", "comparison"},
            "language": {"zh-CN", "en"},
            "detail": {"brief", "standard"},
        }
        unknown = sorted(set(output_spec) - set(allowed))
        invalid = {
            key: value
            for key, value in output_spec.items()
            if key in allowed and value not in allowed[key]
        }
        if unknown or invalid:
            raise AppError(
                code="OUTPUT_SPEC_INVALID",
                message="the requested output format is not supported by the fixed workflow",
                status=422,
                details={"unknown": unknown, "invalid": invalid},
            )

    @staticmethod
    def _assert_revision(draft: RequirementDraft, expected: int) -> None:
        if draft.revision != expected:
            raise AppError(
                code="DRAFT_REVISION_STALE",
                message="the draft changed since it was read; reload and retry",
                status=412,
                details={"current_revision": draft.revision},
            )

    async def _recompute(
        self,
        session: AsyncSession,
        draft: RequirementDraft,
        definition: WorkflowDefinition,
    ) -> None:
        required = required_paths(definition.input_schema)
        fields = {
            path: FieldValue(
                path=path,
                value=entry.get("value"),
                source=FieldSource(entry.get("source", "user")),
                message_id=entry.get("message_id"),
                suggestion_id=entry.get("suggestion_id"),
                accepted_at=entry.get("accepted_at"),
                explicit_none=bool(entry.get("explicit_none", False)),
            )
            for path, entry in draft.field_values_json.items()
        }
        missing = sorted(
            set(missing_fields(required, fields))
            | set(validate_field_values(definition.input_schema, fields))
        )
        conflicts = list(draft.conflicts_json)
        accepted_rows = list((await session.scalars(
            select(Suggestion)
            .where(Suggestion.draft_id == draft.id, Suggestion.status == "ACCEPTED")
            .order_by(Suggestion.created_at)
        )).all())
        accepted = [
            {
                "field_path": row.field_path,
                "value": row.value_json,
                "rationale": row.rationale,
            }
            for row in accepted_rows
        ]
        run_budget = (
            effective_run_limits(self._run_limits, definition.execution_policy.model_dump())
            if self._run_limits else {}
        )
        draft.missing_fields_json = missing
        draft.summary_markdown = render_summary(
            workflow_name=definition.name,
            workflow_version_no=await self._version_no(session, draft.selected_version_id),
            fields=dict(draft.field_values_json),
            missing=missing,
            conflicts=conflicts,
            preference_order=list(draft.preference_order_json),
            accepted_suggestions=accepted,
            output_spec=dict(draft.output_spec_json),
            policy_hash=self._policy_hash,
            run_budget=run_budget,
        )
        # The hash binds every input that shapes execution, not only the prose:
        # fields, accepted suggestions, preference order, output spec, version,
        # policy and budget.
        draft.summary_hash = stable_hash({
            "summary": draft.summary_markdown,
            "fields": draft.field_values_json,
            "preference_order": draft.preference_order_json,
            "output_spec": draft.output_spec_json,
            "accepted_suggestions": accepted,
            "policy_hash": self._policy_hash,
            "policy_version": self._policy_version,
            "run_budget": run_budget,
            "workflow_version_id": draft.selected_version_id,
        })
        if draft.status != DraftStatus.STALE.value:
            current = DraftStatus(draft.status)
            draft.status = derive_status(
                required=required,
                fields=fields,
                conflicts=conflicts,
                confirmed=current is DraftStatus.CONFIRMED,
            ).value
        draft.updated_at = self._clock.now()

    @staticmethod
    async def _version_no(session: AsyncSession, version_id: str) -> int:
        version = await session.get(WorkflowVersion, version_id)
        return version.version_no if version else 0


async def touch_confirmation_draft(
    session: AsyncSession, draft: RequirementDraft, *, revision: int
) -> None:
    """CAS helper kept for the engine's confirmation consumption."""
    await cas_update(
        session,
        RequirementDraft,
        identity={"id": draft.id},
        expected_revision=revision,
        values={"updated_at": draft.updated_at},
        stale_code=CONFIRMATION_STALE,
        stale_status=409,
    )


def field_value_from_entry(path: str, entry: dict[str, Any]) -> FieldValue:
    return FieldValue(
        path=path,
        value=entry.get("value"),
        source=FieldSource(entry.get("source", "user")),
        message_id=entry.get("message_id"),
        suggestion_id=entry.get("suggestion_id"),
        accepted_at=entry.get("accepted_at"),
        explicit_none=bool(entry.get("explicit_none", False)),
    )


def ensure_confirmable_without_pending(pending: int) -> None:
    if pending > 0:
        raise AppError(
            code=UNACCEPTED_SUGGESTION,
            message="pending suggestions must be decided before confirmation",
            status=409,
            details={"pending": pending},
        )
