"""Live Harness adapter (adoption-gated).

Interface mapping follows the pinned contract, not assumptions from the master
branch: the protocol exposes no ``session/cancel`` or ``session/close``, so a stop
request is cooperative and ``remote_cancel_verifiable`` remains false. Until the
T021 adoption tests record passing evidence, this adapter reports all capability
flags as false and refuses to create sessions; Mock remains the only mode that can
execute.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from personal_agent.bootstrap.config import AppConfig
from personal_agent.domain.common import AppError, ErrorCategory, new_id, stable_hash
from personal_agent.ports.contracts import (
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
    WaitResult,
)

ADOPTION_RECORD = Path(__file__).resolve().parents[4] / "docs" / "runtime-adoption.json"


def adoption_evidence() -> dict[str, Any] | None:
    if not ADOPTION_RECORD.exists():
        return None
    try:
        data = json.loads(ADOPTION_RECORD.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - corrupt evidence
        return None
    if data.get("adopted") is not True:
        return None
    checks = data.get("checks") or {}
    required = {
        "windows_start",
        "wheel_interfaces",
        "tool_scope_denied",
        "gateway_no_bypass",
        "events_redacted",
        "no_reasoning_persisted",
        "unknown_blocks",
        "restore_missing_session_fails",
    }
    if not required <= set(checks):
        return None
    if not all(checks[name] is True for name in required):
        return None
    return data


class LiveRuntime:
    """Real adapter. Only usable when recorded adoption evidence exists."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._evidence = adoption_evidence()
        self._sessions: dict[str, SessionHandle] = {}
        self._submissions: dict[str, str] = {}

    def _require_adopted(self) -> dict[str, Any]:
        if self._evidence is None:
            raise AppError(
                code="RUNTIME_INCOMPATIBLE",
                message=(
                    "the live runtime has not passed its adoption gate; "
                    "see docs/runtime-adoption.md"
                ),
                category=ErrorCategory.DEPENDENCY,
                status=503,
            )
        return self._evidence

    async def capabilities(self) -> RuntimeCapabilities:
        evidence = self._evidence
        if evidence is None:
            return RuntimeCapabilities(
                adapter_version="unadopted",
                sdk_version="unverified",
                runtime_version="unverified",
                platform="windows-x64",
                profile_hash=stable_hash({"profile": "unadopted"}),
                supports_resume=False,
                supports_cooperative_stop=False,
                remote_cancel_verifiable=False,
                tool_policy_enforced=False,
                model_gateway_enforced=False,
                persistence_mode="unknown",
            )
        versions = evidence.get("versions", {})
        return RuntimeCapabilities(
            adapter_version="harness-live-1",
            sdk_version=str(versions.get("sdk_version", "unverified")),
            runtime_version=str(versions.get("runtime_version", "unverified")),
            platform=str(versions.get("platform", "windows-x64")),
            profile_hash=stable_hash(evidence.get("profile", {})),
            supports_resume=bool(evidence.get("supports_resume", False)),
            supports_cooperative_stop=True,
            # The protocol has no verifiable remote cancel; never claim it.
            remote_cancel_verifiable=False,
            tool_policy_enforced=bool(evidence.get("tool_policy_enforced", False)),
            model_gateway_enforced=bool(evidence.get("model_gateway_enforced", False)),
            persistence_mode=str(evidence.get("persistence_mode", "none")),
        )

    async def create_session(self, context: SessionContext) -> SessionHandle:
        self._require_adopted()
        self._load_sdk()
        raise AppError(
            code="RUNTIME_ADAPTER_INCOMPLETE",
            message="the recorded adoption evidence does not cover this deployment",
            category=ErrorCategory.DEPENDENCY,
            status=503,
        )

    async def submit(self, task: RuntimeTask) -> Submission:
        self._require_adopted()
        raise AppError(
            code="RUNTIME_INCOMPATIBLE",
            message="submission is unavailable without a created session",
            category=ErrorCategory.DEPENDENCY,
            status=503,
        )

    async def events(
        self, session_id: str, cursor: int | None = None
    ) -> AsyncIterator[RuntimeEvent]:
        self._require_adopted()
        if False:  # pragma: no cover - makes this an async generator
            yield RuntimeEvent(
                schema_version=1,
                event_id=new_id(),
                session_id=session_id,
                sequence=0,
                occurred_at=datetime.now(UTC),
                kind="session.started",
                owner=Owner(run_id="unused"),
            )

    async def wait_result(self, submission_id: str) -> WaitResult:
        self._require_adopted()
        return WaitResult(status="unknown", error=self._unknown_error())

    async def request_stop(self, session_id: str, reason: str) -> StopResult:
        self._require_adopted()
        # Cooperative stop only: revoke new tool/model permissions and report the
        # request; the protocol cannot verify remote termination.
        return StopResult(status="requested", in_flight_request_ids=[session_id])

    async def reconcile(self, session_id: str) -> ReconcileResult:
        self._require_adopted()
        return ReconcileResult(
            local_runtime_status="unknown",
            model_requests={},
            tool_operations={},
            completion_evidence={},
        )

    async def restore_session(
        self, link: SessionHandle, checkpoint: Mapping[str, Any]
    ) -> RestoredSession:
        self._require_adopted()
        raise AppError(
            code="RUNTIME_INCOMPATIBLE",
            message="the original session cannot be restored; start a new attempt",
            category=ErrorCategory.INTERRUPTED,
            status=409,
        )

    async def dispose(self, runtime_id: str) -> None:
        self._sessions.pop(runtime_id, None)

    async def close(self) -> None:
        for handle in list(self._sessions.values()):
            await self.dispose(handle.runtime_id)

    def _load_sdk(self) -> None:
        import importlib.util

        if importlib.util.find_spec("deepseek_harness") is None:
            raise AppError(
                code="RUNTIME_INCOMPATIBLE",
                message="the pinned Harness SDK is not installed",
                category=ErrorCategory.DEPENDENCY,
                status=503,
            )

    @staticmethod
    def _unknown_error() -> AppError:
        return AppError(
            code="MODEL_OUTCOME_UNKNOWN",
            message="the runtime cannot prove the request finished",
            category=ErrorCategory.UNKNOWN_OUTCOME,
            status=409,
        )

    @property
    def model_transport(self) -> Any:
        raise AppError(
            code="RUNTIME_INCOMPATIBLE",
            message="the live model transport is unavailable until adoption passes",
            category=ErrorCategory.DEPENDENCY,
            status=503,
        )

    def normalized_model_response(self, raw: Mapping[str, Any]) -> ModelResponse:
        raise AppError(
            code="RUNTIME_INCOMPATIBLE",
            message="the live model transport is unavailable until adoption passes",
            category=ErrorCategory.DEPENDENCY,
            status=503,
        )
