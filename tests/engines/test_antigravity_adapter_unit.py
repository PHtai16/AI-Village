"""Unit tests for the minimal Antigravity CLI adapter (mocked subprocess I/O)."""

from pathlib import Path
import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from engines.antigravity import (
    AntigravityAdapter,
    AntigravityHandshakeRequest,
    discover,
    format_command_summary,
    redact_secrets,
)


def _completed(
    args: list[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Create a synthetic CompletedProcess for mocked runner tests."""
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture
def fake_agy_binary(tmp_path: Path) -> Path:
    """Create a dummy executable file path for mocked adapter tests."""
    binary = tmp_path / "agy.exe"
    binary.write_text("dummy", encoding="utf-8")
    return binary


@pytest.fixture
def fake_workspace(tmp_path: Path) -> Path:
    """Create an isolated temporary workspace directory."""
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_discover_explicit_env_path_and_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_agy_binary: Path,
) -> None:
    """Verify explicit, env, PATH, user-home fallback, and missing discovery paths."""
    monkeypatch.delenv("ANTIGRAVITY_CLI_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    explicit = discover(str(fake_agy_binary))
    assert explicit.found is True
    assert explicit.source == "explicit"
    assert explicit.executable_path == str(fake_agy_binary.resolve())

    missing_explicit = discover(str(tmp_path / "missing.exe"))
    assert missing_explicit.found is False
    assert missing_explicit.source == "not_found"

    monkeypatch.setenv("ANTIGRAVITY_CLI_PATH", str(fake_agy_binary))
    from_env = discover()
    assert from_env.found is True
    assert from_env.source == "env"

    monkeypatch.setenv("ANTIGRAVITY_CLI_PATH", str(tmp_path / "bad_env.exe"))
    bad_env = discover()
    assert bad_env.found is False

    monkeypatch.delenv("ANTIGRAVITY_CLI_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: str(fake_agy_binary))
    from_path = discover()
    assert from_path.found is True
    assert from_path.source == "path"

    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "engines.antigravity.adapter._default_user_cli_path",
        lambda: fake_agy_binary,
    )
    from_home = discover()
    assert from_home.found is True
    assert from_home.source == "user_home"

    monkeypatch.setattr(
        "engines.antigravity.adapter._default_user_cli_path",
        lambda: tmp_path / "absent.exe",
    )
    absent = discover()
    assert absent.found is False
    assert absent.source == "not_found"


def test_probe_parses_version_and_supported_flags(fake_agy_binary: Path) -> None:
    """Verify capability detection parses real CLI version and help flags."""
    help_sample = (
        "Usage of agy.exe:\n"
        "  --output-format  Output format for print mode (text, json, stream-json)\n"
        "  --print          Run a single prompt non-interactively\n"
        "  --print-timeout  Optional time limit for print mode\n"
        "  --sandbox        Run in a sandbox\n"
        "Available subcommands:\n"
        "  models           List available models\n"
    )

    def runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        return _completed(cmd, 0, stdout="", stderr=help_sample)

    adapter = AntigravityAdapter(cli_path=str(fake_agy_binary), runner=runner)
    caps = adapter.probe()
    assert caps.version == "1.5.14"
    assert caps.supports_print_mode is True
    assert caps.supports_json_output is True
    assert caps.supports_stream_json_flag is True
    assert caps.supports_print_timeout is True
    assert caps.supports_sandbox_flag is True
    assert caps.supports_models_subcommand is True
    assert caps.probe_exit_code == 0
    assert caps.error is None


def test_handshake_success_with_verified_telemetry(
    fake_agy_binary: Path,
    fake_workspace: Path,
) -> None:
    """Verify successful JSON handshake captures response, ID, and measured tokens."""
    seen_commands: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_commands.append(cmd)
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout="--print --output-format json --print-timeout")
        payload = (
            '{"conversation_id":"conv-123","status":"SUCCESS",'
            '"response":"AI_VILLAGE_HANDSHAKE_OK\\n","duration_seconds":1.25,'
            '"num_turns":1,"usage":{"input_tokens":100,"output_tokens":12,'
            '"thinking_tokens":5,"cache_read_tokens":0,"total_tokens":112}}'
        )
        return _completed(cmd, 0, stdout=payload)

    adapter = AntigravityAdapter(cli_path=str(fake_agy_binary), runner=runner)
    req = AntigravityHandshakeRequest(
        prompt="Reply with exact text: AI_VILLAGE_HANDSHAKE_OK",
        workspace_path=str(fake_workspace),
        timeout_seconds=30,
    )
    res = adapter.handshake(req)

    assert res.status == "SUCCEEDED"
    assert res.exit_code == 0
    assert res.version == "1.5.14"
    assert res.conversation_id == "conv-123"
    assert res.response_text == "AI_VILLAGE_HANDSHAKE_OK\n"
    assert res.telemetry.telemetry_status == "VERIFIED_MEASURED"
    assert res.telemetry.input_tokens == 100
    assert res.telemetry.output_tokens == 12
    assert res.telemetry.total_tokens == 112
    assert res.error is None
    assert any(arg.startswith("--print=Reply") for arg in seen_commands[-1])


def test_handshake_without_usage_marks_telemetry_unavailable(
    fake_agy_binary: Path,
    fake_workspace: Path,
) -> None:
    """Ensure missing usage metrics remain None with UNAVAILABLE_FROM_PROVIDER."""

    def runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout="--print --output-format json")
        return _completed(cmd, 0, stdout='{"status":"SUCCESS","response":"OK"}')

    adapter = AntigravityAdapter(cli_path=str(fake_agy_binary), runner=runner)
    res = adapter.handshake(
        AntigravityHandshakeRequest(prompt="ping", workspace_path=str(fake_workspace))
    )
    assert res.status == "SUCCEEDED"
    assert res.telemetry.telemetry_status == "UNAVAILABLE_FROM_PROVIDER"
    assert res.telemetry.input_tokens is None
    assert res.telemetry.total_tokens is None


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected_code", "expected_retryable"),
    [
        (2, "Error: unknown flag: --bogus", "UNSUPPORTED_ARGUMENT", False),
        (1, "Error: user is unauthenticated, please login", "AUTH_ERROR", False),
        (1, "Error: quota exceeded for project", "QUOTA_EXCEEDED", False),
        (1, "HTTP 429 Too Many Requests: rate limit hit", "RATE_LIMITED", True),
        (1, "Internal backend error", "PROVIDER_ERROR", False),
    ],
)
def test_handshake_error_classification(
    fake_agy_binary: Path,
    fake_workspace: Path,
    returncode: int,
    stderr: str,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    """Verify CLI failures normalize into expected error codes without infinite retry."""

    def runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout="--print")
        return _completed(cmd, returncode, stdout="", stderr=stderr)

    adapter = AntigravityAdapter(cli_path=str(fake_agy_binary), runner=runner)
    res = adapter.handshake(
        AntigravityHandshakeRequest(prompt="ping", workspace_path=str(fake_workspace))
    )
    assert res.status == "FAILED"
    assert res.error is not None
    assert res.error.error_code == expected_code
    assert res.error.retryable is expected_retryable


def test_handshake_handles_missing_binary_invalid_json_timeout_and_oserror(
    tmp_path: Path,
    fake_agy_binary: Path,
    fake_workspace: Path,
) -> None:
    """Verify BLOCKED on missing binary, INVALID_RESPONSE, TIMED_OUT, and OSError."""
    missing_adapter = AntigravityAdapter(cli_path=str(tmp_path / "missing_agy.exe"))
    missing_res = missing_adapter.handshake(
        AntigravityHandshakeRequest(prompt="ping", workspace_path=str(fake_workspace))
    )
    assert missing_res.status == "BLOCKED"
    assert missing_res.error is not None
    assert missing_res.error.error_code == "EXECUTABLE_NOT_FOUND"

    def invalid_json_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd or "--help" in cmd:
            return _completed(cmd, 0, stdout="1.5.14")
        return _completed(cmd, 0, stdout="not-json-output")

    bad_json_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=invalid_json_runner,
    ).handshake(AntigravityHandshakeRequest(prompt="ping", workspace_path=str(fake_workspace)))
    assert bad_json_res.status == "FAILED"
    assert bad_json_res.error is not None
    assert bad_json_res.error.error_code == "INVALID_RESPONSE"

    def timeout_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd or "--help" in cmd:
            return _completed(cmd, 0, stdout="1.5.14")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5, stderr="token=secret123456")

    timeout_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=timeout_runner,
    ).handshake(AntigravityHandshakeRequest(prompt="ping", workspace_path=str(fake_workspace)))
    assert timeout_res.status == "TIMED_OUT"
    assert timeout_res.error is not None
    assert timeout_res.error.error_code == "TIMED_OUT"
    assert "secret123456" not in timeout_res.redacted_stderr

    def oserror_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd or "--help" in cmd:
            return _completed(cmd, 0, stdout="1.5.14")
        raise OSError("Access is denied")

    oserror_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=oserror_runner,
    ).handshake(AntigravityHandshakeRequest(prompt="ping", workspace_path=str(fake_workspace)))
    assert oserror_res.status == "FAILED"
    assert oserror_res.error is not None
    assert oserror_res.error.error_code == "PROCESS_LAUNCH_ERROR"

    invalid_ws_res = AntigravityAdapter(cli_path=str(fake_agy_binary)).handshake(
        AntigravityHandshakeRequest(prompt="ping", workspace_path=str(tmp_path / "no_dir"))
    )
    assert invalid_ws_res.status == "FAILED"
    assert invalid_ws_res.error is not None
    assert invalid_ws_res.error.error_code == "PROCESS_LAUNCH_ERROR"


def test_secret_redaction_and_request_validation() -> None:
    """Verify API keys and tokens are stripped from diagnostics and blank prompts fail."""
    fake_google = "AIza" + ("B" * 35)
    fake_openai = "sk-" + ("C" * 48)
    raw = f"Authorization: Bearer abcdefghijklmnop api_key=my_secret_val {fake_google} {fake_openai}"
    redacted = redact_secrets(raw)
    assert "my_secret_val" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert fake_google not in redacted
    assert fake_openai not in redacted

    cmd_summary = format_command_summary(
        ["agy.exe", "--output-format", "json", "--print=hello world"]
    )
    assert "--print=<11_chars>" in cmd_summary
    assert "hello world" not in cmd_summary

    with pytest.raises(ValidationError):
        AntigravityHandshakeRequest(prompt="   ", workspace_path=".")
