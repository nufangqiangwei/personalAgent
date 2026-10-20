"""Pinned headless CLI process and lossless completion boundary.

This module has no provider credentials or model policy. It consumes a trusted
launch patch; the adoption-gated runtime supplies the gateway/tool bindings.
Raw stdout/stderr and reasoning never become diagnostics or persistent events.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from personal_agent.adapters.harness.patch import TASK_ENVIRONMENT_KEYS
from personal_agent.domain.common import AppError, ErrorCategory, stable_hash
from personal_agent.ports.contracts import Usage

CLI_PACKAGE = "@deepseek-ai/dsh"
CLI_VERSION = "0.2.1-rc.2"
MAX_LINE_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 4 * MAX_LINE_BYTES


def _incompatible() -> AppError:
    return AppError(
        code="RUNTIME_INCOMPATIBLE", message="the fixed CLI installation is unavailable or changed",
        category=ErrorCategory.DEPENDENCY, status=503,
    )


@dataclass(frozen=True, slots=True)
class CliInstallation:
    node: Path
    entry: Path
    fingerprint: str

    @classmethod
    def verify(cls, node: Path, entry: Path) -> CliInstallation:
        try:
            node, entry = node.resolve(strict=True), entry.resolve(strict=True)
            package_file = entry.parent.parent / "package.json"
            raw = package_file.read_bytes()
            package = json.loads(raw)
            if (
                package.get("name") != CLI_PACKAGE or package.get("version") != CLI_VERSION
                or package.get("bin", {}).get("dsh") != "lib/bin.js"
                or entry != (package_file.parent / "lib" / "bin.js").resolve()
            ):
                raise ValueError("wrong CLI identity")
            version = subprocess.run(
                [str(node), "--version"], capture_output=True, timeout=5, check=True,
                env=launch_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ).stdout.decode("ascii").strip()
            if not version.startswith("v24."):
                raise ValueError("Node 24 is required")
            fingerprint = stable_hash({
                "package": hashlib.sha256(raw).hexdigest(),
                "entry": hashlib.sha256(entry.read_bytes()).hexdigest(),
                "node": version,
            })
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            raise _incompatible() from None
        return cls(node, entry, fingerprint)

    def recheck(self) -> None:
        if self.verify(self.node, self.entry).fingerprint != self.fingerprint:
            raise _incompatible()


def launch_environment() -> dict[str, str]:
    # Do not inherit API keys, user credential paths, NODE_OPTIONS/NODE_PATH,
    # dotenv selectors, proxy credentials, or DSH permission/profile overrides.
    allowed = {"systemroot", "windir", "comspec", "path", "pathext", "temp", "tmp"}
    return {key: value for key, value in os.environ.items() if key.lower() in allowed}


@dataclass(frozen=True, slots=True)
class CliResult:
    status: str
    exit_code: int | None
    provider_session_id: str | None = None
    text: str | None = None
    usage: Usage = field(default_factory=Usage)
    completion_evidence_ref: str | None = None
    error_code: str | None = None
    durable_session_logs: int = 0


class JsonRun:
    """Bounded protocol state, discarding reasoning and arbitrary vendor fields."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._bytes = 0
        self._invalid = False
        self._session: str | None = None
        self._turn: int | None = None
        self._step: tuple[int, int] | None = None
        self._ended_steps: set[tuple[int, int]] = set()
        self._reason: str | None = None
        self._final: str | None = None
        self._usage = Usage()

    def feed(self, chunk: bytes) -> None:
        self._bytes += len(chunk)
        if self._invalid:
            return
        if self._bytes > MAX_STREAM_BYTES:
            self._invalidate()
            return
        self._buffer.extend(chunk)
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            if len(line) > MAX_LINE_BYTES:
                self._invalidate()
                return
            try:
                value = json.loads(line.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("not an event")
                self._event(value)
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                self._invalidate()
                return
        if len(self._buffer) > MAX_LINE_BYTES:
            self._invalidate()

    def _invalidate(self) -> None:
        self._invalid = True
        self._buffer.clear()
        self._final = None

    def _event(self, value: dict[str, Any]) -> None:
        kind = value.get("type")
        # No arbitrary vendor text, arguments, reasoning, or error strings kept.
        if self._final is not None or value.get("truncated") is True:
            raise ValueError("ambiguous terminal or truncated event")
        if kind == "session":
            session_id = value["sessionId"]
            if self._session is not None or not isinstance(session_id, str) or not session_id:
                raise ValueError("ambiguous session")
            self._session = session_id
        elif kind == "status":
            if self._session is None:
                raise ValueError("missing session")
            phase = value.get("phase")
            turn = value.get("turn")
            if type(turn) is not int or turn < 0:
                raise ValueError("invalid turn")
            if phase == "turn_start":
                if self._turn is not None or self._reason is not None:
                    raise ValueError("overlapping turns")
                self._turn = turn
            elif phase in {"step_start", "step_end"}:
                step = value.get("step")
                if type(step) is not int or step < 0 or turn != self._turn:
                    raise ValueError("invalid step")
                identity = (turn, step)
                if phase == "step_start":
                    if self._step is not None or identity in self._ended_steps:
                        raise ValueError("overlapping or duplicate step")
                    self._step = identity
                else:
                    if identity != self._step or identity in self._ended_steps:
                        raise ValueError("unmatched step")
                    self._ended_steps.add(identity)
                    self._step = None
                    usage = value["usage"]
                    if not isinstance(usage, dict):
                        raise ValueError("missing usage")
                    for count in usage.values():
                        if type(count) is not int or count < 0:
                            raise ValueError("invalid usage")
                    uncached = usage["inputTokens"]
                    output = usage["outputTokens"]
                    inputs = uncached + usage.get("cacheReadTokens", 0)
                    inputs += usage.get("cacheWriteTokens", 0)
                    # Some adapters supply inclusive totals; never undercount a cache bucket.
                    inputs = max(inputs, usage.get("totalTokens", inputs + output) - output)
                    self._usage = Usage(
                        input_tokens=self._usage.input_tokens + inputs,
                        output_tokens=self._usage.output_tokens + output,
                        model_requests=self._usage.model_requests + 1,
                        tool_calls=self._usage.tool_calls,
                    )
            elif phase == "turn_end":
                if self._turn != turn or self._step is not None or self._reason is not None:
                    raise ValueError("unmatched turn end")
                reason = value["reason"]["kind"]
                if reason not in {"completed", "error", "aborted"}:
                    raise ValueError("unknown turn reason")
                self._reason = reason
                self._turn = None
        elif kind == "final":
            text = value.get("text")
            if not isinstance(text, str):
                raise ValueError("missing final text")
            self._final = text
        elif kind == "tool_call":
            self._usage = Usage(
                input_tokens=self._usage.input_tokens, output_tokens=self._usage.output_tokens,
                model_requests=self._usage.model_requests, tool_calls=self._usage.tool_calls + 1,
            )

    def finish(self, exit_code: int | None) -> CliResult:
        complete = (
            not self._invalid and not self._buffer and self._session is not None
            and self._final is not None and self._reason is not None
            and bool(self._ended_steps) and self._step is None and self._turn is None
            and exit_code is not None
        )
        if not complete:
            return CliResult("unknown", exit_code, error_code="CLI_OUTCOME_UNKNOWN")
        success = exit_code == 0 and self._reason == "completed"
        evidence = stable_hash({
            "session": self._session, "exit_code": exit_code, "reason": self._reason,
            "usage": self._usage.as_dict(), "result_hash": stable_hash(self._final),
        })
        return CliResult(
            "completed" if success else "failed", exit_code, self._session,
            self._final if success else None, self._usage, f"cli-completion:{evidence}",
            None if success else "CLI_FAILED",
        )


class HeadlessCli:
    """One process per attempt; killing a process never confirms remote stop."""

    def __init__(self, installation: CliInstallation, patch: Path, root: Path) -> None:
        self.installation = installation
        self.patch = patch.resolve()
        self.root = root
        self._processes: set[asyncio.subprocess.Process] = set()

    @property
    def active_process_count(self) -> int:
        return len(self._processes)

    async def run(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> CliResult:
        self.installation.recheck()
        self.root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="attempt-", dir=self.root) as directory:
            base = Path(directory)
            home, workspace = base / "home", base / "workspace"
            home.mkdir()
            workspace.mkdir()
            env = launch_environment()
            env.update({"DSH_HOME": str(home), "HOME": str(home), "USERPROFILE": str(home)})
            if environment:
                # Only the per-attempt gateway binding may cross the boundary; a
                # supplier credential or arbitrary variable is refused.
                unknown = set(environment) - TASK_ENVIRONMENT_KEYS
                if unknown:
                    raise AppError(
                        code="RUNTIME_ENV_REJECTED",
                        message="a task may only bind the local gateway endpoint and token",
                        category=ErrorCategory.PERMISSION,
                        status=403,
                    )
                env.update(environment)
            process = await asyncio.create_subprocess_exec(
                str(self.installation.node), str(self.installation.entry),
                "headless", "--patch", str(self.patch), "--json", "-",
                cwd=workspace, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self._processes.add(process)
            parser = JsonRun()

            async def consume(stream: asyncio.StreamReader, *, stdout: bool) -> None:
                while chunk := await stream.read(16 * 1024):
                    if stdout:
                        parser.feed(chunk)

            async def send() -> None:
                assert process.stdin
                try:
                    process.stdin.write(prompt.encode("utf-8"))
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

            assert process.stdout and process.stderr
            tasks = [asyncio.create_task(consume(process.stdout, stdout=True)),
                     asyncio.create_task(consume(process.stderr, stdout=False)),
                     asyncio.create_task(send())]
            try:
                async with asyncio.timeout(timeout_seconds):
                    await asyncio.gather(*tasks)
                    await process.wait()
                result = parser.finish(process.returncode)
                persisted = sum(path.is_file() for path in home.rglob("*.jsonl*"))
                if persisted:
                    return replace(
                        result, status="failed" if result.completion_evidence_ref else "unknown",
                        text=None, error_code="CLI_PERSISTENCE_VIOLATION",
                        durable_session_logs=persisted,
                    )
                return result
            except TimeoutError:
                return CliResult("unknown", None, error_code="CLI_TIMEOUT")
            finally:
                # Cancellation is propagated, but first reap the local child and pipes.
                # The persistent model gateway, not this local cleanup, owns UNKNOWN.
                if process.returncode is None:
                    process.kill()
                await asyncio.shield(process.wait())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._processes.discard(process)
