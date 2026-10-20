"""Headless CLI to business RuntimePort mapping, without an Agent Loop.

The production wrapper separately requires current adoption and gateway/tool
bindings. This adapter's injectable runner permits offline contract testing; its
capabilities deliberately do not claim those unverified enforcement boundaries.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict
from typing import Any, Protocol

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from personal_agent.adapters.harness.cli import CLI_VERSION, CliResult
from personal_agent.adapters.harness.events import normalize_event
from personal_agent.adapters.harness.model_proxy import ProxyTokenRegistry
from personal_agent.adapters.tools.scope import ToolScopeRegistry
from personal_agent.domain.common import AppError, ErrorCategory, new_id, stable_hash
from personal_agent.ports.contracts import (
    Clock,
    ReconcileResult,
    RestoredSession,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeTask,
    SessionContext,
    SessionHandle,
    StopResult,
    Submission,
    WaitResult,
)


class CliRunner(Protocol):
    async def run(
        self, prompt: str, *, timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> CliResult: ...


def _error(code: str, message: str, category: ErrorCategory) -> AppError:
    return AppError(code=code, message=message, category=category, status=409)


class CliRuntime:
    def __init__(
        self,
        runner: CliRunner,
        clock: Clock,
        *,
        profile_hash: str,
        tokens: ProxyTokenRegistry | None = None,
        gateway_base_url: str | None = None,
        profile_path: str | None = None,
        scopes: ToolScopeRegistry | None = None,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._profile_hash = profile_hash
        self._tokens = tokens
        self._gateway_base_url = gateway_base_url
        self._profile_path = profile_path
        self._scopes = scopes
        self._sessions: dict[str, SessionHandle] = {}
        self._contexts: dict[str, SessionContext] = {}
        self._session_operations: dict[str, tuple[str, SessionHandle]] = {}
        self._submissions: dict[str, tuple[str, Submission]] = {}
        self._workers: dict[str, asyncio.Task[WaitResult]] = {}
        self._session_submission: dict[str, str] = {}
        self._session_tokens: dict[str, str] = {}
        self._session_scope_tokens: dict[str, list[str]] = {}
        self._events: dict[str, list[RuntimeEvent]] = {}
        self._stopped: set[str] = set()

    async def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            "harness-cli-1", "not-used", CLI_VERSION, "windows-x64", self._profile_hash,
            False, True, False, False, False, "none-no-reasoning",
        )

    async def create_session(self, context: SessionContext) -> SessionHandle:
        fingerprint = stable_hash(asdict(context))
        previous = self._session_operations.get(context.operation_id)
        if previous:
            if previous[0] != fingerprint:
                raise _error("IDEMPOTENCY_CONFLICT", "the session context changed",
                             ErrorCategory.INTEGRITY)
            return previous[1]
        if context.profile_hash != self._profile_hash or context.epoch < 1:
            raise _error("RUNTIME_INCOMPATIBLE", "the session profile or epoch is incompatible",
                         ErrorCategory.PERMISSION)
        session = SessionHandle(new_id(), new_id())
        self._sessions[session.adapter_session_id] = session
        self._contexts[session.adapter_session_id] = context
        self._session_operations[context.operation_id] = (fingerprint, session)
        self._events[session.adapter_session_id] = []
        self._emit(session.adapter_session_id, "session.started", {})
        return session

    def _context(self, session_id: str) -> SessionContext:
        if session_id not in self._contexts:
            raise _error("SESSION_MISSING", "the original session is unavailable",
                         ErrorCategory.INTERRUPTED)
        return self._contexts[session_id]

    async def submit(self, task: RuntimeTask) -> Submission:
        context = self._context(task.session_id)
        if task.session_id in self._stopped:
            raise _error("RUN_STOPPED", "this session no longer accepts actions",
                         ErrorCategory.INTERRUPTED)
        if context.epoch != task.epoch:
            raise _error("EPOCH_MISMATCH", "the task epoch differs from its session",
                         ErrorCategory.PERMISSION)
        remaining = (task.deadline - self._clock.now()).total_seconds()
        if remaining <= 0 or task.model_timeout_seconds <= 0:
            raise _error("RUNTIME_DEADLINE", "the task deadline has expired", ErrorCategory.TIMEOUT)
        fingerprint = stable_hash({**asdict(task), "deadline": task.deadline.isoformat()})
        previous = self._submissions.get(task.operation_id)
        if previous:
            if previous[0] != fingerprint:
                raise _error("IDEMPOTENCY_CONFLICT", "the submitted task changed",
                             ErrorCategory.INTEGRITY)
            return previous[1]
        if task.session_id in self._session_submission:
            raise _error("SESSION_OCCUPIED", "a one-shot session already has a submission",
                         ErrorCategory.INTERRUPTED)
        # Reject schema errors and remote references before any subprocess/model action.
        try:
            Draft202012Validator.check_schema(task.output_schema)
            self._local_schema(task.output_schema)
        except (SchemaError, ValueError):
            raise _error("RUNTIME_SCHEMA_INVALID", "the output contract is not a local schema",
                         ErrorCategory.VALIDATION) from None
        submission = Submission(new_id(), "accepted")
        self._submissions[task.operation_id] = fingerprint, submission
        self._session_submission[task.session_id] = submission.submission_id
        prompt = json.dumps({
            "goal": task.goal, "validated_input": dict(task.validated_input),
            "context_refs": list(task.context_refs), "direction_ids": list(task.direction_ids),
            "output_schema": dict(task.output_schema),
            "response_format": "Return one JSON object satisfying output_schema.",
        }, ensure_ascii=False)
        environment = self._bind_gateway(context, task)
        self._workers[submission.submission_id] = asyncio.create_task(
            self._execute(
                task.session_id, prompt, min(remaining, task.model_timeout_seconds),
                environment,
            ),
        )
        return submission

    def _bind_gateway(
        self, context: SessionContext, task: RuntimeTask,
    ) -> dict[str, str] | None:
        """Issue one attempt token plus per-tool scope tokens for this attempt.

        The CLI and the MCP child only receive the loopback endpoint and this
        token; supplier credentials and scope tokens stay in the gateway process.
        """
        if self._tokens is None or not self._gateway_base_url:
            return None
        tool_scopes: dict[str, str] = {}
        if self._scopes is not None and context.owner.run_id and context.owner.attempt_id:
            for tool_name in task.tool_scope:
                tool_scopes[str(tool_name)] = self._scopes.issue(
                    run_id=context.owner.run_id,
                    attempt_id=context.owner.attempt_id,
                    epoch=context.epoch,
                    tool_name=str(tool_name),
                    allowed_domains=task.allowed_domains,
                    now=self._clock.now(),
                )
        binding = self._tokens.issue(
            owner=context.owner,
            epoch=context.epoch,
            model_id=context.model_id,
            reservation_id=task.budget_reservation_id,
            reserved_tokens=max(task.model_reserved_tokens, task.model_output_limit),
            output_limit=task.model_output_limit,
            timeout_seconds=task.model_timeout_seconds,
            tool_scopes=tool_scopes,
            root_run_id=task.budget_root_run_id,
        )
        self._session_tokens[task.session_id] = binding.token
        self._session_scope_tokens[task.session_id] = list(tool_scopes.values())
        environment = {
            "PERSONAL_AGENT_GATEWAY_BASE_URL": self._gateway_base_url,
            "PERSONAL_AGENT_GATEWAY_TOKEN": binding.token,
            # The guard binds its node scope to this attempt's epoch; the real
            # scope token stays in the gateway's per-tool grants.
            "PERSONAL_AGENT_SCOPE_EPOCH": str(context.epoch),
        }
        if self._profile_path:
            environment["PERSONAL_AGENT_PROFILE_PATH"] = self._profile_path
        return environment

    def _revoke(self, session_id: str) -> None:
        token = self._session_tokens.pop(session_id, None)
        if token is not None and self._tokens is not None:
            self._tokens.revoke(token)
        for scope_token in self._session_scope_tokens.pop(session_id, []):
            if self._scopes is not None:
                self._scopes.revoke(scope_token)

    @staticmethod
    def _local_schema(value: Any) -> None:
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if reference is not None and (not isinstance(reference, str)
                                          or not reference.startswith("#")):
                raise ValueError("remote schema")
            for child in value.values():
                CliRuntime._local_schema(child)
        elif isinstance(value, list):
            for child in value:
                CliRuntime._local_schema(child)

    async def _execute(
        self, session_id: str, prompt: str, duration: float,
        environment: Mapping[str, str] | None = None,
    ) -> WaitResult:
        try:
            result = await self._runner.run(
                prompt, timeout_seconds=duration, environment=environment,
            )
            output: dict[str, Any] | None = None
            error = None
            status = result.status
            if status == "completed":
                try:
                    candidate = json.loads(result.text or "")
                    if not isinstance(candidate, dict):
                        raise ValueError("output must be an object")
                    schema = json.loads(prompt)["output_schema"]
                    Draft202012Validator(schema).validate(candidate)
                    output = candidate
                except (ValueError, ValidationError, RecursionError):
                    status = "failed"
                    error = _error("RUNTIME_OUTPUT_INVALID", "the CLI output failed its contract",
                                   ErrorCategory.VALIDATION)
            if session_id in self._stopped:
                output = None
                if result.completion_evidence_ref:
                    status = "failed"
                    error = _error("RUNTIME_RESULT_QUARANTINED", "a stopped session returned late",
                                   ErrorCategory.INTERRUPTED)
                else:
                    status = "unknown"
            if status == "unknown":
                error = _error("MODEL_OUTCOME_UNKNOWN", "the CLI cannot prove remote completion",
                               ErrorCategory.UNKNOWN_OUTCOME)
            if status == "failed" and error is None:
                error = _error("CLI_FAILED", "the CLI did not complete successfully",
                               ErrorCategory.DEPENDENCY)
            mapped = WaitResult(status, output, result.usage,
                                completion_evidence_ref=result.completion_evidence_ref, error=error)
        except asyncio.CancelledError:
            mapped = WaitResult("unknown", error=_error(
                "MODEL_OUTCOME_UNKNOWN", "local disposal cannot prove remote completion",
                ErrorCategory.UNKNOWN_OUTCOME,
            ))
        except Exception:
            mapped = WaitResult("unknown", error=_error(
                "MODEL_OUTCOME_UNKNOWN", "the CLI transport did not provide completion evidence",
                ErrorCategory.UNKNOWN_OUTCOME,
            ))
        self._emit(session_id, "result.available", {"status": mapped.status})
        # The attempt token dies with the attempt: a late or leaked value cannot
        # start another model or tool action.
        self._revoke(session_id)
        return mapped

    def _emit(self, session_id: str, kind: str, payload: dict[str, Any]) -> None:
        context = self._contexts[session_id]
        event = normalize_event(
            raw={"kind": kind, "payload": payload}, owner=context.owner,
            session_id=session_id, sequence=len(self._events[session_id]) + 1,
            occurred_at=self._clock.now(), attempt_id=context.owner.attempt_id,
            epoch=context.epoch, submission_id=self._session_submission.get(session_id),
        )
        if event:
            self._events[session_id].append(event)

    async def events(
        self, session_id: str, cursor: int | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        self._context(session_id)
        for event in self._events[session_id]:
            if event.sequence > (cursor or 0):
                yield event

    async def wait_result(self, submission_id: str) -> WaitResult:
        worker = self._workers.get(submission_id)
        if worker is None:
            raise _error("SUBMISSION_MISSING", "the submission is unavailable",
                         ErrorCategory.INTERRUPTED)
        if worker.cancelled():
            return self._disposed_result()
        return await asyncio.shield(worker)

    @staticmethod
    def _disposed_result() -> WaitResult:
        return WaitResult("unknown", error=_error(
            "MODEL_OUTCOME_UNKNOWN", "local disposal has no remote completion evidence",
            ErrorCategory.UNKNOWN_OUTCOME,
        ))

    async def request_stop(self, session_id: str, reason: str) -> StopResult:
        self._context(session_id)
        self._stopped.add(session_id)
        # No new model or tool action may start after a stop request.
        self._revoke(session_id)
        submission = self._session_submission.get(session_id)
        worker = self._workers.get(submission or "")
        return StopResult("requested" if worker and not worker.done() else "drained",
                          (submission,) if submission and worker and not worker.done() else ())

    async def reconcile(self, session_id: str) -> ReconcileResult:
        self._context(session_id)
        submission = self._session_submission.get(session_id)
        worker = self._workers.get(submission or "")
        if worker is None or not worker.done():
            return ReconcileResult("running" if worker else "created", {}, {}, {})
        result = self._disposed_result() if worker.cancelled() else worker.result()
        evidence = ({"evidence_ref": result.completion_evidence_ref,
                     "tokens": result.usage.tokens} if result.completion_evidence_ref else {})
        return ReconcileResult(result.status, {}, {}, evidence)

    async def restore_session(
        self, link: SessionHandle, checkpoint: Mapping[str, Any],
    ) -> RestoredSession:
        raise _error("SESSION_MISSING", "this nonpersistent profile requires a new attempt/session",
                     ErrorCategory.INTERRUPTED)

    async def dispose(self, runtime_id: str) -> None:
        for session in self._sessions.values():
            if session.runtime_id != runtime_id:
                continue
            self._stopped.add(session.adapter_session_id)
            self._revoke(session.adapter_session_id)
            submission = self._session_submission.get(session.adapter_session_id)
            worker = self._workers.get(submission or "")
            if worker is not None and not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def close(self) -> None:
        for session in list(self._sessions.values()):
            await self.dispose(session.runtime_id)
