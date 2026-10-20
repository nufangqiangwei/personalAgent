"""The launch patch is data with hard boundaries, checked without the CLI."""

from __future__ import annotations

from pathlib import Path

from personal_agent.adapters.harness.patch import (
    GATEWAY_BASE_URL_ENV,
    GATEWAY_TOKEN_ENV,
    PROFILE_PATH_ENV,
    TASK_ENVIRONMENT_KEYS,
    cli_patch_text,
    write_cli_patch,
)


def patch() -> str:
    return cli_patch_text(
        policy_plugin=Path("code/runtime-policy/dist/index.js"), max_output_tokens=64,
    )


def test_patch_points_models_at_the_loopback_gateway_only() -> None:
    text = patch()
    assert f"baseURL: !!js process.env.{GATEWAY_BASE_URL_ENV}" in text
    assert f"apiKeyEnv: {GATEWAY_TOKEN_ENV}" in text
    assert "https://api.deepseek.com" not in text, "no direct provider endpoint"
    assert TASK_ENVIRONMENT_KEYS == {GATEWAY_BASE_URL_ENV, GATEWAY_TOKEN_ENV, PROFILE_PATH_ENV}


def test_patch_disables_persistence_retry_and_default_tools() -> None:
    text = patch()
    for entry in (
        "session-persistence-jsonl", "session-log-deepseek", "compaction-basic",
        "llm-retry", "subagent-spawn-in-process", "tool-bash", "tool-web",
    ):
        assert f"- id: {entry}\n  disabled: true" in text
    assert "maxRetries: 0" in text
    plugin_line = "runtime-policy/dist/index.js" in text.replace("\\", "/")
    assert "personal-agent-policy" in text and plugin_line
    assert "maxTokens: 64" in text


def test_patch_contains_no_secret_and_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "a.yml"
    second = tmp_path / "b.yml"
    plugin = Path("code/runtime-policy/dist/index.js")
    write_cli_patch(first, policy_plugin=plugin, max_output_tokens=64)
    write_cli_patch(second, policy_plugin=plugin, max_output_tokens=64)
    assert first.read_bytes() == second.read_bytes()
    text = first.read_text(encoding="utf-8")
    for forbidden in ("DEEPSEEK_API_KEY", "api_key", ".credentials", "x-api-key"):
        assert forbidden not in text
