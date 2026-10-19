"""Deterministic Mock runtime and Mock model transport.

The mock implements the *same* ports as the real adapter and replays fixed
observations. It never opens a network connection, never reads credentials and
never pretends that a queued submission is a completed business result. Scenarios
cover accepted-only, completed, failed, unknown, duplicate events, late results,
missing sessions and incompatibility so the frozen contract tests can be written
before the real adapter is allowed to run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

from personal_agent.adapters.harness.events import normalize_event
from personal_agent.domain.common import AppError, ErrorCategory, new_id, stable_hash
from personal_agent.ports.contracts import (
    Clock,
    ModelResponse,
    Owner,
    ReconcileResult,
    RestoredSession,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeTask,
    SessionContext,
    SessionHandle,
    StopResult,
    Submission,
    Usage,
    WaitResult,
)
from personal_agent.ports.contracts import (
    ModelRequest as ModelRequestSpec,
)

PROFILE_HASH = stable_hash({"profile": "personal-agent-restricted", "version": 1})


class MockRuntime:
    """In-memory runtime port. One instance per test or per app in mock mode."""

    def __init__(
        self,
        observations: Mapping[str, Any],
        clock: Clock,
        *,
        default_scenario: str = "engine.topic.directions",
    ) -> None:
        self._observations = observations
        self._clock = clock
        self._default_scenario = default_scenario
        self._sessions: dict[str, SessionHandle] = {}
        self._session_by_operation: dict[str, str] = {}
        self._scenario_by_submission: dict[str, str] = {}
        self._results: dict[str, WaitResult] = {}
        self._session_events: dict[str, list[RuntimeEvent]] = {}
        self._sequence = 0
        self._stopped: set[str] = set()
        self._disposed: set[str] = set()
        self.session_creation_count = 0

    # -- capabilities ---------------------------------------------------
    async def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            adapter_version="mock-1",
            sdk_version="0.0.0-mock",
            runtime_version="mock",
            platform="windows-x64",
            profile_hash=PROFILE_HASH,
            supports_resume=True,
            supports_cooperative_stop=True,
            remote_cancel_verifiable=False,
            tool_policy_enforced=True,
            model_gateway_enforced=True,
            persistence_mode="none-no-reasoning",
        )

    # -- sessions -------------------------------------------------------
    async def create_session(self, context: SessionContext) -> SessionHandle:
        if context.capability_token != "mock-capability-token":
            raise AppError(
                code="CAPABILITY_TOKEN_INVALID",
                message="the capability token is not valid for this runtime",
                category=ErrorCategory.PERMISSION,
                status=403,
            )
        existing_id = self._session_by_operation.get(context.operation_id)
        if existing_id is not None:
            return self._sessions[existing_id]
        session = SessionHandle(
            adapter_session_id=f"mock-session-{new_id()}",
            provider_session_id=f"mock-provider-{new_id()}",
            runtime_id=f"mock-runtime-{context.owner.run_id or context.owner.draft_id}",
        )
        self._sessions[session.adapter_session_id] = session
        self._session_by_operation[context.operation_id] = session.adapter_session_id
        self._session_events[session.adapter_session_id] = []
        self.session_creation_count += 1
        self._emit(session.adapter_session_id, "session.started", context.owner, context.epoch)
        return session

    # -- submissions ----------------------------------------------------
    async def submit(self, task: RuntimeTask) -> Submission:
        session = self._sessions.get(task.session_id)
        if session is None:
            raise AppError(
                code="SESSION_MISSING",
                message="the runtime session is not known; a new session would be a new attempt",
                category=ErrorCategory.INTERRUPTED,
                status=409,
            )
        if task.session_id in self._stopped:
            raise AppError(
                code="RUN_STOPPED",
                message="the runtime no longer accepts tasks for this session",
                category=ErrorCategory.INTERRUPTED,
                status=409,
            )
        scenario = str(task.validated_input.get("mock_scenario") or self._default_scenario)
        observation = self._scenario(scenario)
        submission_id = new_id()
        self._scenario_by_submission[submission_id] = scenario
        self._emit(
            task.session_id,
            "action.proposed",
            self._owner_for(task),
            task.epoch,
            payload={
                "direction_ids": list(task.direction_ids),
                "goal_digest": stable_hash(task.goal),
            },
        )
        self._emit(
            task.session_id,
            "result.available",
            self._owner_for(task),
            task.epoch,
            submission_id=submission_id,
            payload={"status": str(observation.get("status", "completed"))},
        )

        usage = self._usage(observation.get("usage"))
        status = str(observation.get("status", "completed"))
        error_payload = observation.get("error")
        error = None
        if error_payload:
            error = AppError(
                code=str(error_payload.get("code", "MOCK_ERROR")),
                message=str(error_payload.get("message", "mock failure")),
                category=ErrorCategory(str(error_payload.get("category", "DEPENDENCY"))),
                retryable=bool(error_payload.get("retryable", False)),
            )
        self._results[submission_id] = WaitResult(
            status=status,
            structured_output=observation.get("output"),
            usage=usage,
            completion_evidence_ref=(
                f"mock-evidence:{submission_id}" if status == "completed" else None
            ),
            error=error,
        )
        return Submission(submission_id=submission_id, status="accepted")

    def _owner_for(self, task: RuntimeTask) -> Owner:
        run_id = task.validated_input.get("run_id")
        attempt_id = task.validated_input.get("attempt_id")
        draft_id = task.validated_input.get("draft_id")
        if draft_id and not run_id:
            return Owner(draft_id=str(draft_id))
        return Owner(run_id=str(run_id), attempt_id=str(attempt_id) if attempt_id else None)

    # -- results --------------------------------------------------------
    async def wait_result(self, submission_id: str) -> WaitResult:
        result = self._results.get(submission_id)
        if result is None:
            raise AppError(
                code="SUBMISSION_NOT_FOUND",
                message="unknown submission id",
                status=404,
            )
        return result

    async def events(
        self, session_id: str, cursor: int | None = None
    ) -> AsyncIterator[RuntimeEvent]:
        events = self._session_events.get(session_id)
        if events is None:
            raise AppError(
                code="SESSION_MISSING",
                message="unknown runtime session",
                category=ErrorCategory.INTERRUPTED,
                status=404,
            )
        after = cursor or 0
        for event in list(events):
            if event.sequence > after:
                yield event
        # Duplicate delivery is a real runtime behaviour; tests must dedupe by key.
        for event in list(events):
            if event.sequence > after:
                yield event

    async def request_stop(self, session_id: str, reason: str) -> StopResult:
        if session_id not in self._sessions:
            raise AppError(
                code="SESSION_MISSING",
                message="unknown runtime session",
                category=ErrorCategory.INTERRUPTED,
                status=404,
            )
        self._stopped.add(session_id)
        if reason == "cancel-unknown":
            return StopResult(status="unknown", in_flight_request_ids=[session_id])
        self._emit(
            session_id,
            "runtime.stopped",
            Owner(run_id=self._sessions[session_id].runtime_id),
            None,
            payload={"reason": reason},
        )
        return StopResult(status="requested", in_flight_request_ids=[])

    async def reconcile(self, session_id: str) -> ReconcileResult:
        if session_id not in self._sessions:
            raise AppError(
                code="SESSION_MISSING",
                message="unknown runtime session",
                status=404,
            )
        return ReconcileResult(
            local_runtime_status="stopped" if session_id in self._stopped else "running",
            model_requests={},
            tool_operations={},
            completion_evidence={},
        )

    async def restore_session(
        self, link: SessionHandle, checkpoint: Mapping[str, Any]
    ) -> RestoredSession:
        if link.adapter_session_id not in self._sessions:
            raise AppError(
                code="SESSION_MISSING",
                message=(
                    "the original session is missing; create a new attempt instead of "
                    "claiming the old session was restored"
                ),
                category=ErrorCategory.INTERRUPTED,
                status=409,
            )
        return RestoredSession(
            session=self._sessions[link.adapter_session_id],
            restored_original=True,
            checkpoint_hash=stable_hash(dict(checkpoint)),
        )

    async def dispose(self, runtime_id: str) -> None:
        self._disposed.add(runtime_id)

    # -- helpers --------------------------------------------------------
    def scenario_names(self) -> list[str]:
        return sorted(self._observations["model"]["scenarios"])

    def _scenario(self, name: str) -> dict[str, Any]:
        try:
            return dict(self._observations["model"]["scenarios"][name])
        except KeyError as error:
            raise AppError(
                code="MOCK_SCENARIO_MISSING",
                message=f"no observation for scenario {name!r}",
                status=500,
            ) from error

    @staticmethod
    def _usage(raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        return Usage(
            input_tokens=int(raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            model_requests=1,
        )

    def _emit(
        self,
        session_id: str,
        kind: str,
        owner: Owner,
        epoch: int | None,
        *,
        submission_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._sequence += 1
        event = normalize_event(
            raw={"kind": kind, "payload": payload or {}},
            owner=owner,
            session_id=session_id,
            sequence=self._sequence,
            occurred_at=self._clock.now(),
            submission_id=submission_id,
            epoch=epoch,
        )
        if event is not None:
            self._session_events.setdefault(session_id, []).append(event)

    def _scenario_from_input(self, payload: Mapping[str, Any]) -> str:
        return str(payload.get("mock_scenario") or self._default_scenario)


class MockModelTransport:
    """Model transport replaying a named observation; never contacts a provider."""

    def __init__(self, observations: Mapping[str, Any], *, latency_seconds: float = 0.0) -> None:
        self._observations = observations
        self._latency = latency_seconds
        self.calls: list[ModelRequestSpec] = []

    async def complete(self, spec: ModelRequestSpec) -> ModelResponse:
        self.calls.append(spec)
        scenario = "requirement.analysis.complete"
        hints = self._observations.get("model", {}).get("scenario_hints", {})
        for message in spec.messages:
            if message.content.startswith("scenario:"):
                scenario = message.content.split(":", 1)[1].strip()
                break
            for hint, hinted_scenario in hints.items():
                if hint in message.content:
                    scenario = hinted_scenario
                    break
        observation = dict(self._observations["model"]["scenarios"].get(scenario, {}))
        if self._latency:
            await asyncio.sleep(self._latency)
        status = str(observation.get("status", "completed"))
        if status != "completed":
            return ModelResponse(
                status=status,
                error=AppError(
                    code="MODEL_UNKNOWN",
                    message="the mock transport reported an unknown outcome",
                    category=ErrorCategory.UNKNOWN_OUTCOME,
                ),
            )
        import json

        return ModelResponse(
            status="completed",
            content=json.dumps(observation.get("output") or {}, ensure_ascii=False),
            usage=Usage(
                input_tokens=int((observation.get("usage") or {}).get("input_tokens") or 0),
                output_tokens=int((observation.get("usage") or {}).get("output_tokens") or 0),
                model_requests=1,
            ),
            completion_evidence_ref=f"mock-model-evidence:{spec.request_id}",
        )
