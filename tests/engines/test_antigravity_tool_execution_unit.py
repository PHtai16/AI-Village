"""Unit tests for P02.02 Antigravity tool execution and independent verification."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from engines.antigravity import (
    AntigravityAdapter,
    AntigravityHandshakeResult,
    AntigravityTelemetry,
    AntigravityToolExecutionRequest,
    compute_sha256_bytes,
    resolve_within_workspace,
    verify_artifact,
)

FULL_HELP = (
    "Usage of agy.exe:\n"
    "  --mode           Execution mode (accept-edits, auto, plan)\n"
    "  --output-format  Output format (text, json, stream-json)\n"
    "  --print          Run a single prompt\n"
    "  --print-timeout  Timeout\n"
    "  --sandbox        Run in sandbox\n"
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
    """Create a dummy executable path for mocked adapter tests."""
    binary = tmp_path / "agy.exe"
    binary.write_text("dummy", encoding="utf-8")
    return binary


@pytest.fixture
def fixture_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create an isolated temporary workspace with a protected package.json fixture."""
    ws = tmp_path / "p02_02_unit_ws"
    ws.mkdir()
    marker = "mk-unit-987654"
    pkg = {
        "name": "ai-village-p02-02-fixture",
        "version": "0.0.1",
        "private": True,
        "verification_marker": marker,
    }
    (ws / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    return ws, marker


def test_tool_execution_happy_path_verified_pass(
    fake_agy_binary: Path,
    fixture_workspace: tuple[Path, str],
) -> None:
    """Verify end-to-end tool execution checks existence, content, SHA-256, and source integrity."""
    ws, marker = fixture_workspace
    expected = f"name=ai-village-p02-02-fixture\nverification_marker={marker}\n"
    seen_commands: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_commands.append(cmd)
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout="", stderr=FULL_HELP)
        pkg_data = json.loads((ws / "package.json").read_text(encoding="utf-8"))
        content = (
            f"name={pkg_data['name']}\n"
            f"verification_marker={pkg_data['verification_marker']}\n"
        )
        (ws / "proof.txt").write_bytes(content.encode("utf-8"))
        return _completed(
            cmd,
            0,
            stdout=(
                '{"conversation_id":"conv-tool-1","status":"SUCCESS","response":"Done",'
                '"duration_seconds":2.0,"num_turns":1,"usage":{"input_tokens":50,'
                '"output_tokens":10,"total_tokens":60}}'
            ),
        )

    adapter = AntigravityAdapter(cli_path=str(fake_agy_binary), runner=runner)
    req = AntigravityToolExecutionRequest(
        prompt="Read package.json and write proof.txt.",
        workspace_path=str(ws),
        expected_artifact_relpath="proof.txt",
        source_files_to_protect=["package.json"],
        expected_content=expected,
        required_substrings=[marker],
    )
    res = adapter.execute_tool_task(req)

    assert res.status == "VERIFIED_PASS"
    assert res.error is None
    assert res.tool_events_note == "NOT OBSERVABLE FROM CURRENT CLI OUTPUT"
    assert res.verification.cli_success is True
    assert res.verification.artifact_exists is True
    assert res.verification.within_workspace is True
    assert res.verification.content_verified is True
    assert res.verification.hash_verified is True
    assert res.verification.source_integrity_verified is True
    assert res.verification.business_verified is True
    assert res.verification.actual_sha256 == compute_sha256_bytes(expected.encode("utf-8"))
    assert res.verification.actual_sha256 == res.verification.expected_sha256
    assert "--sandbox" in seen_commands[-1]
    assert "--mode" in seen_commands[-1]
    assert "accept-edits" in seen_commands[-1]
    assert "--dangerously-skip-permissions" not in seen_commands[-1]


