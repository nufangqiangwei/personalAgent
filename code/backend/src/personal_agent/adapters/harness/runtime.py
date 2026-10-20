"""Production CLI runtime: deployment evidence AND mounted boundaries required.

No SDK fallback or Web RPC. Installing a CLI or setting an environment flag
cannot create the gateway/tool binding or grant execution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from personal_agent.adapters.harness.adoption import (
    ADOPTION_RECORD as ADOPTION_RECORD,
)
from personal_agent.adapters.harness.adoption import (
    PROFILE_PATH,
    configured_installation,
    read_adoption_evidence,
)
from personal_agent.adapters.harness.adoption import (
    REQUIRED_CLI_CHECKS as REQUIRED_CLI_CHECKS,
)
from personal_agent.adapters.harness.cli import CLI_VERSION
from personal_agent.adapters.harness.cli_runtime import CliRuntime
from personal_agent.adapters.harness.model_gateway import ModelTransport
from personal_agent.bootstrap.config import AppConfig
from personal_agent.domain.common import AppError, ErrorCategory, stable_hash
from personal_agent.ports.contracts import (
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


def adoption_evidence(
    *, record_path: Path | None = None, profile_path: Path | None = None,
    cli_fingerprint: str | None = None,
) -> dict[str, Any] | None:
    if cli_fingerprint is None:
        installation = configured_installation()
        cli_fingerprint = installation.fingerprint if installation else None
    return read_adoption_evidence(
        record_path=record_path or ADOPTION_RECORD, profile_path=profile_path or PROFILE_PATH,
        cli_fingerprint=cli_fingerprint,
    )


class LiveRuntime:
    """Production composition must provide a gateway-bound CLI and transport.

    Neither is defaulted to a direct supplier CLI. Until the actual bridge is
    available, no production session can start.
    """

    def __init__(
        self, config: AppConfig, *, adapter: CliRuntime | None = None,
        transport: ModelTransport | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._transport = transport

    def _require_adopted(self) -> dict[str, Any]:
        evidence = adoption_evidence(profile_path=self._config.profile_path)
        if evidence is None or self._adapter is None or self._transport is None:
            raise AppError(
                code="RUNTIME_INCOMPATIBLE",
                message="the CLI runtime needs current adoption and mounted gateway/tool bindings",
                category=ErrorCategory.DEPENDENCY, status=503,
            )
        return evidence

    def _bound_adapter(self) -> CliRuntime:
        self._require_adopted()
        assert self._adapter is not None
        return self._adapter

    async def capabilities(self) -> RuntimeCapabilities:
        try:
            self._require_adopted()
        except AppError:
            return RuntimeCapabilities(
                "harness-cli-1-unadopted", "not-used", "unverified", "windows-x64",
                stable_hash({"profile": "unadopted"}),
                False, False, False, False, False, "unknown",
            )
        capabilities = await self._bound_adapter().capabilities()
        return RuntimeCapabilities(
            "harness-cli-1", "not-used", CLI_VERSION, "windows-x64", capabilities.profile_hash,
            False, True, False, True, True, "none-no-reasoning",
        )

    async def create_session(self, context: SessionContext) -> SessionHandle:
        return await self._bound_adapter().create_session(context)

    async def submit(self, task: RuntimeTask) -> Submission:
        return await self._bound_adapter().submit(task)

    async def events(
        self, session_id: str, cursor: int | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        async for event in self._bound_adapter().events(session_id, cursor):
            yield event

    async def wait_result(self, submission_id: str) -> WaitResult:
        return await self._bound_adapter().wait_result(submission_id)

    async def request_stop(self, session_id: str, reason: str) -> StopResult:
        # Stop/disposal still operate if evidence changed after submission.
        if self._adapter is None:
            return StopResult("unknown")
        return await self._adapter.request_stop(session_id, reason)

    async def reconcile(self, session_id: str) -> ReconcileResult:
        if self._adapter is None:
            return ReconcileResult("unknown", {}, {}, {})
        return await self._adapter.reconcile(session_id)

    async def restore_session(
        self, link: SessionHandle, checkpoint: Mapping[str, Any],
    ) -> RestoredSession:
        return await self._bound_adapter().restore_session(link, checkpoint)

    async def dispose(self, runtime_id: str) -> None:
        if self._adapter is not None:
            await self._adapter.dispose(runtime_id)

    async def close(self) -> None:
        if self._adapter is not None:
            await self._adapter.close()

    @property
    def model_transport(self) -> ModelTransport:
        self._require_adopted()
        assert self._transport is not None
        return self._transport
