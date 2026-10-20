"""Real pinned CLI probe; run only with explicit live authorization.

Raw stdout/stderr remain in memory: only allowlisted protocol facts are reported.
An empty temporary home/workspace prevents reading a user's Web sessions or dotenv
files. An explicitly selected managed store is resolved by Harness itself; this
test never reads or exports a stored key. The probe disables tools and reasoning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live
PACKAGE = "@deepseek-ai/dsh@0.2.1-rc.2"
PROMPT = "Reply with exactly CLI_SMOKE_OK. Do not invoke any tool."


def test_pinned_headless_cli_returns_a_real_model_answer(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js 24 is required"
    npx_script = Path(node).parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
    assert npx_script.is_file(), "the installed npm npx entrypoint is required"
    env = dict(os.environ)
    patch = Path(__file__).parent / "fixtures" / "headless-smoke.patch.yml"
    arguments = ["headless", "--patch", str(patch.resolve()), "--json", PROMPT]
    cli_override = env.get("PERSONAL_AGENT_DSH_CLI_PATH")
    if cli_override:
        cli = Path(cli_override).resolve()
        package = json.loads((cli.parent.parent / "package.json").read_text(encoding="utf-8"))
        assert package["name"] == "@deepseek-ai/dsh"
        assert package["version"] == "0.2.1-rc.2"
        command = [node, str(cli), *arguments]
        runner = "verified-installed-cli"
    else:
        command = [node, str(npx_script), "--yes", PACKAGE, *arguments]
        runner = "npx"
    home = tmp_path / "harness-home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    env["DSH_HOME"] = str(home)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=workspace, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            process.kill()
        stdout, stderr = process.communicate(timeout=10)
    events: list[dict[str, Any]] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    final_events = [event for event in events if event.get("type") == "final"]
    final = final_events[-1] if final_events else {}
    # Match the fixed harmless response without publishing arbitrary model text.
    matched = final.get("text", "").strip() == "CLI_SMOKE_OK"
    steps = [event for event in events if event.get("phase") == "step_end"]
    usage_keys = {"inputTokens", "outputTokens", "totalTokens", "cacheReadTokens",
                  "cacheWriteTokens", "reasoningTokens"}
    usage = {key: sum(event.get("usage", {}).get(key, 0) for event in steps)
             for key in usage_keys}
    # Harness reports uncached input separately from cache-read input.
    input_tokens_upper = max(
        usage["totalTokens"] - usage["outputTokens"],
        usage["inputTokens"] + usage["cacheReadTokens"] + usage["cacheWriteTokens"],
    )
    turn_reasons = [event.get("reason", {}).get("kind") for event in events
                    if event.get("phase") == "turn_end"]
    tool_calls = sum(event.get("type") == "tool_call" for event in events)
    thinking_events = sum(event.get("type") == "thinking" for event in events)
    session_logs = list(home.rglob("*.jsonl*"))
    safe_codes = sorted(set(re.findall(
        r"\b(?:MISSING_CREDENTIAL|INVALID_CREDENTIAL|MODEL_UNAVAILABLE|MODEL_NOT_FOUND|"
        r"ENOTCACHED|E404|ENOENT|EPERM|MODULE_NOT_FOUND)\b",
        stderr.decode("utf-8", errors="replace") + stdout.decode("utf-8", errors="replace"),
    )))
    summary = {
        "package": PACKAGE, "runner": runner,
        "exit_code": process.returncode, "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "model_api_key_in_launch_environment": bool(env.get("DEEPSEEK_API_KEY")),
        "managed_credential_store_selected": bool(env.get("PERSONAL_AGENT_DSH_CREDENTIAL_STORE")),
        "event_types": sorted({str(event.get("type")) for event in events}),
        "final_event_received": bool(final_events), "expected_answer_received": matched,
        "failure_codes": safe_codes, "raw_output_saved": False,
        "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
        "completed_steps": len(steps), "turn_reasons": turn_reasons,
        "tool_calls": tool_calls, "thinking_events": thinking_events,
        "durable_session_logs": len(session_logs),
        "usage": usage,
        "estimated_cny_peak_no_cache": round(
            input_tokens_upper * 2 / 1_000_000 + usage["outputTokens"] * 8 / 1_000_000, 8
        ) if steps and any(event.get("usage") for event in steps) else None,
    }
    print(json.dumps(summary, ensure_ascii=True))
    assert process.returncode == 0 and matched and not timed_out, summary
    assert len(steps) == 1 and turn_reasons == ["completed"], summary
    assert tool_calls == thinking_events == len(session_logs) == 0, summary
    assert 0 < usage["outputTokens"] <= 32, summary
