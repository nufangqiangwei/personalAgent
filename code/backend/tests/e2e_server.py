"""Local E2E host with controllable external observations, never shipped in the app.

Uses the production app and services. The only fixture API arms/releases the Mock
runtime's result boundary, without editing business rows or synthesizing snapshots.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import uvicorn

from personal_agent.adapters.harness.mock_runtime import MockRuntime
from personal_agent.api.app import create_app
from personal_agent.bootstrap.clock import SystemClock
from personal_agent.bootstrap.config import load_config
from personal_agent.bootstrap.lifespan import build_context
from personal_agent.domain.common import AppError, ErrorCategory
from personal_agent.ports.contracts import ModelRequest, ModelResponse, Usage, WaitResult

E2E_WORKSPACE = Path(__file__).resolve().parents[3] / ".cache" / "e2e-data"


class ObservationClock(SystemClock):
    offset_ms = 0

    def advance(self, milliseconds: int) -> None:
        self.offset_ms += milliseconds

    def now(self) -> Any:
        return super().now() + timedelta(milliseconds=self.offset_ms)

    def monotonic_ms(self) -> int:
        return super().monotonic_ms() + self.offset_ms


class GatedRuntime(MockRuntime):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.hold = False
        self.entered = 0
        self.release = asyncio.Event()
        self.advance_result = False

    async def wait_result(self, submission_id: str) -> WaitResult:
        if self.hold:
            self.entered += 1
            await self.release.wait()
        result = await super().wait_result(submission_id)
        if self.advance_result:
            self._clock.advance(2000)
        return result


class GatedTransport:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.hold = False
        self.entered = 0
        self.active = 0
        self.maximum = 0
        self.release = asyncio.Event()
        self.timeout_once = False
        self.research_calls = 0
        self.fail_research = False

    async def complete(self, spec: ModelRequest) -> ModelResponse:
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            if spec.owner.run_id:
                self.research_calls += 1
                if self.fail_research:
                    return ModelResponse(
                        status="failed", usage=Usage(input_tokens=100, output_tokens=50),
                        completion_evidence_ref=f"mock:e2e:failed:{spec.request_id}",
                        error=AppError(code="SOURCE_FAILED", message="固定来源失败观测",
                                       category=ErrorCategory.DEPENDENCY),
                    )
                if self.timeout_once:
                    self.timeout_once = False
                    return ModelResponse(
                        status="unknown", error=AppError(
                            code="MODEL_TIMEOUT", message="固定超时，远端结果未明",
                            category=ErrorCategory.TIMEOUT,
                        ),
                    )
            text = "\n".join(message.content for message in spec.messages)
            cases = {
                "P8-output": ("output_structure", "checklist"),
                "P8-topic": ("topic", "城市夜间公交"),
                "P8-unknown": ("unclassified_requirement", "尚不明确的要求"),
            }
            for marker, (path, value) in cases.items():
                if spec.owner.draft_id and marker in text:
                    return ModelResponse(
                        status="completed", content=json.dumps({
                            "fields": [{"path": path, "value": value, "source": "user"}],
                            "suggestions": [], "conflicts": [], "missing_fields": [],
                        }), usage=Usage(input_tokens=100, output_tokens=50),
                        completion_evidence_ref=f"mock:e2e:{spec.request_id}",
                    )
            if self.hold and spec.owner.run_id:
                self.entered += 1
                await self.release.wait()
            return await self.inner.complete(spec)
        finally:
            self.active -= 1


async def main() -> None:
    # Refuse accidental use against the normal user workspace or any live mode.
    config = load_config(mode="mock")
    if config.data_dir != E2E_WORKSPACE:
        raise RuntimeError("E2E fixtures require the isolated repository cache workspace")
    context = await build_context(config, clock=ObservationClock())
    runtime = GatedRuntime(context.runtime._observations, context.clock)
    runtime.bind_model_gateway(context.gateway)
    context.runtime = runtime
    context.engine._deps.runtime = runtime
    transport = GatedTransport(context.gateway._transport)
    context.gateway._transport = transport
    fault_after_publish = False
    original_put = context.writer.store.put

    async def observed_put(relative_path: str, data: bytes) -> Any:
        nonlocal fault_after_publish
        stored = await original_put(relative_path, data)
        if fault_after_publish:
            fault_after_publish = False
            raise AppError(code="WRITE_RESULT_UNKNOWN", message="文件发布后提交前的固定故障观测",
                           category=ErrorCategory.UNKNOWN_OUTCOME)
        return stored

    context.writer.store.put = observed_put  # type: ignore[method-assign]
    app = create_app(config, context=context)

    @app.post("/__test__/gate/{command}")
    async def gate(command: str) -> dict[str, int | bool]:
        nonlocal fault_after_publish
        if command == "arm":
            runtime.hold = True
            runtime.release.clear()
        elif command == "arm-model":
            transport.hold = True
            transport.release.clear()
        elif command == "release":
            runtime.hold = False
            runtime.release.set()
            transport.hold = False
            transport.release.set()
            runtime.advance_result = False
            transport.fail_research = False
        elif command == "advance-result":
            runtime.advance_result = True
        elif command == "timeout-model":
            transport.timeout_once = True
        elif command == "fail-model":
            transport.fail_research = True
        elif command == "fault-after-publish":
            fault_after_publish = True
        elif command == "finish-unknown":
            pending = await context.gateway.non_terminal_request()
            if pending is not None and pending.status == "UNKNOWN":
                await context.gateway.reconcile(pending.id, {
                    "completed": True, "tokens": 150,
                    "evidence_ref": f"mock:e2e:verified-end:{pending.id}",
                })
        elif command != "status":
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        return {"held": runtime.hold or transport.hold, "entered": runtime.entered,
                "model_entered": transport.entered, "active": transport.active,
                "maximum": transport.maximum, "research_calls": transport.research_calls}

    try:
        await uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=int(os.environ.get("PERSONAL_AGENT_E2E_PORT", "8123")),
            access_log=False,
        )).serve()
    finally:
        runtime.release.set()
        transport.release.set()
        await context.executor.shutdown()
        await context.database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
