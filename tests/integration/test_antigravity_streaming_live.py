"""Live integration tests for P02.03 Antigravity CLI event streaming."""

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

import pytest

from engines.antigravity import (
    AntigravityAdapter,
    AntigravityHandshakeResult,
    AntigravityStreamEvent,
    AntigravityStreamParser,
    AntigravityStreamRequest,
    compute_sha256_bytes,
    run_streaming_process,
    snapshot_file_hashes,
    verify_artifact,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"


def _cleanup_workspace(ws: Path) -> None:
    """Remove temporary workspace after giving Windows background handles time to close."""
    if ws.exists():
        time.sleep(0.5)
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI streaming tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_antigravity_streaming_simple_response() -> None:
    """Verify real-time NDJSON events arrive before subprocess completion."""
    adapter = AntigravityAdapter()
    discovery = adapter.discover()
    assert discovery.found is True, f"Antigravity CLI not found: {discovery.error}"

    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    observed: list[tuple[str, str, datetime]] = []

    def _on_event(evt: AntigravityStreamEvent) -> None:
        observed.append((evt.event_type, evt.source, evt.timestamp))

    with tempfile.TemporaryDirectory(
        dir=str(workspace_root),
        prefix="p02_03_live_a_",
        ignore_cleanup_errors=True,
    ) as tmp:
        ws = Path(tmp)
        result = adapter.stream_task(
            AntigravityStreamRequest(
                prompt="Reply with exactly: AI_VILLAGE_STREAM_OK",
                workspace_path=str(ws),
                timeout_seconds=60,
                require_sandbox=True,
                execution_mode="accept-edits",
            ),
            on_event=_on_event,
        )
    _cleanup_workspace(ws)

    assert result.status == "SUCCEEDED", f"Live stream failed: {result.error}"
    assert result.exit_code == 0
    assert result.completion_event_received is True
    assert result.first_event_at is not None
    assert result.first_event_at < result.finished_at
    assert result.events[0].event_type == "process_started"
    assert result.events[0].source == "adapter"
    assert result.events[-1].event_type == "process_exited"
    assert result.events[-1].source == "adapter"

    provider_types = [evt.event_type for evt in result.events if evt.source == "provider_cli"]
    assert provider_types[0] == "execution_started"
    assert provider_types[-1] == "execution_completed"
    assert "AI_VILLAGE_STREAM_OK" in (result.response_text or "")


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI streaming tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_antigravity_streaming_tool_execution_and_sha256() -> None:
    """Verify live tool_call_* events and independent SHA-256 verification on disk."""
    adapter = AntigravityAdapter()
    discovery = adapter.discover()
    assert discovery.found is True, f"Antigravity CLI not found: {discovery.error}"

    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    marker = f"mk-{uuid.uuid4().hex[:12]}"
    pkg_payload = {
        "name": "ai-village-p02-03-fixture",
        "version": "0.0.1",
        "private": True,
        "verification_marker": marker,
    }
    expected_proof = (
        f"name={pkg_payload['name']}\n"
        f"verification_marker={marker}\n"
    )
    expected_sha256 = compute_sha256_bytes(expected_proof.encode("utf-8"))

    # Note: verification_marker is NEVER placed inside the prompt.
    prompt = (
        "Read package.json in the current workspace directory without modifying it. "
        "Extract the exact values of 'name' and 'verification_marker'. "
        "Do not run any shell or terminal commands; use only file read and file write tools. "
        "Create proof.txt in the current workspace directory with exactly two UTF-8 lines ending with LF:\n"
        "name=<name>\n"
        "verification_marker=<verification_marker>\n"
        "Then read proof.txt back to confirm its content and summarize the result."
    )

    callback_events: list[AntigravityStreamEvent] = []

    with tempfile.TemporaryDirectory(
        dir=str(workspace_root),
        prefix="p02_03_live_b_",
        ignore_cleanup_errors=True,
    ) as tmp:
        ws = Path(tmp)
        (ws / "package.json").write_text(
            json.dumps(pkg_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        protected_hashes = snapshot_file_hashes(ws, ["package.json"])

        result = adapter.stream_task(
            AntigravityStreamRequest(
                prompt=prompt,
                workspace_path=str(ws),
                timeout_seconds=90,
                require_sandbox=True,
                execution_mode="accept-edits",
            ),
            on_event=callback_events.append,
        )

        cli_proxy = AntigravityHandshakeResult(
            status="SUCCEEDED" if result.status == "SUCCEEDED" else "FAILED",
            executable_path=result.executable_path,
            version=result.version,
            command_summary=result.command_summary,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            exit_code=result.exit_code,
            response_text=result.response_text,
            provider_status=result.provider_status,
            conversation_id=result.conversation_id,
            model_id=result.model_id,
            num_turns=result.num_turns,
            telemetry=result.telemetry,
            error=result.error,
            denied_actions=result.denied_actions,
            redacted_stderr=result.redacted_stderr,
        )
        verification = verify_artifact(
            workspace_dir=ws,
            artifact_relpath="proof.txt",
            cli_result=cli_proxy,
            expected_content=expected_proof,
            required_substrings=[pkg_payload["name"], marker],
            protected_source_hashes=protected_hashes,
        )
    _cleanup_workspace(ws)

    assert result.status == "SUCCEEDED", f"Live tool stream failed: {result.error}"
    assert result.completion_event_received is True
    assert result.events == callback_events
    assert result.first_event_at is not None
    assert result.first_event_at < result.finished_at

    tool_started = [e for e in result.events if e.event_type == "tool_call_started"]
    tool_completed = [e for e in result.events if e.event_type == "tool_call_completed"]
    assert tool_started, "TOOL_EVENT_NOT_OBSERVABLE: no tool_call_started event received"
    assert tool_completed, "TOOL_EVENT_NOT_OBSERVABLE: no tool_call_completed event received"
    assert "read_file" in result.observed_tool_names
    assert "write_file" in result.observed_tool_names

    completed_seq = next(
        e.sequence_number for e in result.events if e.event_type == "execution_completed"
    )
    assert all(e.sequence_number < completed_seq for e in tool_started + tool_completed)
    assert all(e.source == "provider_cli" for e in tool_started + tool_completed)

    # Ensure allowlisting protects tool output and raw parameter values from leaking.
    for tool_evt in tool_started + tool_completed:
        serialized_payload = json.dumps(tool_evt.redacted_payload)
        assert marker not in serialized_payload
        assert "ai-village-p02-03-fixture" not in serialized_payload

    assert verification.business_verified is True, f"Verification failed: {verification.failure_reasons}"
    assert verification.hash_verified is True
    assert verification.source_integrity_verified is True
    assert verification.expected_sha256 == expected_sha256
    assert verification.actual_sha256 == expected_sha256


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI streaming tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_antigravity_streaming_nonzero_exit_failure() -> None:
    """Verify real CLI nonzero exit failure produces redacted adapter process events."""
    adapter = AntigravityAdapter()
    discovery = adapter.discover()
    assert discovery.found is True and discovery.executable_path

    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(
        dir=str(workspace_root),
        prefix="p02_03_live_c_",
        ignore_cleanup_errors=True,
    ) as tmp:
        ws = Path(tmp)
        parser = AntigravityStreamParser()
        outcome = run_streaming_process(
            command=[discovery.executable_path, "--invalid-p02-03-flag"],
            workspace=ws,
            timeout_seconds=15,
            parser=parser,
        )
    _cleanup_workspace(ws)

    assert outcome.timed_out is False
    assert outcome.exit_code is not None and outcome.exit_code != 0
    assert parser.completion_event_received is False
    assert outcome.redacted_stderr
    assert outcome.events[0].event_type == "process_started"
    assert outcome.events[0].source == "adapter"
    assert outcome.events[-1].event_type == "process_exited"
    assert outcome.events[-1].source == "adapter"
    assert outcome.events[-1].status == "FAILED"