def test_tool_execution_blocks_missing_cli_sandbox_and_mode(
    tmp_path: Path,
    fake_agy_binary: Path,
    fixture_workspace: tuple[Path, str],
) -> None:
    """Verify BLOCKED states when CLI is missing or lacks --sandbox/--mode flags."""
    ws, _ = fixture_workspace
    req = AntigravityToolExecutionRequest(
        prompt="Read package.json",
        workspace_path=str(ws),
        expected_artifact_relpath="proof.txt",
    )

    missing_res = AntigravityAdapter(cli_path=str(tmp_path / "missing.exe")).execute_tool_task(req)
    assert missing_res.status == "BLOCKED"
    assert missing_res.error is not None
    assert missing_res.error.error_code == "EXECUTABLE_NOT_FOUND"

    def no_sandbox_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        return _completed(cmd, 0, stdout="--print --output-format json --mode")

    no_sbx_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=no_sandbox_runner,
    ).execute_tool_task(req)
    assert no_sbx_res.status == "BLOCKED"
    assert no_sbx_res.error is not None
    assert no_sbx_res.error.error_code == "SANDBOX_UNVERIFIED"

    def no_mode_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        return _completed(cmd, 0, stdout="--print --output-format json --sandbox")

    no_mode_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=no_mode_runner,
    ).execute_tool_task(req)
    assert no_mode_res.status == "BLOCKED"
    assert no_mode_res.error is not None
    assert no_mode_res.error.error_code == "UNSUPPORTED_MODE"


def test_tool_execution_rejects_missing_workspace_and_path_traversal(
    tmp_path: Path,
    fake_agy_binary: Path,
    fixture_workspace: tuple[Path, str],
) -> None:
    """Verify workspace existence check and canonical path containment enforcement."""
    ws, _ = fixture_workspace
    adapter = AntigravityAdapter(cli_path=str(fake_agy_binary))

    bad_ws = adapter.execute_tool_task(
        AntigravityToolExecutionRequest(
            prompt="Read package.json",
            workspace_path=str(tmp_path / "does_not_exist"),
            expected_artifact_relpath="proof.txt",
        )
    )
    assert bad_ws.status == "FAILED"
    assert bad_ws.error is not None
    assert bad_ws.error.error_code == "PROCESS_LAUNCH_ERROR"

    escape_rel = adapter.execute_tool_task(
        AntigravityToolExecutionRequest(
            prompt="Write outside",
            workspace_path=str(ws),
            expected_artifact_relpath="../escape.txt",
        )
    )
    assert escape_rel.status == "FAILED"
    assert escape_rel.error is not None
    assert escape_rel.error.error_code == "ARTIFACT_OUTSIDE_WORKSPACE"
    assert escape_rel.verification.within_workspace is False

    with pytest.raises(ValueError):
        resolve_within_workspace(ws, str(tmp_path / "abs.txt"))


