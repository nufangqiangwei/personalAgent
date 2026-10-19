"""Explicit ports. The domain depends only on these DTOs and protocols.

The signatures are Personal-Agent ports, not the vendor SDK surface: the Harness
adapter maps them and may not invent calls (for example ``session.cancel``) that
the pinned wheel does not expose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from personal_agent.domain.common import AppError


@dataclass(frozen=True, slots=True)
class Owner:
    """Exactly one owner: a research attempt, or a requirement draft."""

    run_id: str | None = None
    attempt_id: str | None = None
    draft_id: str | None = None

    def __post_init__(self) -> None:
        provided = [value for value in (self.run_id, self.attempt_id, self.draft_id) if value]
        if len(provided) == 0:
            raise ValueError("an owner requires run_id, attempt_id or draft_id")
        if self.attempt_id and not self.run_id:
            raise ValueError("attempt_id requires its run_id")

    @property
    def kind(self) -> str:
        return "DRAFT" if self.draft_id else "RUN"


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    model_requests: int = 0
    tool_calls: int = 0
    search_calls: int = 0
    page_reads: int = 0
    active_ms: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model_requests": self.model_requests,
            "tool_calls": self.tool_calls,
            "search_calls": self.search_calls,
            "page_reads": self.page_reads,
            "active_ms": self.active_ms,
        }


# --------------------------------------------------------------------------
# Runtime (official Harness Loop; adapter maps the pinned wheel)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    adapter_version: str
    sdk_version: str
    runtime_version: str
    platform: str
    profile_hash: str
    supports_resume: bool
    supports_cooperative_stop: bool
    remote_cancel_verifiable: bool
    tool_policy_enforced: bool
    model_gateway_enforced: bool
    persistence_mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter_version": self.adapter_version,
            "sdk_version": self.sdk_version,
            "runtime_version": self.runtime_version,
            "platform": self.platform,
            "profile_hash": self.profile_hash,
            "supports_resume": self.supports_resume,
            "supports_cooperative_stop": self.supports_cooperative_stop,
            "remote_cancel_verifiable": self.remote_cancel_verifiable,
            "tool_policy_enforced": self.tool_policy_enforced,
            "model_gateway_enforced": self.model_gateway_enforced,
            "persistence_mode": self.persistence_mode,
        }


@dataclass(frozen=True, slots=True)
class SessionContext:
    operation_id: str
    owner: Owner
    epoch: int
    model_id: str
    profile_hash: str
    capability_token: str
    policy_hash: str


@dataclass(frozen=True, slots=True)
class SessionHandle:
    adapter_session_id: str
    runtime_id: str
    provider_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTask:
    operation_id: str
    session_id: str
    epoch: int
    goal: str
    context_refs: Sequence[str]
    validated_input: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    deadline: datetime
    tool_scope: Sequence[str]
    budget_reservation_id: str
    direction_ids: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class Submission:
    submission_id: str
    status: str  # "accepted" is enqueue only, never business completion


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    schema_version: int
    event_id: str
    session_id: str
    sequence: int
    occurred_at: datetime
    kind: str
    owner: Owner
    attempt_id: str | None = None
    submission_id: str | None = None
    epoch: int | None = None
    provider_event_key: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WaitResult:
    status: str  # completed | failed | unknown
    structured_output: Mapping[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    tool_action_refs: Sequence[str] = ()
    completion_evidence_ref: str | None = None
    error: AppError | None = None


@dataclass(frozen=True, slots=True)
class StopResult:
    status: str  # requested | drained | unknown
    in_flight_request_ids: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    local_runtime_status: str
    model_requests: Mapping[str, str]
    tool_operations: Mapping[str, str]
    completion_evidence: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RestoredSession:
    session: SessionHandle
    restored_original: bool
    checkpoint_hash: str | None = None


@runtime_checkable
class RuntimePort(Protocol):
    async def capabilities(self) -> RuntimeCapabilities: ...

    async def create_session(self, context: SessionContext) -> SessionHandle: ...

    async def submit(self, task: RuntimeTask) -> Submission: ...

    def events(
        self, session_id: str, cursor: int | None = None
    ) -> AsyncIterator[RuntimeEvent]: ...

    async def wait_result(self, submission_id: str) -> WaitResult: ...

    async def request_stop(self, session_id: str, reason: str) -> StopResult: ...

    async def reconcile(self, session_id: str) -> ReconcileResult: ...

    async def restore_session(
        self, link: SessionHandle, checkpoint: Mapping[str, Any]
    ) -> RestoredSession: ...

    async def dispose(self, runtime_id: str) -> None: ...


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolRequest:
    operation_id: str
    run_id: str
    attempt_id: str
    epoch: int
    tool_name: str
    args: Mapping[str, Any]
    scope_token: str
    deadline: datetime


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: str
    result_ref: str | None = None
    source_refs: Sequence[str] = ()
    side_effect_operation_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    error: AppError | None = None


@runtime_checkable
class ToolPort(Protocol):
    async def execute(self, request: ToolRequest) -> ToolResult: ...


# --------------------------------------------------------------------------
# Model gateway
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    request_id: str
    owner: Owner
    epoch: int
    model_id: str
    messages: Sequence[ModelMessage]
    output_limit: int
    deadline: datetime
    reservation_id: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    status: str  # completed | failed | unknown
    content: str | None = None
    usage: Usage = field(default_factory=Usage)
    completion_evidence_ref: str | None = None
    error: AppError | None = None


@runtime_checkable
class ModelGatewayPort(Protocol):
    async def request(self, request: ModelRequest) -> ModelResponse: ...


# --------------------------------------------------------------------------
# Infrastructure ports
# --------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ms(self) -> int: ...


@runtime_checkable
class RendererPort(Protocol):
    def render_report(self, document: Mapping[str, Any]) -> str: ...

    def render_checklist(self, document: Mapping[str, Any]) -> str: ...

    def render_comparison(self, document: Mapping[str, Any]) -> str: ...


@runtime_checkable
class ArtifactContentStore(Protocol):
    async def put(self, relative_path: str, data: bytes) -> str: ...

    async def get(self, relative_path: str) -> bytes: ...

    async def exists(self, relative_path: str) -> bool: ...

    async def delete(self, relative_path: str) -> None: ...
