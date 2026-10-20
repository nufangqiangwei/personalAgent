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
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, timedelta
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
from personal_agent.application.event_log import event_session
from personal_agent.application.research_evidence import EvidenceService
from personal_agent.domain.assistance import AssistanceKind
from personal_agent.domain.budget import effective_run_limits
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
from personal_agent.domain.expressions import MISSING, evaluate, resolve_binding
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
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimePort,
    RuntimeTask,
    SessionContext,
    SessionHandle,
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
    control: Any | None = None
    profile_hash: str = "profile"
    # The live CLI Loop meters each model call itself (T105); the engine then
    # must not hold a single attempt-level model reservation.
    per_call_model_budget: bool = False
    stop_signal: Any | None = None


@dataclass(slots=True)
class StepOutcome:
    output: dict[str, Any] = field(default_factory=dict)
    on: str = "success"
    waiting: bool = False
    terminal: bool = False
    # The attempt and its step completion were committed together.
    recorded: bool = False


class WorkflowEngine:
    def __init__(self, deps: EngineDeps) -> None:
        self._deps = deps
        self._evidence_summary: dict[str, Any] = {}
        self._sources: list[dict[str, Any]] = []
        self._pending_output_attempt: StepAttempt | None = None

    async def run(self, run_id: str, *, resume: bool = False) -> str:
        # The engine instance is reused; per-run evidence state must never leak.
        self._evidence_summary = {}
        self._sources = []
        run, definition = await self._load(run_id)
        if run.status != RunStatus.RUNNING.value:
            return run.status
        if self._deps.stop_signal is not None:
            self._deps.stop_signal.stopping = False
        inputs = {
            path: entry.get("value")
            for path, entry in (run.input_snapshot_json.get("fields") or {}).items()
        }
        outputs: dict[str, dict[str, Any]] = {}
        steps = {step.id: step for step in definition.steps}
        current: str | None = definition.entry_step
        if resume or run.parent_run_id is not None:
            outputs, current = await self._resume_point(run, definition)
            # Reused work must carry the evidence and sources the Output step
            # needs; restoring only the raw values loses the document.
            await self._restore_reuse(run)
        guard = 0
        active_tick = self._deps.clock.monotonic_ms()
        incoming = "success"
        if resume or run.parent_run_id is not None:
            if await self._has_exhausted_edge(run):
                incoming = "exhausted"

        async def tick() -> None:
            nonlocal active_tick
            active_tick = await self._charge_active(run, active_tick)

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
                stopped = await self._stop_check(run_id)
                if stopped is not None:
                    return stopped
                step = steps[current]
                await tick()
                await self._require_budget(run)
                try:
                    outcome = await self._execute_with_retries(
                        run, definition, step, inputs, outputs, tick,
                        degraded=incoming == "exhausted",
                    )
                finally:
                    await tick()
                if not outcome.waiting and not outcome.terminal and not outcome.recorded:
                    # Record the finished step before a stop can take effect: the
                    # work really happened and a resumed run must not redo it.
                    await self._complete_step(run, step, outcome.on)
                stopped = await self._stop_check(run_id)
                if stopped is not None:
                    return stopped
                if outcome.waiting:
                    return await self._status(run_id)
                if outcome.terminal:
                    attempt = self._pending_output_attempt
                    self._pending_output_attempt = None
                    try:
                        # The artifact must exist before the Output attempt can
                        # be called successful.
                        await self._deliver(run, definition, step, outcome, inputs)
                    except AppError as error:
                        if attempt is not None:
                            await self._finish_attempt(
                                run, attempt, StepOutcome(), Usage(), error=error,
                            )
                        raise
                    if attempt is not None:
                        await self._finish_attempt(
                            run, attempt, outcome, Usage(), allow_terminal=True,
                        )
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
                incoming = outcome.on
                current = edge.to
            if resume:
                # A resumed traversal may find every declared step already done.
                return await self._status(run_id)
            raise AppError(
                code="ENGINE_NO_OUTPUT",
                message="the traversal ended without an Output step",
                category=ErrorCategory.INTEGRITY,
                status=500,
            )
        except AppError as error:
            if error.category == ErrorCategory.BUDGET:
                await self._request_budget_assistance(run, definition, current, error)
                return await self._status(run_id)
            if error.code == "OUTPUT_CANCELLED":
                # The stop convergence owns the run state; this is not a failure.
                return await self._status(run_id)
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
        tick: Callable[[], Awaitable[None]],
        *,
        degraded: bool = False,
    ) -> StepOutcome:
        offset = await self._attempt_offset(run, step)
        payload = self._attempt_payload(step, inputs, outputs)
        if step.type == "Output":
            attempt = await self._start_attempt(
                run, step, attempt_no=offset + 1, payload=payload,
            )
            self._pending_output_attempt = attempt
            return StepOutcome(terminal=True)

        policy = definition.execution_policy
        attempts = 0
        last_error: AppError | None = None
        max_attempts = min(policy.max_attempts_per_step, step.policy.max_attempts,
                           self._deps.policy.model.max_attempts_per_step)
        while offset + attempts < max_attempts:
            await tick()
            await self._require_budget(run)
            attempts += 1
            attempt = await self._start_attempt(
                run, step, attempt_no=offset + attempts, payload=payload,
            )
            try:
                outcome = await self._dispatch_bounded(
                    run, definition, step, inputs, outputs, attempt, degraded=degraded,
                )
            except AppError as error:
                await tick()
                last_error = error
                await self._finish_attempt(run, attempt, StepOutcome(), Usage(), error=error)
                # A stop may revoke model permission while submit is waiting.
                # That refusal must converge the requested stop, not turn it
                # into an ordinary failed run or start a retry.
                if await self._stop_check(run.id) is not None:
                    return StepOutcome(waiting=True)
                if error.category == ErrorCategory.BUDGET:
                    raise
                retryable = error.category in RETRYABLE_CATEGORIES
                if error.category in NON_RETRYABLE_CATEGORIES or not retryable:
                    break
                if offset + attempts < max_attempts:
                    backoff = self._deps.policy.model.retry_backoff_seconds
                    index = min(attempts - 1, len(backoff) - 1)
                    await self._deps.sleep(float(backoff[index]))
                continue
            usage = outcome.output.pop("__usage__", Usage())
            recorded = await self._finish_attempt(run, attempt, outcome, usage, on=outcome.on)
            if not recorded:
                # The result was quarantined (stale epoch/terminal/cancel); the
                # stop convergence owns the run state from here.
                return StepOutcome(waiting=True)
            outcome.recorded = True
            return outcome

        exhausted = self._exhausted_transition(definition, step)
        if exhausted is not None and last_error is not None:
            await self._set_step_status(
                run, step, StepStatus.FAILED, reason=last_error.code, on="exhausted",
            )
            return StepOutcome(output={"error": last_error.code}, on="exhausted", recorded=True)
        raise last_error or AppError(
            code="STEP_FAILED",
            message=f"step {step.id!r} failed",
            status=500,
        )

    async def _remaining_seconds(self, run: WorkflowRun) -> float:
        ledger = await self._ledger(run)
        return self._deps.budget.to_domain(ledger).remaining().get("active_ms", 0) / 1000

    async def _dispatch_bounded(
        self, run: WorkflowRun, definition: WorkflowDefinition, step: Step,
        inputs: dict[str, Any], outputs: dict[str, dict[str, Any]], attempt: StepAttempt,
        *, degraded: bool = False,
    ) -> StepOutcome:
        active_seconds = await self._remaining_seconds(run)
        node_seconds = step.policy.timeout_seconds
        if step.type == "ToolTask":
            node_seconds = min(node_seconds, definition.execution_policy.tool_timeout_seconds)
        try:
            async with asyncio.timeout(min(active_seconds, node_seconds)):
                return await self._dispatch(
                    run, definition, step, inputs, outputs, attempt, degraded=degraded,
                )
        except TimeoutError as error:
            if active_seconds <= node_seconds:
                raise AppError(
                    code="BUDGET_ASSISTANCE_REQUIRED",
                    message="有效研究时间已到限，已停止等待并保留未核验用量",
                    category=ErrorCategory.BUDGET, status=409,
                    details={"requested": {"active_ms": 1}},
                ) from error
            if step.type == "AgentTask":
                await self._request_runtime_uncertain(run, step, None)
                return StepOutcome(waiting=True)
            raise AppError(
                code="STEP_TIMEOUT", message="步骤超过声明的等待时限",
                category=ErrorCategory.TIMEOUT, status=504,
            ) from error

    async def _dispatch(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        step: Step,
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        attempt: StepAttempt,
        *,
        degraded: bool = False,
    ) -> StepOutcome:
        config = step.config
        # Execution-time dependency and binding checks: a step may not run on a
        # missing predecessor and a named/pointer binding must resolve to a value.
        # An explicit exhausted edge is the one path allowed to continue without
        # the normal data: it exists to collect the user's decision.
        if not degraded:
            for dependency in step.depends_on:
                if dependency not in outputs:
                    raise AppError(
                        code="DEPENDENCY_NOT_READY",
                        message=f"step {dependency!r} has no completed output for {step.id!r}",
                        category=ErrorCategory.INTEGRITY,
                        status=409,
                    )
        resolved = self._resolved_bindings(step, inputs, outputs)
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
            # A review that the user already answered advances the step once; asking
            # again would turn an explicit decision into an endless prompt.
            if await self._review_already_approved(run, step):
                return StepOutcome(output={"review": "approved"}, on="success")
            await self._request_human_review(run, step, config)
            return StepOutcome(waiting=True)
        if isinstance(config, ToolTaskConfig):
            args = {
                key: self._binding_value(step, name, inputs, outputs, resolved)
                for key, name in config.arguments_bindings.items()
            }
            self._validate_step_input(step, args)
            return await self._run_tool(
                run, definition, step, config, inputs, attempt, args=args,
            )
        if isinstance(config, AgentTaskConfig):
            # Context references are optional by nature: a declared context slot
            # that the user left empty is simply not sent to the model.
            refs: list[str] = []
            for name in config.context_bindings:
                try:
                    refs.append(self._as_reference(
                        self._binding_value(step, name, inputs, outputs, resolved)
                    ))
                except AppError as error:
                    if error.code != "BINDING_UNRESOLVED":
                        raise
            context_refs = tuple(refs)
            if resolved:
                self._validate_step_input(step, resolved)
            return await self._run_agent(
                run, definition, step, config, inputs, attempt,
                context_refs=context_refs,
            )
        if isinstance(config, OutputConfig):
            return StepOutcome(terminal=True)
        raise AppError(
            code="STEP_CONFIG_INVALID", message="unknown step configuration", status=422
        )

    # -- binding resolution ---------------------------------------------
    def _resolved_bindings(
        self, step: Step, inputs: Mapping[str, Any], outputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for name, binding in step.bindings.items():
            value = resolve_binding(
                binding, input_values=inputs, step_outputs=outputs,
            )
            if value is MISSING:
                raise AppError(
                    code="BINDING_UNRESOLVED",
                    message=f"binding {name!r} of step {step.id!r} has no value",
                    category=ErrorCategory.VALIDATION,
                    status=409,
                )
            resolved[name] = value
        return resolved

    def _binding_value(
        self, step: Step, name: str, inputs: Mapping[str, Any],
        outputs: Mapping[str, Any], resolved: Mapping[str, Any],
    ) -> Any:
        if name in resolved:
            return resolved[name]
        if isinstance(name, str) and name.startswith("/"):
            return self._resolve_pointer(name, inputs, outputs)
        # A plain string is a literal argument value; only a pointer or a named
        # binding refers to stored data.
        return name

    @staticmethod
    def _resolve_pointer(
        pointer: str, inputs: Mapping[str, Any], outputs: Mapping[str, Any],
    ) -> Any:
        tokens = [
            token.replace("~1", "/").replace("~0", "~")
            for token in pointer.lstrip("/").split("/")
        ]
        if tokens and tokens[0] in outputs:
            current: Any = outputs[tokens[0]]
            tokens = tokens[1:]
        else:
            current = inputs
        for token in tokens:
            if isinstance(current, Mapping) and token in current:
                current = current[token]
            elif isinstance(current, list):
                try:
                    current = current[int(token)]
                except (TypeError, ValueError, IndexError):
                    raise AppError(
                        code="BINDING_UNRESOLVED",
                        message=f"pointer {pointer!r} does not resolve",
                        category=ErrorCategory.VALIDATION, status=409,
                    ) from None
            else:
                raise AppError(
                    code="BINDING_UNRESOLVED",
                    message=f"pointer {pointer!r} does not resolve",
                    category=ErrorCategory.VALIDATION, status=409,
                )
        if current is None:
            raise AppError(
                code="BINDING_UNRESOLVED",
                message=f"pointer {pointer!r} resolved to null",
                category=ErrorCategory.VALIDATION, status=409,
            )
        return current

    @staticmethod
    def _as_reference(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _validate_step_input(step: Step, value: Any) -> None:
        schema = step.input_schema
        if not isinstance(schema, dict) or not schema:
            return
        try:
            validator = jsonschema.Draft202012Validator(schema)
        except jsonschema.exceptions.SchemaError:
            raise AppError(
                code="STEP_SCHEMA_INVALID",
                message=f"step {step.id!r} has an invalid input schema",
                category=ErrorCategory.INTEGRITY, status=500,
            ) from None
        errors = sorted(
            validator.iter_errors(value), key=lambda error: list(error.absolute_path)
        )
        if errors:
            raise AppError(
                code="STEP_INPUT_INVALID",
                message=f"the input of step {step.id!r} does not match its schema",
                category=ErrorCategory.VALIDATION,
                status=422,
                details={
                    "step": step.id,
                    "errors": [
                        {
                            "path": "/".join(str(part) for part in error.absolute_path),
                            "message": error.message,
                        }
                        for error in errors[:10]
                    ],
                },
            )

    # -- AgentTask ------------------------------------------------------
    async def _run_agent(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        step: Step,
        config: AgentTaskConfig,
        inputs: dict[str, Any],
        attempt: StepAttempt,
        *,
        context_refs: Sequence[str] = (),
    ) -> StepOutcome:
        goal = self._render_goal(config.goal_template, inputs)
        timeout_seconds = min(step.policy.timeout_seconds, await self._remaining_seconds(run))
        reservation = self._deps.policy.model.max_output_tokens + len(goal) * 2 + 64
        ledger = await self._ledger(run)
        if not self._deps.per_call_model_budget:
            await self._reserve(
                ledger,
                {"model_requests": 1, "tokens": reservation},
                operation_id=f"{attempt.id}:model",
            )
        session = await self._deps.runtime.create_session(
            SessionContext(
                operation_id=f"{attempt.id}:agent",
                owner=Owner(run_id=run.id, attempt_id=attempt.id),
                epoch=run.epoch,
                model_id=self._deps.model_id,
                profile_hash=self._deps.profile_hash,
                capability_token=self._deps.capability_token,
                policy_hash=run.budget_policy_hash,
            )
        )
        # Persist the local association before the task can leave the process.
        capabilities = await self._deps.runtime.capabilities()
        await self._link_session(run, attempt, session, capabilities)
        submission = await self._deps.runtime.submit(
            RuntimeTask(
                operation_id=f"{attempt.id}:agent",
                session_id=session.adapter_session_id,
                epoch=run.epoch,
                goal=goal,
                context_refs=context_refs,
                validated_input={**_validated_input(run.id, inputs), "attempt_id": attempt.id},
                output_schema=step.output_schema,
                deadline=self._deps.clock.now() + timedelta(seconds=timeout_seconds),
                tool_scope=tuple(step.policy.allowed_tools),
                budget_reservation_id=f"{attempt.id}:model",
                direction_ids=tuple(config.direction_ids),
                model_reserved_tokens=reservation,
                model_output_limit=self._deps.policy.model.max_output_tokens,
                model_timeout_seconds=min(
                    self._deps.policy.model.timeout_seconds,
                    definition.execution_policy.model_timeout_seconds,
                    step.policy.timeout_seconds,
                    timeout_seconds,
                ),
                allowed_domains=tuple(step.policy.allowed_domains),
                budget_root_run_id=run.root_run_id,
            )
        )
        await self._set_session_status(attempt.id, "SUBMITTED")
        stop_pump = asyncio.Event()
        pump = asyncio.create_task(
            self._pump_runtime_events(
                run, attempt, session.adapter_session_id, stop_pump,
            )
        )
        # The stop signal must reach the runtime while a call is in flight, not
        # only between steps.
        watch = asyncio.create_task(self._watch_stop(run, session.adapter_session_id))
        try:
            result = await self._deps.runtime.wait_result(submission.submission_id)
        finally:
            stop_pump.set()
            watch.cancel()
            await asyncio.gather(pump, watch, return_exceptions=True)
        if result.status == "unknown":
            await self._set_session_status(attempt.id, "UNKNOWN")
            await self._request_runtime_uncertain(run, step, result)
            return StepOutcome(waiting=True)
        if result.completion_evidence_ref and not self._deps.per_call_model_budget:
            await self._settle(
                ledger,
                {"model_requests": 1, "tokens": reservation},
                {"model_requests": 1, "tokens": result.usage.tokens or reservation},
                operation_id=f"{attempt.id}:model",
            )
        if result.status != "completed" or result.structured_output is None:
            raise result.error or AppError(
                code="AGENT_FAILED",
                message="the agent task did not complete",
                category=ErrorCategory.DEPENDENCY,
            )
        output = dict(result.structured_output)
        self._validate_output(step, output)
        summary = await self._record_evidence(run, step, definition, output, result.usage)
        output["__evidence__"] = summary
        output["__usage__"] = result.usage
        self._evidence_summary = summary
        return StepOutcome(output=output)

    async def _pump_runtime_events(
        self, run: WorkflowRun, attempt: StepAttempt, session_id: str,
        stop: asyncio.Event,
    ) -> None:
        """Consume the adapter's cursor-addressed events while the task runs.

        Only whitelisted, non-reasoning facts are persisted. A final drain after
        the result settles guarantees an event emitted during submit/finish is
        not lost.
        """
        cursor = 0
        while True:
            try:
                batch = [
                    event async for event in self._deps.runtime.events(session_id, cursor)
                ]
            except Exception:
                batch = []
            for event in batch:
                cursor = max(cursor, event.sequence)
                await self._record_runtime_event(run, attempt, event)
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.1)
            except TimeoutError:
                continue

    async def _watch_stop(self, run: WorkflowRun, session_id: str) -> None:
        """Forward a pause/cancel intent to the live runtime once it appears."""
        control = self._deps.control
        if control is None:
            return
        while True:
            action = await control.observe(run.id)
            if action is not None:
                if self._deps.stop_signal is not None:
                    self._deps.stop_signal.stopping = True
                try:
                    await self._deps.runtime.request_stop(session_id, action)
                except AppError:
                    pass
                return
            await asyncio.sleep(0.5)

    async def _record_runtime_event(
        self, run: WorkflowRun, attempt: StepAttempt, event: RuntimeEvent,
    ) -> None:
        if event.kind != "action.proposed":
            return
        direction_ids = [
            str(item) for item in (event.payload.get("direction_ids") or [])
        ][:20]
        async with event_session(self._deps.database) as session:
            session.add(ExecutionEvent(
                id=new_id(),
                run_id=run.id,
                draft_id=None,
                sequence=await self._next_sequence(session, run.id),
                step_run_id=attempt.step_run_id,
                attempt_id=attempt.id,
                workflow_version_id=run.workflow_version_id,
                occurred_at=self._deps.clock.now(),
                kind=EventKind.ACTION_STARTED.value,
                status="RUNNING",
                reason_code=None,
                payload_json={"direction_ids": direction_ids, "purpose": "agent"},
                payload_ref=None,
                schema_version=1,
                provider_event_key=None,
            ))
            await session.commit()

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
        attempt: StepAttempt,
        *,
        args: Mapping[str, Any] | None = None,
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
        operation_id = f"{attempt.id}:tool"
        await self._reserve(ledger, worst_case, operation_id=operation_id)
        result = await self._deps.tools.execute(
            ToolRequest(
                operation_id=operation_id,
                run_id=run.id,
                attempt_id=attempt.id,
                epoch=run.epoch,
                tool_name=config.tool_name,
                args=dict(args or {}),
                scope_token=self._issue_scope_token(run, step, config, attempt),
                deadline=self._deps.clock.now() + timedelta(seconds=min(
                    step.policy.timeout_seconds, definition.execution_policy.tool_timeout_seconds,
                    await self._remaining_seconds(run),
                )),
            )
        )
        if result.status != "ok":
            await self._settle(
                ledger,
                worst_case,
                {"tool_calls": 1},
                operation_id=operation_id,
            )
            raise result.error or AppError(
                code="TOOL_FAILED",
                message="the tool failed",
                category=ErrorCategory.DEPENDENCY,
            )
        await self._settle(
            ledger, worst_case, {"tool_calls": 1}, operation_id=operation_id
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

    def _issue_scope_token(
        self, run: WorkflowRun, step: Step, config: ToolTaskConfig, attempt: StepAttempt
    ) -> str:
        if self._deps.scopes is None:
            return stable_hash({"run": run.id, "step": step.id, "epoch": run.epoch})
        return self._deps.scopes.issue(
            run_id=run.id,
            attempt_id=attempt.id,
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
        # A stop request may have converged while the document was being built;
        # the current epoch and terminal state decide whether it may be written.
        async with self._deps.database.session() as session:
            row = await session.get(WorkflowRun, run.id)
        if row is None or row.status != RunStatus.RUNNING.value or row.epoch != run.epoch:
            raise AppError(
                code="OUTPUT_CANCELLED",
                message="the run stopped before its output could be published",
                category=ErrorCategory.INTERRUPTED,
                status=409,
            )
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

    async def _attempt_offset(self, run: WorkflowRun, step: Step) -> int:
        """Continue the attempt numbering so a resumed step never reuses a number."""
        from sqlalchemy import func

        async with self._deps.database.session() as session:
            value = await session.scalar(
                select(func.max(StepAttempt.attempt_no))
                .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                .where(StepRun.run_id == run.id, StepRun.step_id == step.id)
            )
        return int(value or 0)

    @staticmethod
    def _attempt_payload(
        step: Step, inputs: dict[str, Any], outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """The exact input facts an attempt ran with, hashed for recovery checks."""
        return {
            "step": step.id,
            "inputs": inputs,
            "dependencies": {
                dependency: stable_hash(outputs.get(dependency, {}))
                for dependency in step.depends_on
            },
        }

    async def _start_attempt(
        self, run: WorkflowRun, step: Step, *, attempt_no: int,
        payload: dict[str, Any] | None = None,
    ) -> StepAttempt:
        attempt_input = payload if payload is not None else {
            "step": step.id, "inputs": {}, "dependencies": {},
        }
        async with event_session(self._deps.database) as session:
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
                input_json=attempt_input,
                input_hash=stable_hash(attempt_input),
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
        run: WorkflowRun,
        attempt: StepAttempt,
        outcome: StepOutcome,
        usage: Usage,
        *,
        error: AppError | None = None,
        on: str | None = None,
        allow_terminal: bool = False,
    ) -> bool:
        async with event_session(self._deps.database) as session:
            row = await session.get(StepAttempt, attempt.id)
            if row is None:
                return False
            row.status = (
                "INTERRUPTED"
                if outcome.waiting or (
                    error is not None and error.category == ErrorCategory.INTERRUPTED
                )
                else "SUCCEEDED" if error is None else "FAILED"
            )
            row.ended_at = self._deps.clock.now()
            row.error_category = error.category.value if error else None
            row.output_json = {
                key: value for key, value in outcome.output.items() if not key.startswith("__")
            }
            evidence = outcome.output.get("__evidence__")
            if isinstance(evidence, dict) and evidence:
                row.output_json["__evidence__"] = evidence
            if self._sources:
                row.output_json["__sources__"] = list(self._sources)
            row.output_hash = stable_hash({
                key: value for key, value in row.output_json.items()
                if not key.startswith("__")
            })
            run_row = await session.get(WorkflowRun, run.id)
            stale_epoch = run_row is not None and run_row.epoch != row.epoch
            terminal = not allow_terminal and (run_row is None or run_row.status in {
                RunStatus.SUCCEEDED.value, RunStatus.CANCELLED.value,
                RunStatus.FAILED.value, RunStatus.INTERRUPTED.value,
            })
            cancelled = run_row is not None and (
                run_row.cancel_requested or run_row.status == RunStatus.CANCELLING.value
            )
            if error is None and (stale_epoch or terminal or cancelled):
                # A late result can never reopen a terminal run or cross an epoch.
                # It stays reviewable evidence and the attempt is not a success.
                row.status = "INTERRUPTED" if (stale_epoch or terminal) else "CANCELLED"
                await self._mark_step_cancelled(session, row.step_run_id)
                session.add(
                    ExecutionEvent(
                        id=new_id(),
                        run_id=run.id,
                        draft_id=None,
                        sequence=await self._next_sequence(session, run.id),
                        step_run_id=row.step_run_id,
                        attempt_id=row.id,
                        workflow_version_id=run.workflow_version_id,
                        occurred_at=self._deps.clock.now(),
                        kind=EventKind.LATE_RESULT_QUARANTINED.value,
                        status=row.status,
                        reason_code=(
                            "STALE_EPOCH" if stale_epoch else "TERMINAL" if terminal
                            else "CANCEL_REQUESTED"
                        ),
                        payload_json={"attempt_id": row.id, "output_hash": row.output_hash},
                        payload_ref=None,
                        schema_version=1,
                        provider_event_key=None,
                    )
                )
            if on is not None and row.status == "SUCCEEDED":
                # The result, its step completion, the selected transition and
                # the review event share this one transaction: a crash can never
                # persist a success without the edge that follows it.
                step_row = await session.get(StepRun, row.step_run_id)
                if step_row is not None:
                    if step_row.status in {
                        StepStatus.PENDING.value, StepStatus.RUNNING.value,
                    }:
                        step_row.status = StepStatus.SUCCEEDED.value
                    step_row.selected_transition = on
                    step_row.revision += 1
                    step_row.updated_at = self._deps.clock.now()
            await session.commit()
            return row.status == "SUCCEEDED"

    async def _link_session(
        self, run: WorkflowRun, attempt: StepAttempt, session: SessionHandle,
        capabilities: RuntimeCapabilities,
    ) -> None:
        """Persist the local association before the task is submitted."""
        from personal_agent.adapters.persistence.session_models import SessionLink

        operation_id = f"{attempt.id}:session"
        async with self._deps.database.session() as db:
            link = await db.scalar(
                select(SessionLink).where(SessionLink.operation_id == operation_id)
            )
            if link is None:
                link = SessionLink(
                    id=new_id(),
                    operation_id=operation_id,
                    run_id=run.id,
                    attempt_id=attempt.id,
                    draft_id=None,
                    adapter_session_id=session.adapter_session_id,
                    provider_session_id=session.provider_session_id,
                    runtime_id=session.runtime_id,
                    provider="harness-cli",
                    sdk_version=capabilities.sdk_version,
                    runtime_version=capabilities.runtime_version,
                    profile_hash=capabilities.profile_hash or self._deps.profile_hash,
                    capability_hash=stable_hash(self._deps.capability_token),
                    store_ref=None,
                    status="READY",
                    revision=1,
                    created_at=self._deps.clock.now(),
                )
                db.add(link)
            else:
                link.status = "READY"
                link.adapter_session_id = session.adapter_session_id
                link.revision += 1
            attempt_row = await db.get(StepAttempt, attempt.id)
            if attempt_row is not None:
                attempt_row.session_link_id = link.id
            await db.commit()

    async def _set_session_status(self, attempt_id: str, status: str) -> None:
        from personal_agent.adapters.persistence.session_models import SessionLink

        async with self._deps.database.session() as db:
            link = await db.scalar(
                select(SessionLink).where(SessionLink.operation_id == f"{attempt_id}:session")
            )
            if link is None or link.status == status:
                return
            link.status = status
            link.revision += 1
            await db.commit()

    async def _mark_step_cancelled(self, session: Any, step_run_id: str) -> None:
        step_run = await session.get(StepRun, step_run_id)
        if step_run is not None:
            step_run.status = StepStatus.CANCELLED.value
            step_run.revision += 1
            step_run.updated_at = self._deps.clock.now()

    async def _set_step_status(
        self, run: WorkflowRun, step: Step, status: StepStatus, *,
        reason: str | None = None, on: str | None = None,
    ) -> None:
        async with self._deps.database.session() as session:
            step_run = await session.scalar(
                select(StepRun).where(StepRun.run_id == run.id, StepRun.step_id == step.id)
            )
            if step_run is None:
                return
            step_run.status = status.value
            step_run.skip_reason = reason
            if on is not None:
                step_run.selected_transition = on
            step_run.revision += 1
            step_run.updated_at = self._deps.clock.now()
            await session.commit()

    async def _complete_step(self, run: WorkflowRun, step: Step, on: str) -> None:
        """Record a finished step and the transition it selected.

        The transition is persisted so history is reviewable and a resumed run can
        continue from exactly the same edge instead of guessing.
        """
        async with self._deps.database.session() as session:
            step_run = await session.scalar(
                select(StepRun).where(StepRun.run_id == run.id, StepRun.step_id == step.id)
            )
            if step_run is None:
                return
            # The exhausted path deliberately keeps the step FAILED while following
            # the declared edge; only a genuinely finished step becomes SUCCEEDED.
            if step_run.status in {StepStatus.PENDING.value, StepStatus.RUNNING.value}:
                step_run.status = StepStatus.SUCCEEDED.value
            step_run.selected_transition = on
            step_run.revision += 1
            step_run.updated_at = self._deps.clock.now()
            await session.commit()

    async def _stop_check(self, run_id: str) -> str | None:
        """Converge a stop request before starting or continuing another step."""
        control = self._deps.control
        if control is None:
            return None
        action = await control.observe(run_id)
        if action is None:
            return None
        if self._deps.stop_signal is not None:
            self._deps.stop_signal.stopping = True
        return str(await control.converge_from_engine(run_id))

    async def _has_exhausted_edge(self, run: WorkflowRun) -> bool:
        async with self._deps.database.session() as session:
            return bool(await session.scalar(
                select(StepRun.id).where(
                    StepRun.run_id == run.id,
                    StepRun.selected_transition == "exhausted",
                ).limit(1)
            ))

    async def _resume_point(
        self, run: WorkflowRun, definition: WorkflowDefinition
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Rebuild the outputs of already-succeeded steps and find the next one.

        Succeeded steps are never re-executed; the traversal follows the recorded
        transitions from the entry step. Steps whose transition was never recorded
        fall back to the only available edge, otherwise the step is simply next.
        """
        async with self._deps.database.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(StepRun).where(StepRun.run_id == run.id)
                    )
                ).all()
            )
            attempts = list(
                (
                    await session.scalars(
                        select(StepAttempt)
                        .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                        .where(StepRun.run_id == run.id)
                        .order_by(StepAttempt.attempt_no)
                    )
                ).all()
            )
        by_step = {row.step_id: row for row in rows}
        outputs: dict[str, dict[str, Any]] = {}
        for attempt in attempts:
            row = next((item for item in rows if item.id == attempt.step_run_id), None)
            if row is None or row.status != StepStatus.SUCCEEDED.value:
                continue
            if attempt.status == "SUCCEEDED":
                # Internal reuse facts are restored separately, never mixed into
                # the business output a Transform or Output step consumes.
                outputs[row.step_id] = {
                    key: value for key, value in dict(attempt.output_json or {}).items()
                    if not key.startswith("__")
                }
        if run.parent_run_id is not None:
            # A branch reuses the validated results of its ancestors. They are
            # evidence carried over, not new attempts of this run.
            outputs = await self._inherited_outputs(run, outputs)
        current: str | None = run.fork_step_id or definition.entry_step
        steps = {step.id: step for step in definition.steps}
        guard = 0
        while current is not None:
            guard += 1
            if guard > len(definition.steps) * 4 + 8:
                return outputs, current
            row = by_step.get(current)
            if row is None or (
                row.status != StepStatus.SUCCEEDED.value
                and not (
                    row.status == StepStatus.FAILED.value and row.selected_transition == "exhausted"
                )
            ):
                return outputs, current
            on = row.selected_transition or "success"
            edge = self._pick_transition(definition, steps[current], on)
            if edge is None:
                return outputs, None
            current = edge.to
        return outputs, None

    async def _restore_reuse(self, run: WorkflowRun) -> None:
        """Restore the evidence and sources carried by the latest reused attempt.

        A resumed run must be able to deliver the same document without redoing
        research, so the evidence summary and every collected source are read
        back from the attempt that produced them.
        """
        async with self._deps.database.session() as session:
            attempts = list((await session.scalars(
                select(StepAttempt)
                .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                .where(StepRun.run_id == run.id, StepAttempt.status == "SUCCEEDED")
                .order_by(StepAttempt.ended_at.desc())
            )).all())
        for attempt in attempts:
            data = dict(attempt.output_json or {})
            evidence = data.get("__evidence__")
            if not self._evidence_summary and isinstance(evidence, dict) and evidence:
                self._evidence_summary = dict(evidence)
            sources = data.get("__sources__")
            if not self._sources and isinstance(sources, list):
                self._sources = [dict(item) for item in sources if isinstance(item, dict)]
            if self._evidence_summary and self._sources:
                return

    async def _inherited_outputs(
        self, run: WorkflowRun, outputs: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Fill in outputs of steps reused from the parent chain."""
        async with self._deps.database.session() as session:
            reused = set((await session.scalars(
                select(StepRun.step_id).where(
                    StepRun.run_id == run.id, StepRun.status == "SUCCEEDED",
                    StepRun.skip_reason == "reused from parent",
                )
            )).all())
            parent_id = run.parent_run_id
            seen: set[str] = set()
            while parent_id is not None and parent_id not in seen:
                seen.add(parent_id)
                parent = await session.get(WorkflowRun, parent_id)
                if parent is None:
                    break
                rows = (
                    await session.scalars(
                        select(StepRun).where(StepRun.run_id == parent.id)
                    )
                ).all()
                attempts = (
                    await session.scalars(
                        select(StepAttempt)
                        .join(StepRun, StepAttempt.step_run_id == StepRun.id)
                        .where(StepRun.run_id == parent.id)
                        .order_by(StepAttempt.attempt_no)
                    )
                ).all()
                by_id = {row.id: row for row in rows}
                for attempt in attempts:
                    row = by_id.get(attempt.step_run_id)
                    if row is None or row.status != StepStatus.SUCCEEDED.value:
                        continue
                    if (
                        attempt.status != "SUCCEEDED" or row.step_id in outputs
                        or row.step_id not in reused
                    ):
                        continue
                    data = dict(attempt.output_json or {})
                    # Reused work carries the evidence/sources the new branch
                    # needs; they are restored here and kept out of the public
                    # output consumed by Transform and Output steps.
                    evidence = data.get("__evidence__")
                    if isinstance(evidence, dict) and evidence:
                        self._evidence_summary = dict(evidence)
                    sources = data.get("__sources__")
                    if isinstance(sources, list) and sources:
                        self._sources = [dict(item) for item in sources if isinstance(item, dict)]
                    outputs[row.step_id] = {
                        key: value for key, value in data.items() if not key.startswith("__")
                    }
                parent_id = parent.parent_run_id
        return outputs

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
                    completed_steps=[],
                    remaining_steps=[step.id],
                    observed_problem=config.prompt,
                    unknowns=[],
                    attempted_remedies=[],
                    decision_schema_json=dict(config.decision_schema or {}),
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

    async def _review_already_approved(self, run: WorkflowRun, step: Step) -> bool:
        """True when this exact step has a resolved, explicitly-continued review."""
        from personal_agent.adapters.persistence.assistance_models import (
            AssistanceRequest,
            UserDecision,
        )

        async with self._deps.database.session() as session:
            requests = (
                await session.scalars(
                    select(AssistanceRequest)
                    .where(
                        AssistanceRequest.run_id == run.id,
                        AssistanceRequest.kind == AssistanceKind.HUMAN_REVIEW.value,
                        AssistanceRequest.status == "RESOLVED",
                    )
                    .order_by(AssistanceRequest.created_at.desc())
                )
            ).all()
            for request in requests:
                if step.id not in list(request.remaining_steps or []):
                    continue
                decision = await session.scalar(
                    select(UserDecision)
                    .where(
                        UserDecision.assistance_id == request.id,
                        UserDecision.continue_authorized.is_(True),
                    )
                    .order_by(UserDecision.created_at.desc())
                )
                if decision is not None:
                    return True
        return False

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
                    unknowns=[getattr(getattr(result, "error", None), "code", "UNKNOWN")],
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
        async with event_session(self._deps.database) as session:
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
                version = await session.get(WorkflowVersion, run.workflow_version_id)
                if version is None:
                    raise AppError(
                        code="VERSION_NOT_FOUND", message="workflow version not found", status=404
                    )
                limits = effective_run_limits(
                    self._deps.policy.budget.as_dict(),
                    version.definition_json.get("execution_policy") or {},
                )
                ledger = await self._deps.budget.ensure_run_ledger(
                    session, root_run_id=run.root_run_id, limits=limits
                )
                await session.commit()
            return ledger

    async def _charge_active(self, run: WorkflowRun, since_ms: int) -> int:
        tick = self._deps.clock.monotonic_ms()
        elapsed = max(0, tick - since_ms)
        if elapsed:
            async with self._deps.database.session() as session:
                ledger = await session.scalar(select(BudgetLedger).where(
                    BudgetLedger.root_run_id == run.root_run_id))
                row = await session.get(WorkflowRun, run.id)
                if ledger is None or row is None:
                    raise AppError(
                        code="BUDGET_LEDGER_MISSING", message="run ledger is missing", status=503
                    )
                await self._deps.budget.record_active_time(
                    session, ledger=ledger, elapsed_ms=elapsed,
                    operation_id=f"{run.id}:{run.epoch}:active:{since_ms}:{tick}",
                )
                row.active_elapsed_ms += elapsed
                await session.commit()
        return tick

    async def _require_budget(self, run: WorkflowRun) -> None:
        ledger = await self._ledger(run)
        domain = self._deps.budget.to_domain(ledger)
        if domain.exhausted:
            raise AppError(
                code="BUDGET_ASSISTANCE_REQUIRED", message="研究额度已到限，需要用户决定后续处置",
                category=ErrorCategory.BUDGET, status=409,
                details={"exhausted_dimensions": domain.exhausted_dimensions()},
            )

    async def _request_budget_assistance(
        self, run: WorkflowRun, definition: WorkflowDefinition, step_id: str | None, error: AppError
    ) -> None:
        from personal_agent.adapters.persistence.assistance_models import AssistanceRequest

        async with event_session(self._deps.database) as session:
            row = await session.get(WorkflowRun, run.id)
            if row is None or row.status != RunStatus.RUNNING.value or row.epoch != run.epoch:
                return
            ledger = await session.scalar(select(BudgetLedger).where(
                BudgetLedger.root_run_id == run.root_run_id))
            assert ledger is not None
            completed = list((await session.scalars(select(StepRun.step_id).where(
                StepRun.run_id == run.id, StepRun.status == StepStatus.SUCCEEDED.value
            ))).all())
            request = AssistanceRequest(
                id=new_id(), run_id=run.id, kind=AssistanceKind.BUDGET_LIMIT.value,
                limit_snapshot=dict(ledger.limits_json),
                consumed_snapshot=dict(ledger.consumed_json),
                completed_steps=completed,
                remaining_steps=[step.id for step in definition.steps if step.id not in completed],
                observed_problem=error.message,
                unknowns=[
                    f"{key} 仍有未核验用量 {value}"
                    for key, value in ledger.reserved_json.items() if value
                ],
                attempted_remedies=["已停止新增行动；保留累计消耗与已完成成果，等待明确追加及继续授权"],
                decision_schema_json={
                    "required_budget": (error.details or {}).get("requested", {})
                },
                status="OPEN", revision=1, created_at=self._deps.clock.now(),
            )
            session.add(request)
            row.status = RunStatus.WAITING_USER.value
            row.reason_code = "BUDGET_LIMIT"
            row.current_step_id = step_id
            row.revision += 1
            row.updated_at = self._deps.clock.now()
            session.add(ExecutionEvent(
                id=new_id(), run_id=run.id, draft_id=None,
                sequence=await self._next_sequence(session, run.id), step_run_id=None,
                attempt_id=None, workflow_version_id=run.workflow_version_id,
                occurred_at=self._deps.clock.now(), kind=EventKind.ASSISTANCE_REQUIRED.value,
                status=row.status, reason_code="BUDGET_LIMIT",
                payload_json={"assistance_id": request.id, "step_id": step_id},
                payload_ref=None, schema_version=1, provider_event_key=None,
            ))
            await session.commit()

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



