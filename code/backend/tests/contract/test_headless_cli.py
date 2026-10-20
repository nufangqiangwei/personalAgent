"""Offline subprocess contract: a fake CLI never imports a provider or uses HTTP."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from personal_agent.adapters.harness.cli import CliInstallation, HeadlessCli, JsonRun
from personal_agent.domain.common import AppError


def events(*, text: str = '{"answer":"ok"}') -> list[dict[str, Any]]:
    return [
        {"type": "session", "sessionId": "provider-session"},
        {"type": "status", "phase": "turn_start", "turn": 0},
        {"type": "status", "phase": "step_start", "turn": 0, "step": 0},
        {"type": "thinking", "text": "PRIVATE_THOUGHT"},
        {"type": "status", "phase": "step_end", "turn": 0, "step": 0,
         "usage": {"inputTokens": 3, "cacheReadTokens": 10,
                   "outputTokens": 2, "totalTokens": 15}},
        {"type": "status", "phase": "turn_end", "turn": 0,
         "reason": {"kind": "completed"}},
        {"type": "final", "text": text},
    ]


def parse(values: list[dict[str, Any]], *, code: int = 0) -> Any:
    run = JsonRun()
    for value in values:
        run.feed(json.dumps(value).encode() + b"\n")
    return run.finish(code)


def test_success_requires_protocol_usage_and_counts_cached_input() -> None:
    result = parse(events())
    assert result.status == "completed"
    assert result.text == '{"answer":"ok"}'
    assert result.usage.input_tokens == 13
    assert result.usage.tokens == 15
    assert result.usage.model_requests == 1
    assert result.completion_evidence_ref
    assert "PRIVATE_THOUGHT" not in repr(result)


@pytest.mark.parametrize("change", [
    "missing_final", "duplicate_final", "missing_usage", "negative_usage", "bool_usage",
    "missing_turn_end", "truncated", "duplicate_step", "missing_session", "two_sessions",
    "event_after_final", "missing_step_end", "wrong_step", "unknown_reason",
])
def test_incomplete_or_ambiguous_protocol_is_unknown(change: str) -> None:
    values = events()
    if change == "missing_final":
        values.pop()
    elif change == "duplicate_final":
        values.append(values[-1])
    elif change == "missing_usage":
        values[4].pop("usage")
    elif change == "negative_usage":
        values[4]["usage"]["inputTokens"] = -1
    elif change == "bool_usage":
        values[4]["usage"]["inputTokens"] = True
    elif change == "missing_turn_end":
        values.pop(5)
    elif change == "truncated":
        values[4]["truncated"] = True
    elif change == "duplicate_step":
        values.insert(5, values[4])
    elif change == "missing_session":
        values.pop(0)
    elif change == "two_sessions":
        values.insert(1, {"type": "session", "sessionId": "another"})
    elif change == "event_after_final":
        values.append({"type": "status", "phase": "turn_start", "turn": 1})
    elif change == "missing_step_end":
        values.pop(4)
    elif change == "wrong_step":
        values[4]["step"] = 1
    elif change == "unknown_reason":
        values[5]["reason"]["kind"] = "invented"
    result = parse(values)
    assert result.status == "unknown"
    assert result.completion_evidence_ref is None


def test_final_with_nonzero_exit_never_succeeds() -> None:
    result = parse(events(), code=1)
    assert result.status == "failed"
    # The remote completion and its usage remain facts even when local teardown fails.
    assert result.usage.tokens == 15 and result.completion_evidence_ref


@pytest.mark.parametrize("raw", [b"not json\n", b"{}", b"[]\n", b"\xff\n"])
def test_malformed_or_unterminated_stdout_never_completes(raw: bytes) -> None:
    run = JsonRun()
    run.feed(raw)
    assert run.finish(0).status == "unknown"


def fake_cli(tmp_path: Path, body: str) -> CliInstallation:
    package = tmp_path / "fake-package"
    entry = package / "lib" / "bin.js"
    entry.parent.mkdir(parents=True)
    entry.write_text(body, encoding="utf-8")
    (package / "package.json").write_text(json.dumps({
        "name": "@deepseek-ai/dsh", "version": "0.2.1-rc.2", "type": "module",
        "bin": {"dsh": "lib/bin.js"},
    }), encoding="utf-8")
    node = shutil.which("node")
    assert node
    return CliInstallation.verify(Path(node), entry)


def test_installation_rejects_version_or_entry_mismatch(tmp_path: Path) -> None:
    installation = fake_cli(tmp_path, "")
    package_file = installation.entry.parent.parent / "package.json"
    package = json.loads(package_file.read_text())
    package["version"] = "0.2.1-rc.3"
    package_file.write_text(json.dumps(package))
    with pytest.raises(AppError, match="fixed CLI"):
        CliInstallation.verify(installation.node, installation.entry)


async def test_process_uses_stdin_isolated_home_and_no_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "user text ' ` $() 中文"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SENTINEL_SECRET")
    monkeypatch.setenv("NODE_OPTIONS", "--require bad.js")
    monkeypatch.setenv("PERSONAL_AGENT_DSH_CREDENTIAL_STORE", "PRIVATE_STORE")
    body = """
