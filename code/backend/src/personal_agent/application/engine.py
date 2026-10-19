"""Workflow engine: the only component that advances business state.

Execution is a deterministic traversal of the declared DAG. Model output never
selects the next step: transitions come from ``transition_rules`` and, for a
Condition, from the bounded expression AST. Retries are bounded; permission,
integrity and unknown-outcome errors are never retried automatically. Every step
attempt is persisted before an external action and finalized only after its
result is validated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import jsonschema  # type: ignore[import-untyped]
from sqlalchemy import select

from personal_agent.adapters.persistence.budget_models import BudgetLedger
from personal_agent.adapters.persistence.database import Database
from personal_agent.adapters.persistence.execution_models import (
    ExecutionEvent,
    ExecutionSlot,
    StepAttempt,
    StepRun,
    WorkflowRun,
)
from personal_agent.adapters.persistence.workflow_models import WorkflowVersion
from personal_agent.adapters.tools.scope import ToolScopeRegistry
from personal_agent.application.artifact_service import ArtifactService
from personal_agent.application.budget_service import BudgetService
from personal_agent.application.research_evidence import EvidenceService
from personal_agent.domain.assistance import AssistanceKind
from personal_agent.domain.common import (
    AppError,
    ErrorCategory,
    new_id,
    stable_hash,
)
from personal_agent.domain.execution import (
    EventKind,
    RunStatus,
    StepStatus,
)
from personal_agent.domain.expressions import evaluate, resolve_binding
from personal_agent.domain.workflow_schema import (
    AgentTaskConfig,
    ConditionConfig,
    HumanReviewConfig,
    OutputConfig,
    Step,
    ToolTaskConfig,
    TransformConfig,
    WorkflowDefinition,
)
from personal_agent.domain.workflow_validation import ALLOWED_TRANSFORMS
from personal_agent.ports.contracts import (
    Clock,
    Owner,
    RuntimePort,
    RuntimeTask,
    SessionContext,
    ToolPort,
    ToolRequest,
    Usage,
)

RETRYABLE_CATEGORIES = {
    ErrorCategory.DEPENDENCY,
    ErrorCategory.TIMEOUT,
    ErrorCategory.VALIDATION,
}
NON_RETRYABLE_CATEGORIES = {
    ErrorCategory.PERMISSION,
    ErrorCategory.INTEGRITY,
    ErrorCategory.UNKNOWN_OUTCOME,
}


@dataclass(slots=True)
class EngineDeps:
    database: Database
    clock: Clock
    runtime: RuntimePort
    tools: ToolPort
    artifacts: ArtifactService
    budget: BudgetService
    evidence: EvidenceService
    policy: Any
    model_id: str
    capability_token: str
    scopes: ToolScopeRegistry | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(slots=True)
class StepOutcome:
    output: dict[str, Any] = field(default_factory=dict)
    on: str = "success"
    waiting: bool = False
    terminal: bool = False


class WorkflowEngine:
    def __init__(self, deps: EngineDeps) -> None:
        self._deps = deps
        self._evidence_summary: dict[str, Any] = {}
        self._sources: list[dict[str, Any]] = []

    async def run(self, run_id: str) -> str:
        # The engine instance is reused; per-run evidence state must never leak.
        self._evidence_summary = {}
        self._sources = []
        run, definition = await self._load(run_id)
        if run.status != RunStatus.RUNNING.value:
            return run.status
        inputs = {
            path: entry.get("value")
            for path, entry in (run.input_snapshot_json.get("fields") or {}).items()
        }
        outputs: dict[str, dict[str, Any]] = {}
        steps = {step.id: step for step in definition.steps}
        current: str | None = definition.entry_step
        guard = 0
        try:
            while current is not None:
                guard += 1
                if guard > len(steps) * 4 + 8:
                    raise AppError(
                        code="ENGINE_LOOP_GUARD",
                        message="the traversal exceeded the bounded step guard",
                        category=ErrorCategory.INTEGRITY,
                        status=500,
                    )
                step = steps[current]
                outcome = await self._execute_with_retries(
                    run, definition, step, inputs, outputs
                )
                if outcome.waiting:
                    return await self._status(run_id)
                if outcome.terminal:
                    await self._deliver(run, definition, step, outcome, inputs)
                    return await self._status(run_id)
                outputs[step.id] = outcome.output
                edge = self._pick_transition(definition, step, outcome.on)
                if edge is None:
                    raise AppError(
                        code="NO_TRANSITION",
                        message=f"step {step.id!r} has no {outcome.on!r} transition",
                        category=ErrorCategory.INTEGRITY,
                        status=500,
                    )
                if step.type == "Condition":
                    await self._skip_unselected_branch(run, definition, step, edge.to)
                current = edge.to
            raise AppError(
                code="ENGINE_NO_OUTPUT",
                message="the traversal ended without an Output step",
                category=ErrorCategory.INTEGRITY,
                status=500,
            )
        except AppError as error:
            await self._fail(run, error)
            return await self._status(run_id)

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------
    async def _execute_with_retries(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        step: Step,
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
    ) -> StepOutcome:
        if step.type == "Output":
            attempt = await self._start_attempt(run, step, attempt_no=1)
            outcome = StepOutcome(terminal=True)
            await self._finish_attempt(attempt, outcome, Usage())
            return outcome

        policy = definition.execution_policy
        attempts = 0
        last_error: AppError | None = None
        while attempts < policy.max_attempts_per_step:
            attempts += 1
            attempt = await self._start_attempt(run, step, attempt_no=attempts)
            try:
                outcome = await self._dispatch(run, definition, step, inputs, outputs)
            except AppError as error:
                last_error = error
                await self._finish_attempt(attempt, StepOutcome(), Usage(), error=error)
                retryable = error.category in RETRYABLE_CATEGORIES
                if error.category in NON_RETRYABLE_CATEGORIES or not retryable:
                    break
                if attempts < policy.max_attempts_per_step:
                    backoff = self._deps.policy.model.retry_backoff_seconds
                    index = min(attempts - 1, len(backoff) - 1)
                    await self._deps.sleep(float(backoff[index]))
                continue
            usage = outcome.output.pop("__usage__", Usage())
            await self._finish_attempt(attempt, outcome, usage)
            return outcome

        exhausted = self._exhausted_transition(definition, step)
        if exhausted is not None and last_error is not None:
            await self._set_step_status(run, step, StepStatus.FAILED, reason=last_error.code)
            return StepOutcome(output={"error": last_error.code}, on="exhausted")
        raise last_error or AppError(
            code="STEP_FAILED",
            message=f"step {step.id!r} failed",
            status=500,
        )

    async def _dispatch(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        step: Step,
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
    ) -> StepOutcome:
        config = step.config
        if isinstance(config, ConditionConfig):
            value = evaluate(
                config.expression,
                lambda binding: resolve_binding(
                    binding, input_values=inputs, step_outputs=outputs
                ),
            )
            return StepOutcome(output={"result": value}, on="true" if value else "false")
        if isinstance(config, TransformConfig):
            if config.transform_id not in ALLOWED_TRANSFORMS:
                raise AppError(
                    code="TRANSFORM_UNKNOWN",
                    message=f"unknown transform {config.transform_id!r}",
                    status=422,
                )
            merged = self._apply_transform(config.transform_id, step, outputs)
            return StepOutcome(output=merged)
        if isinstance(config, HumanReviewConfig):
            await self._request_human_review(run, step, config)
            return StepOutcome(waiting=True)
        if isinstance(config, ToolTaskConfig):
            return await self._run_tool(run, definition, step, config, inputs)
        if isinstance(config, AgentTaskConfig):
            return await self._run_agent(run, definition, step, config, inputs)
        if isinstance(config, OutputConfig):
            return StepOutcome(terminal=True)
        raise AppError(
            code="STEP_CONFIG_INVALID", message="unknown step configuration", status=422
        )

    # -- AgentTask ------------------------------------------------------
    async def _run_agent(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        step: Step,
        config: AgentTaskConfig,
        inputs: dict[str, Any],
    ) -> StepOutcome:
        goal = self._render_goal(config.goal_template, inputs)
        reservation = self._deps.policy.model.max_output_tokens + len(goal) * 2 + 64
        ledger = await self._ledger(run)
        await self._reserve(
            ledger,
            {"model_requests": 1, "tokens": reservation},
            operation_id=f"{run.id}:{step.id}:model",
        )
        session = await self._deps.runtime.create_session(
            SessionContext(
                operation_id=f"{run.id}:{step.id}:agent",
                owner=Owner(run_id=run.id),
                epoch=run.epoch,
                model_id=self._deps.model_id,
                profile_hash="profile",
                capability_token=self._deps.capability_token,
                policy_hash=run.budget_policy_hash,
            )
        )
        submission = await self._deps.runtime.submit(
            RuntimeTask(
                operation_id=f"{run.id}:{step.id}:agent",
                session_id=session.adapter_session_id,
                epoch=run.epoch,
                goal=goal,
                context_refs=tuple(config.context_bindings),
                validated_input=_validated_input(run.id, inputs),
                output_schema=step.output_schema,
                deadline=self._deps.clock.now(),
                tool_scope=tuple(step.policy.allowed_tools),
                budget_reservation_id=f"{run.id}:{step.id}:model",
                direction_ids=tuple(config.direction_ids),
            )
        )
        result = await self._deps.runtime.wait_result(submission.submission_id)
        if result.status == "unknown":
            await self._request_runtime_uncertain(run, step, result)
            return StepOutcome(waiting=True)
        if result.status != "completed" or result.structured_output is None:
            raise result.error or AppError(
                code="AGENT_FAILED",
                message="the agent task did not complete",
                category=ErrorCategory.DEPENDENCY,
            )
        output = dict(result.structured_output)
        self._validate_output(step, output)
        await self._settle(
            ledger,
            {"model_requests": 1, "tokens": reservation},
            {
                "model_requests": 1,
                "tokens": result.usage.tokens or reservation,
            },
            operation_id=f"{run.id}:{step.id}:model",
        )
        summary = await self._record_evidence(run, step, definition, output, result.usage)
        output["__evidence__"] = summary
        output["__usage__"] = result.usage
        self._evidence_summary = summary
        return StepOutcome(output=output)

    async def _record_evidence(
        self,
        run: WorkflowRun,
        step: Step,
        definition: WorkflowDefinition,
        output: dict[str, Any],
        usage: Usage,
    ) -> dict[str, Any]:
        sources = [item for item in output.get("sources") or [] if isinstance(item, dict)]
        self._sources.extend(sources)
        async with self._deps.database.session() as session:
            summary = await self._deps.evidence.record(
                session,
                run_id=run.id,
                attempt_id=None,
                defined_direction_ids=[direction.id for direction in definition.directions],
                output=output,
                sources=sources,
                actions=[
                    {
                        "direction_id": direction_id,
                        "purpose": "collect",
                        "tool_name": "agent",
                        "arguments_ref": f"goal:{step.id}",
                        "result_ref": None,
                        "scope_hash": stable_hash(sorted(step.policy.allowed_tools)),
                    }
                    for direction_id in (
                        step.config.direction_ids
                        if isinstance(step.config, AgentTaskConfig)
                        else []
                    )
                ],
                confirmed_priority=list(run.input_snapshot_json.get("preference_order") or []),
            )
            await session.commit()
        return summary

    # -- ToolTask -------------------------------------------------------
    async def _run_tool(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        step: Step,
        config: ToolTaskConfig,
        inputs: dict[str, Any],
    ) -> StepOutcome:
        if config.tool_name not in step.policy.allowed_tools:
            raise AppError(
                code="TOOL_SCOPE_DENIED",
                message="the tool is not allowed for this step",
                category=ErrorCategory.PERMISSION,
            )
        ledger = await self._ledger(run)
        worst_case: dict[str, int] = {"tool_calls": 1}
        if config.tool_name == "search.web":
            worst_case["search_calls"] = 1
        if config.tool_name in {"http.fetch", "browser.read"}:
            worst_case["page_reads"] = 1
        await self._reserve(ledger, worst_case, operation_id=f"{run.id}:{step.id}:tool")
        result = await self._deps.tools.execute(
            ToolRequest(
                operation_id=f"{run.id}:{step.id}:tool",
                run_id=run.id,
                attempt_id=run.id,
                epoch=run.epoch,
                tool_name=config.tool_name,
                args=dict(config.arguments_bindings),
                scope_token=self._issue_scope_token(run, step, config),
                deadline=self._deps.clock.now(),
            )
        )
        if result.status != "ok":
            await self._settle(
                ledger,
                worst_case,
                {"tool_calls": 1},
                operation_id=f"{run.id}:{step.id}:tool",
            )
            raise result.error or AppError(
                code="TOOL_FAILED",
                message="the tool failed",
                category=ErrorCategory.DEPENDENCY,
            )
        await self._settle(
            ledger, worst_case, {"tool_calls": 1}, operation_id=f"{run.id}:{step.id}:tool"
        )
        output = {"status": "ok", "source_refs": list(result.source_refs)}
        self._sources.extend(
            {
                "source_id": ref,
                "url": ref,
                "title": ref,
                "fetched_at": self._deps.clock.now(),
            }
            for ref in result.source_refs
            if str(ref).startswith("http")
        )
        return StepOutcome(output=output)

    def _issue_scope_token(self, run: WorkflowRun, step: Step, config: ToolTaskConfig) -> str:
        if self._deps.scopes is None:
            return stable_hash({"run": run.id, "step": step.id, "epoch": run.epoch})
        return self._deps.scopes.issue(
            run_id=run.id,
            attempt_id=f"{run.id}:{step.id}",
            epoch=run.epoch,
            tool_name=config.tool_name,
            allowed_domains=step.policy.allowed_domains,
            now=self._deps.clock.now(),
        )

    # -- Transform ------------------------------------------------------
    def _apply_transform(
        self, transform_id: str, step: Step, outputs: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        inputs = [
            outputs.get(dependency, {}) for dependency in step.depends_on
        ]
        if transform_id == "dedupe":
            seen: set[str] = set()
            items: list[Any] = []
            source_ids: list[str] = []
            has_gaps = False
            has_conflicts = False
            for payload in inputs:
                for item in payload.get("items") or []:
                    key = stable_hash(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(item)
                source_ids.extend(payload.get("source_ids") or [])
                has_gaps = has_gaps or bool(payload.get("has_gaps"))
                has_conflicts = has_conflicts or bool(payload.get("has_conflicts"))
            for direction in (self._evidence_summary.get("directions") or []):
                if direction.get("status") == "missing":
                    has_gaps = True
            return {
                "items": items,
                "source_ids": sorted(set(source_ids)),
                "has_gaps": has_gaps,
                "has_conflicts": has_conflicts,
            }
        if transform_id == "sort":
            values = [item for payload in inputs for item in payload.get("items") or []]
            return {"items": sorted(values, key=str)}
        if transform_id == "merge":
            merged: dict[str, Any] = {}
            for payload in inputs:
                merged.update(payload)
            return merged
        raise AppError(
            code="TRANSFORM_UNKNOWN", message="unknown transform", status=422
        )

    # -- Output ---------------------------------------------------------
    async def _deliver(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        step: Step,
        outcome: StepOutcome,
        inputs: dict[str, Any],
    ) -> None:
        await self._deps.artifacts.deliver(
            run=run,
            definition=definition,
            step=step,
            inputs=inputs,
            evidence=self._evidence_summary,
            sources=self._sources,
        )
        await self._set_step_status(run, step, StepStatus.SUCCEEDED)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    async def _load(self, run_id: str) -> tuple[WorkflowRun, WorkflowDefinition]:
        async with self._deps.database.session() as session:
            run = await session.get(WorkflowRun, run_id)
            if run is None:
                raise AppError(code="RUN_NOT_FOUND", message="run not found", status=404)
            version = await session.get(WorkflowVersion, run.workflow_version_id)
            if version is None:
                raise AppError(
                    code="VERSION_NOT_FOUND", message="workflow version not found", status=404
                )
            return run, WorkflowDefinition.model_validate(version.definition_json)

    async def _start_attempt(
        self, run: WorkflowRun, step: Step, *, attempt_no: int
    ) -> StepAttempt:
        async with self._deps.database.session() as session:
            step_run = await session.scalar(
                select(StepRun).where(StepRun.run_id == run.id, StepRun.step_id == step.id)
            )
            if step_run is None:
                step_run = StepRun(
                    id=new_id(),
                    run_id=run.id,
                    step_id=step.id,
                    status=StepStatus.PENDING.value,
                    current_attempt_no=0,
                    selected_transition=None,
                    skip_reason=None,
                    revision=1,
                    created_at=self._deps.clock.now(),
                    updated_at=self._deps.clock.now(),
                )
                session.add(step_run)
                await session.flush()
            step_run.status = StepStatus.RUNNING.value
            step_run.current_attempt_no = attempt_no
            step_run.revision += 1
            step_run.updated_at = self._deps.clock.now()
            attempt = StepAttempt(
                id=new_id(),
                step_run_id=step_run.id,
                attempt_no=attempt_no,
                epoch=run.epoch,
                status="RUNNING",
                input_json={},
                input_hash=stable_hash({"step": step.id, "attempt": attempt_no}),
                output_json=None,
                output_hash=None,
                started_at=self._deps.clock.now(),
                ended_at=None,
                error_category=None,
                retry_reason=None,
                session_link_id=None,
            )
            session.add(attempt)
            run.current_step_id = step.id
            run.revision += 1
            session.add(
                ExecutionEvent(
                    id=new_id(),
                    run_id=run.id,
                    draft_id=None,
                    sequence=await self._next_sequence(session, run.id),
                    step_run_id=step_run.id,
                    attempt_id=attempt.id,
                    workflow_version_id=run.workflow_version_id,
                    occurred_at=self._deps.clock.now(),
                    kind=EventKind.STEP_STATE_CHANGED.value,
                    status=StepStatus.RUNNING.value,
                    reason_code=None,
                    payload_json={"step_id": step.id, "attempt": attempt_no},
                    payload_ref=None,
                    schema_version=1,
                    provider_event_key=None,
                )
            )
            await session.commit()
            return attempt

    async def _finish_attempt(
        self,
        attempt: StepAttempt,
        outcome: StepOutcome,
        usage: Usage,
        *,
        error: AppError | None = None,
    ) -> None:
        async with self._deps.database.session() as session:
            row = await session.get(StepAttempt, attempt.id)
            if row is None:
                return
            row.status = "SUCCEEDED" if error is None else "FAILED"
            row.ended_at = self._deps.clock.now()
            row.error_category = error.category.value if error else None
            row.output_json = {
                key: value for key, value in outcome.output.items() if not key.startswith("__")
            }
            row.output_hash = stable_hash(row.output_json)
            await session.commit()

    async def _set_step_status(
        self, run: WorkflowRun, step: Step, status: StepStatus, *, reason: str | None = None
    ) -> None:
        async with self._deps.database.session() as session:
            step_run = await session.scalar(
                select(StepRun).where(StepRun.run_id == run.id, StepRun.step_id == step.id)
            )
            if step_run is None:
                return
            step_run.status = status.value
            step_run.skip_reason = reason
            step_run.revision += 1
            step_run.updated_at = self._deps.clock.now()
            await session.commit()

    async def _skip_unselected_branch(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        step: Step,
        selected_to: str,
    ) -> None:
        others = [
            rule
            for rule in definition.transition_rules
            if rule.from_ == step.id and rule.to != selected_to
        ]
        if not others:
            return
        shared = {
            rule.to
            for rule in definition.transition_rules
            if rule.from_ != step.id and rule.to in {item.to for item in others}
        }
        async with self._deps.database.session() as session:
            for rule in others:
                if rule.to in shared:
                    continue
                step_run = await session.scalar(
                    select(StepRun).where(StepRun.run_id == run.id, StepRun.step_id == rule.to)
                )
                if step_run is None:
                    session.add(
                        StepRun(
                            id=new_id(),
                            run_id=run.id,
                            step_id=rule.to,
                            status=StepStatus.SKIPPED.value,
                            current_attempt_no=0,
                            selected_transition=None,
                            skip_reason=f"condition {step.id} selected {selected_to}",
                            revision=1,
                            created_at=self._deps.clock.now(),
                            updated_at=self._deps.clock.now(),
                        )
                    )
            await session.commit()

    async def _request_human_review(
        self, run: WorkflowRun, step: Step, config: HumanReviewConfig
    ) -> None:
        from personal_agent.adapters.persistence.assistance_models import (
            AssistanceRequest,
        )

        async with self._deps.database.session() as session:
            session.add(
                AssistanceRequest(
                    id=new_id(),
                    run_id=run.id,
                    kind=AssistanceKind.HUMAN_REVIEW.value,
                    limit_snapshot={},
                    consumed_snapshot={},
                    completed_steps=[run.current_step_id or step.id],
                    remaining_steps=[],
                    observed_problem=config.prompt,
                    unknowns=[],
                    attempted_remedies=[],
                    status="OPEN",
                    revision=1,
                    created_at=self._deps.clock.now(),
                )
            )
            row = await session.get(WorkflowRun, run.id)
            if row is not None:
                row.status = RunStatus.WAITING_USER.value
                row.reason_code = config.reason_code
                row.revision += 1
            await session.commit()
        await self._set_step_status(run, step, StepStatus.WAITING_USER)

    async def _request_runtime_uncertain(
        self, run: WorkflowRun, step: Step, result: Any
    ) -> None:
        from personal_agent.adapters.persistence.assistance_models import (
            AssistanceRequest,
        )

        async with self._deps.database.session() as session:
            session.add(
                AssistanceRequest(
                    id=new_id(),
                    run_id=run.id,
                    kind=AssistanceKind.RUNTIME_UNCERTAIN.value,
                    limit_snapshot={},
                    consumed_snapshot={},
                    completed_steps=[],
                    remaining_steps=[step.id],
                    observed_problem="模型结果无法确认，执行已暂停等待核验",
                    unknowns=[getattr(result.error, "code", "UNKNOWN")],
                    attempted_remedies=[],
                    status="OPEN",
                    revision=1,
                    created_at=self._deps.clock.now(),
                )
            )
            row = await session.get(WorkflowRun, run.id)
            if row is not None:
                row.status = RunStatus.WAITING_USER.value
                row.reason_code = "MODEL_OUTCOME_UNKNOWN"
                row.revision += 1
            await session.commit()
        await self._set_step_status(run, step, StepStatus.WAITING_USER)

    async def _fail(self, run: WorkflowRun, error: AppError) -> None:
        async with self._deps.database.session() as session:
            row = await session.get(WorkflowRun, run.id)
            if row is None or row.status in {
                RunStatus.SUCCEEDED.value,
                RunStatus.CANCELLED.value,
            }:
                return
            row.status = RunStatus.FAILED.value
            row.reason_code = error.code
            row.revision += 1
            session.add(
                ExecutionEvent(
                    id=new_id(),
                    run_id=row.id,
                    draft_id=None,
                    sequence=await self._next_sequence(session, row.id),
                    step_run_id=None,
                    attempt_id=None,
                    workflow_version_id=row.workflow_version_id,
                    occurred_at=self._deps.clock.now(),
                    kind=EventKind.RUN_STATE_CHANGED.value,
                    status=row.status,
                    reason_code=error.code,
                    payload_json={"error": error.code},
                    payload_ref=None,
                    schema_version=1,
                    provider_event_key=None,
                )
            )
            await self._release_slot(session, row)
            await session.commit()

    async def _release_slot(self, session: Any, run: WorkflowRun) -> None:
        slot = await session.get(ExecutionSlot, 1)
        if slot is not None and slot.run_id == run.id:
            slot.run_id = None
            slot.draining = False
            slot.epoch += 1
            slot.revision += 1

    async def _status(self, run_id: str) -> str:
        async with self._deps.database.session() as session:
            run = await session.get(WorkflowRun, run_id)
            return run.status if run else "UNKNOWN"

    async def _next_sequence(self, session: Any, run_id: str) -> int:
        from sqlalchemy import func

        value = await session.scalar(
            select(func.max(ExecutionEvent.sequence)).where(ExecutionEvent.run_id == run_id)
        )
        return int(value or 0) + 1

    async def _ledger(self, run: WorkflowRun) -> BudgetLedger:
        async with self._deps.database.session() as session:
            ledger = await session.scalar(
                select(BudgetLedger).where(BudgetLedger.root_run_id == run.root_run_id)
            )
            if ledger is None:
                limits = self._deps.policy.budget.as_dict()
                ledger = await self._deps.budget.ensure_run_ledger(
                    session, root_run_id=run.root_run_id, limits=limits
                )
                await session.commit()
            return ledger

    async def _reserve(
        self, ledger: BudgetLedger, amount: dict[str, int], *, operation_id: str
    ) -> None:
        async with self._deps.database.session() as session:
            row = await session.get(BudgetLedger, ledger.id)
            if row is None:
                return
            await self._deps.budget.reserve(
                session, ledger=row, worst_case=amount, operation_id=operation_id
            )
            await session.commit()

    async def _settle(
        self,
        ledger: BudgetLedger,
        reserved: dict[str, int],
        actual: dict[str, int],
        *,
        operation_id: str,
    ) -> None:
        async with self._deps.database.session() as session:
            row = await session.get(BudgetLedger, ledger.id)
            if row is None:
                return
            await self._deps.budget.settle(
                session,
                ledger=row,
                reserved=reserved,
                actual=actual,
                operation_id=operation_id,
            )
            await session.commit()

    # ------------------------------------------------------------------
    def _pick_transition(
        self, definition: WorkflowDefinition, step: Step, on: str
    ) -> Any | None:
        for rule in definition.transition_rules:
            if rule.from_ == step.id and rule.on == on:
                return rule
        return None

    def _exhausted_transition(
        self, definition: WorkflowDefinition, step: Step
    ) -> Any | None:
        return self._pick_transition(definition, step, "exhausted")

    @staticmethod
    def _render_goal(template: str, inputs: dict[str, Any]) -> str:
        rendered = template
        for path, value in inputs.items():
            rendered = rendered.replace("{" + path.lstrip("/") + "}", str(value))
        return rendered

    @staticmethod
    def _validate_output(step: Step, output: dict[str, Any]) -> None:
        schema = step.output_schema
        if not schema:
            return
        try:
            jsonschema.validate(instance=output, schema=schema)
        except jsonschema.ValidationError as error:
            raise AppError(
                code="OUTPUT_SCHEMA_INVALID",
                message="the model output did not match the declared schema",
                category=ErrorCategory.VALIDATION,
                status=422,
                details={"path": list(error.absolute_path)},
            ) from error


def _validated_input(run_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"run_id": run_id, "attempt_id": run_id, "inputs": inputs}
    # Test-only observation override. Production inputs never carry this key
    # because the workflow input schema rejects unknown properties.
    scenario = inputs.get("mock_scenario")
    if isinstance(scenario, str) and scenario:
        payload["mock_scenario"] = scenario
    return payload


def _iso(value: Any) -> str:
    if isinstance(value, str):
        return value
    if hasattr(value, "astimezone"):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)