def test_tool_execution_detects_missing_artifact_wrong_content_hash_and_tampered_source(
    fake_agy_binary: Path,
    fixture_workspace: tuple[Path, str],
) -> None:
    """Verify CLI exit 0 does not pass when artifact is missing, wrong, or source changes."""
    ws, marker = fixture_workspace
    expected = f"name=ai-village-p02-02-fixture\nverification_marker={marker}\n"

    def text_only_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout=FULL_HELP)
        return _completed(cmd, 0, stdout='{"status":"SUCCESS","response":"I created it"}')

    missing_artifact_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=text_only_runner,
    ).execute_tool_task(
        AntigravityToolExecutionRequest(
            prompt="Create proof.txt",
            workspace_path=str(ws),
            expected_artifact_relpath="proof.txt",
            source_files_to_protect=["package.json"],
            expected_content=expected,
        )
    )
    assert missing_artifact_res.status == "VERIFIED_FAIL"
    assert missing_artifact_res.verification.cli_success is True
    assert missing_artifact_res.verification.artifact_exists is False
    assert missing_artifact_res.verification.business_verified is False
    assert missing_artifact_res.error is not None
    assert missing_artifact_res.error.error_code == "ARTIFACT_MISSING"

    def wrong_marker_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout=FULL_HELP)
        (ws / "proof.txt").write_text(
            "name=ai-village-p02-02-fixture\nverification_marker=wrong-marker\n",
            encoding="utf-8",
        )
        return _completed(cmd, 0, stdout='{"status":"SUCCESS","response":"Done"}')

    wrong_content_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=wrong_marker_runner,
    ).execute_tool_task(
        AntigravityToolExecutionRequest(
            prompt="Create proof.txt",
            workspace_path=str(ws),
            expected_artifact_relpath="proof.txt",
            source_files_to_protect=["package.json"],
            expected_content=expected,
            required_substrings=[marker],
        )
    )
    assert wrong_content_res.status == "VERIFIED_FAIL"
    assert wrong_content_res.verification.content_verified is False
    assert wrong_content_res.verification.hash_verified is False
    assert wrong_content_res.error is not None
    assert wrong_content_res.error.error_code == "CONTENT_MISMATCH"

    # Direct hash mismatch check with verify_artifact()
    now = datetime.now(timezone.utc)
    fake_cli = AntigravityHandshakeResult(
        status="SUCCEEDED",
        command_summary="agy",
        started_at=now,
        finished_at=now,
        duration_ms=10,
        exit_code=0,
        telemetry=AntigravityTelemetry(duration_ms=10, telemetry_status="UNAVAILABLE_FROM_PROVIDER"),
    )
    hash_check = verify_artifact(
        workspace_dir=ws,
        artifact_relpath="proof.txt",
        cli_result=fake_cli,
        expected_content=expected,
    )
    assert hash_check.hash_verified is False

    def tamper_source_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout=FULL_HELP)
        (ws / "package.json").write_text('{"tampered": true}\n', encoding="utf-8")
        (ws / "proof.txt").write_bytes(expected.encode("utf-8"))
        return _completed(cmd, 0, stdout='{"status":"SUCCESS","response":"Done"}')

    tamper_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=tamper_source_runner,
    ).execute_tool_task(
        AntigravityToolExecutionRequest(
            prompt="Create proof.txt",
            workspace_path=str(ws),
            expected_artifact_relpath="proof.txt",
            source_files_to_protect=["package.json"],
            expected_content=expected,
        )
    )
    assert tamper_res.status == "VERIFIED_FAIL"
    assert tamper_res.verification.source_integrity_verified is False
    assert tamper_res.error is not None
    assert tamper_res.error.error_code == "SOURCE_MODIFIED"


def test_tool_execution_handles_provider_error_timeout_denied_actions_and_redaction(
    fake_agy_binary: Path,
    fixture_workspace: tuple[Path, str],
) -> None:
    """Verify provider failures, timeouts, denied_actions scope violations, and redaction."""
    ws, _ = fixture_workspace
    secret_key = "AIza" + ("Z" * 35)

    def error_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout=FULL_HELP)
        return _completed(cmd, 1, stdout="", stderr=f"Provider error with key={secret_key}")

    err_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=error_runner,
    ).execute_tool_task(
        AntigravityToolExecutionRequest(
            prompt="Create proof.txt",
            workspace_path=str(ws),
            expected_artifact_relpath="proof.txt",
        )
    )
    assert err_res.status == "FAILED"
    assert err_res.error is not None
    assert err_res.error.error_code == "PROVIDER_ERROR"
    assert secret_key not in err_res.error.message
    assert secret_key not in err_res.cli_result.redacted_stderr

    def timeout_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout=FULL_HELP)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    to_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=timeout_runner,
    ).execute_tool_task(
        AntigravityToolExecutionRequest(
            prompt="Create proof.txt",
            workspace_path=str(ws),
            expected_artifact_relpath="proof.txt",
        )
    )
    assert to_res.status == "TIMED_OUT"
    assert to_res.error is not None
    assert to_res.error.error_code == "TIMED_OUT"

    def denied_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, 0, stdout="1.5.14\n")
        if "--help" in cmd:
            return _completed(cmd, 0, stdout=FULL_HELP)
        return _completed(
            cmd,
            0,
            stdout='{"status":"SUCCESS","response":"","denied_actions":[{"action":"command","display_name":"Bash"}]}',
        )

    denied_res = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=denied_runner,
    ).execute_tool_task(
        AntigravityToolExecutionRequest(
            prompt="Try command",
            workspace_path=str(ws),
            expected_artifact_relpath="proof.txt",
        )
    )
    assert denied_res.status == "VERIFIED_FAIL"
    assert denied_res.error is not None
    assert denied_res.error.error_code == "SCOPE_VIOLATION"

    with pytest.raises(ValidationError):
        AntigravityToolExecutionRequest(
            prompt="   ",
            workspace_path=str(ws),
            expected_artifact_relpath="proof.txt",
        )