import process from 'node:process';
let input = ''; for await (const chunk of process.stdin) input += chunk;
if (input !== EXPECTED || process.argv.at(-1) !== '-' ||
    process.env.DEEPSEEK_API_KEY || process.env.NODE_OPTIONS ||
    process.env.PERSONAL_AGENT_DSH_CREDENTIAL_STORE ||
    process.cwd() === process.env.DSH_HOME) process.exit(8);
process.stderr.write('PRIVATE_THOUGHT SENTINEL_SECRET');
for (const event of EVENTS) process.stdout.write(JSON.stringify(event) + '\\n');
""".replace("EXPECTED", json.dumps(expected)).replace("EVENTS", json.dumps(events()))
    installation = fake_cli(tmp_path, body)
    patch = tmp_path / "restricted.yml"
    patch.write_text("[]")
    runner = HeadlessCli(installation, patch, tmp_path / "managed")
    result = await runner.run(expected, timeout_seconds=5)
    assert result.status == "completed"
    assert result.exit_code == 0
    assert "SENTINEL_SECRET" not in repr(result)
    assert not list((tmp_path / "managed").rglob("*.jsonl"))


async def test_timeout_and_local_cancellation_never_prove_remote_end(tmp_path: Path) -> None:
    installation = fake_cli(tmp_path, "setInterval(() => {}, 1000);")
    patch = tmp_path / "restricted.yml"
    patch.write_text("[]")
    runner = HeadlessCli(installation, patch, tmp_path / "managed")
    result = await runner.run("fixed", timeout_seconds=0.05)
    assert result.status == "unknown" and result.completion_evidence_ref is None
    pending = asyncio.create_task(runner.run("fixed", timeout_seconds=30))
    await asyncio.sleep(0.05)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert runner.active_process_count == 0


async def test_pipe_flood_is_bounded_and_does_not_deadlock(tmp_path: Path) -> None:
    installation = fake_cli(tmp_path, """
process.stderr.write('x'.repeat(1024 * 1024));
process.stdout.write('x'.repeat(1024 * 1024));
""")
    patch = tmp_path / "restricted.yml"
    patch.write_text("[]")
    result = await HeadlessCli(installation, patch, tmp_path / "managed").run(
        "fixed", timeout_seconds=5,
    )
    assert result.status == "unknown" and result.text is None


async def test_persistence_violation_is_detected_before_home_cleanup(tmp_path: Path) -> None:
    body = """
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
writeFileSync(join(process.env.DSH_HOME, 'session.jsonl'), 'PRIVATE_THOUGHT');
for (const event of EVENTS) process.stdout.write(JSON.stringify(event) + '\\n');
""".replace("EVENTS", json.dumps(events()))
    installation = fake_cli(tmp_path, body)
    patch = tmp_path / "restricted.yml"
    patch.write_text("[]")
    result = await HeadlessCli(installation, patch, tmp_path / "managed").run(
        "fixed", timeout_seconds=5,
    )
    assert result.status == "failed" and result.error_code == "CLI_PERSISTENCE_VIOLATION"
    assert result.durable_session_logs == 1
    assert result.usage.tokens == 15 and result.completion_evidence_ref


async def test_a_task_may_only_bind_the_per_attempt_gateway(tmp_path: Path) -> None:
    body = """
import process from 'node:process';
if (process.env.PERSONAL_AGENT_GATEWAY_TOKEN !== 'per-attempt'
    || process.env.PERSONAL_AGENT_GATEWAY_BASE_URL !== 'http://127.0.0.1:1/anthropic')
  process.exit(8);
for (const event of EVENTS) process.stdout.write(JSON.stringify(event) + '\\n');
""".replace("EVENTS", json.dumps(events()))
    installation = fake_cli(tmp_path, body)
    patch = tmp_path / "restricted.yml"
    patch.write_text("[]")
    runner = HeadlessCli(installation, patch, tmp_path / "managed")
    result = await runner.run(
        "fixed", timeout_seconds=5,
        environment={
            "PERSONAL_AGENT_GATEWAY_TOKEN": "per-attempt",
            "PERSONAL_AGENT_GATEWAY_BASE_URL": "http://127.0.0.1:1/anthropic",
        },
    )
    assert result.status == "completed"
    with pytest.raises(AppError) as error:
        await runner.run("fixed", timeout_seconds=5, environment={"DEEPSEEK_API_KEY": "x"})
    assert error.value.code == "RUNTIME_ENV_REJECTED"
