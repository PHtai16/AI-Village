"""Live integration tests for P02.05 Antigravity process lifecycle and cancellation."""

import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import uuid

import pytest

from engines.antigravity import (
    AntigravityAdapter,
    AntigravityProcessManager,
    AntigravitySessionConfig,
    AntigravitySessionEvent,
    AntigravitySessionManager,
    AntigravityStreamEvent,
    AntigravityStreamParser,
    AntigravityStreamRequest,
    compute_sha256_bytes,
    is_pid_alive,
    run_streaming_process,
    snapshot_file_hashes,
)
from engines.antigravity.adapter import _classify_cli_error

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"


def _create_lifecycle_root(prefix: str) -> tuple[Path, Path]:
    """Create a non-temp fixture directory under ``workspace/p02_05_lifecycle/``."""
    ws_root = REPO_ROOT / "workspace"
    ws_root.mkdir(exist_ok=True)
    lc_base = ws_root / "p02_05_lifecycle"
    lc_base.mkdir(exist_ok=True)
    run_dir = lc_base / f"{prefix}_{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return lc_base, run_dir


def _cleanup_lifecycle_root(lc_base: Path, run_dir: Path) -> None:
    """Remove owned test fixture directories after processes exit."""
    if run_dir.exists():
        time.sleep(0.3)
        shutil.rmtree(run_dir, ignore_errors=True)
    if lc_base.exists():
        try:
            if not any(lc_base.iterdir()):
                lc_base.rmdir()
        except OSError:
            pass


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI lifecycle tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_lifecycle_normal_execution() -> None:
    """Test A: Start real CLI execution, inspect live RUNNING state and PID, then clean exit."""
    lc_base, run_dir = _create_lifecycle_root("normal")
    adapter = AntigravityAdapter()
    mgr = AntigravityProcessManager(adapter=adapter)
    started_evt = threading.Event()
    observed_mid_states: list[tuple[str, bool, int | None]] = []

    try:
        def _on_event(evt: AntigravityStreamEvent) -> None:
            if evt.event_type == "execution_started":
                started_evt.set()

        exec_id = mgr.start_execution(
            AntigravityStreamRequest(
                prompt="Reply with exactly: AI_VILLAGE_LIFECYCLE_OK",
                workspace_path=str(run_dir),
                timeout_seconds=60,
            ),
            on_event=_on_event,
        )
        assert started_evt.wait(timeout=45.0), "CLI did not emit execution_started in time"
        mid = mgr.get_execution_status(exec_id)
        observed_mid_states.append((mid.state, mid.process_running, mid.pid))

        result = mgr.await_result(exec_id, timeout=75.0)
        end = mgr.get_execution_status(exec_id)
    finally:
        _cleanup_lifecycle_root(lc_base, run_dir)

    assert observed_mid_states
    assert result.status == "SUCCEEDED", f"Live lifecycle execution failed: {result.error}"
    assert result.exit_code == 0
    assert result.pid is not None and result.pid > 0
    assert end.state == "SUCCEEDED"
    assert end.is_terminal is True
    assert end.process_running is False
    assert not is_pid_alive(result.pid)
    assert "AI_VILLAGE_LIFECYCLE_OK" in (result.response_text or "")


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI lifecycle tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_lifecycle_real_cancellation() -> None:
    """Test B: Confirm RUNNING + live PID via init event, cancel, and verify tree termination."""
    lc_base, run_dir = _create_lifecycle_root("cancel")
    mgr = AntigravityProcessManager(adapter=AntigravityAdapter())
    init_seen = threading.Event()

    try:
        exec_id = mgr.start_execution(
            AntigravityStreamRequest(
                prompt=(
                    "Write a detailed 15-item numbered technical guide explaining TCP three-way "
                    "handshakes, TLS 1.3 key exchange, and congestion control with two paragraphs "
                    "for every item. Do not run any shell or file tools."
                ),
                workspace_path=str(run_dir),
                timeout_seconds=90,
            ),
            on_event=lambda e: init_seen.set() if e.event_type == "execution_started" else None,
        )
        assert init_seen.wait(timeout=45.0), "Live CLI never emitted execution_started"

        status_before = mgr.get_execution_status(exec_id)
        assert status_before.state == "RUNNING", (
            f"Process already finished before cancel could be tested: {status_before.state}"
        )
        assert status_before.process_running is True
        assert status_before.pid is not None and status_before.pid > 0
        live_pid = status_before.pid

        cancel_outcome = mgr.request_cancel(exec_id, wait=True, wait_timeout=15.0)
        result = mgr.await_result(exec_id, timeout=15.0)
        end_status = mgr.get_execution_status(exec_id)

        assert cancel_outcome.accepted is True
        assert cancel_outcome.final_state == "CANCELLED"
        assert result.status == "CANCELLED"
        assert result.error is not None and result.error.error_code == "CANCELLED"
        assert result.error.error_source == "cancellation"
        assert result.pid == live_pid
        assert end_status.is_terminal is True and end_status.process_running is False
        assert not is_pid_alive(live_pid)
        if os.name == "nt":
            assert result.process_tree_terminated is True
            assert result.termination_mechanism == "win32_job_object"
    finally:
        _cleanup_lifecycle_root(lc_base, run_dir)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI lifecycle tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_lifecycle_cross_session_cancellation() -> None:
    """Test C: Cancel running Session A while concurrent Session B finishes SUCCEEDED + SHA-256."""
    lc_base, run_dir = _create_lifecycle_root("cross")
    ws_a = run_dir / "session_A"
    ws_b = run_dir / "session_B"
    ws_a.mkdir()
    ws_b.mkdir()

    b_marker = f"LC_B_{uuid.uuid4().hex[:10]}"
    (ws_b / "package.json").write_text(
        json.dumps({"name": "ai-village-p02-05-b", "version": "0.0.1", "private": True}, indent=2) + "\n",
        encoding="utf-8",
    )
    (ws_b / "b_private.txt").write_text(f"b_marker={b_marker}\n", encoding="utf-8")
    hashes_b = snapshot_file_hashes(ws_b, ["package.json", "b_private.txt"])
    expected_b = f"name=ai-village-p02-05-b\nb_marker={b_marker}\n"
    expected_sha_b = compute_sha256_bytes(expected_b.encode("utf-8"))

    session_mgr = AntigravitySessionManager(adapter=AntigravityAdapter())
    init_a = threading.Event()
    init_b = threading.Event()
    events_b: list[AntigravitySessionEvent] = []

    try:
        session_mgr.create_session(
            AntigravitySessionConfig(
                session_id="live_lc_A",
                workspace_path=str(ws_a),
                allowed_root_path=str(run_dir),
                timeout_seconds=90,
            )
        )
        session_mgr.create_session(
            AntigravitySessionConfig(
                session_id="live_lc_B",
                workspace_path=str(ws_b),
                allowed_root_path=str(run_dir),
                timeout_seconds=90,
            )
        )

        exec_a = session_mgr.start_turn(
            "live_lc_A",
            "Write a detailed 15-item numbered technical guide about OS kernels and memory "
            "paging with two paragraphs per item. Do not run shell or file tools.",
            on_event=lambda e: init_a.set() if e.event.event_type == "execution_started" else None,
        )
        exec_b = session_mgr.start_turn(
            "live_lc_B",
            "Read package.json and b_private.txt in your workspace without modifying them. "
            "Do not run shell commands. Create proof_B.txt with two UTF-8 LF lines:\n"
            "name=<package_json_name>\n"
            "b_marker=<value_after_equals_in_b_private>\n"
            "Then read proof_B.txt back to confirm.",
            on_event=lambda e: (events_b.append(e), init_b.set() if e.event.event_type == "execution_started" else None),
        )

        assert init_a.wait(timeout=45.0) and init_b.wait(timeout=45.0), (
            "Both concurrent sessions must emit execution_started before targeted cancel"
        )
        stat_a = session_mgr.get_turn_status(exec_a)
        stat_b = session_mgr.get_turn_status(exec_b)
        assert stat_a.pid is not None and stat_b.pid is not None
        assert stat_a.pid != stat_b.pid
        assert stat_a.state == "RUNNING"

        cancel_a = session_mgr.cancel_turn(exec_a, wait=True, wait_timeout=15.0)
        turn_a = session_mgr.await_turn(exec_a, timeout=15.0)
        turn_b = session_mgr.await_turn(exec_b, timeout=105.0)

        assert cancel_a.final_state == "CANCELLED"
        assert turn_a.stream_result.status == "CANCELLED"
        assert not is_pid_alive(stat_a.pid)

        assert turn_b.stream_result.status == "SUCCEEDED", f"Session B disrupted: {turn_b.stream_result.error}"
        assert not is_pid_alive(stat_b.pid)
        assert all(e.session_id == "live_lc_B" for e in events_b)
        ver_b = session_mgr.verify_session_artifact(
            "live_lc_B",
            artifact_relpath="proof_B.txt",
            turn_result=turn_b,
            expected_content=expected_b,
            required_substrings=["ai-village-p02-05-b", b_marker],
            protected_source_hashes=hashes_b,
        )
        assert ver_b.business_verified and ver_b.expected_sha256 == expected_sha_b and ver_b.actual_sha256 == expected_sha_b

        session_mgr.close_session("live_lc_A", cleanup_workspace=True)
        session_mgr.close_session("live_lc_B", cleanup_workspace=True)
    finally:
        _cleanup_lifecycle_root(lc_base, run_dir)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI lifecycle tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_lifecycle_controlled_process_timeout() -> None:
    """Test D: Verify real owned OS subprocess timeout kills process tree and cleans up."""
    lc_base, run_dir = _create_lifecycle_root("timeout")
    try:
        parser = AntigravityStreamParser()
        sleep_cmd = [
            sys.executable,
            "-c",
            "import time; print('{\"event\":\"init\",\"init\":{}}', flush=True); time.sleep(30)",
        ]
        outcome = run_streaming_process(
            command=sleep_cmd,
            workspace=run_dir,
            timeout_seconds=2,
            parser=parser,
            hard_timeout_seconds=1.5,
            grace_seconds=0.5,
        )
        assert outcome.timed_out is True
        assert outcome.cancelled is False
        assert outcome.pid is not None and outcome.pid > 0
        assert not is_pid_alive(outcome.pid)
        assert outcome.events[-1].event_type == "process_timed_out"
        if os.name == "nt":
            assert outcome.process_tree_terminated is True
    finally:
        _cleanup_lifecycle_root(lc_base, run_dir)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI lifecycle tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_lifecycle_cli_error_classification() -> None:
    """Test E: Verify live agy.exe invalid-flag exit maps to UNSUPPORTED_ARGUMENT from cli."""
    adapter = AntigravityAdapter()
    discovery = adapter.discover()
    assert discovery.found and discovery.executable_path

    lc_base, run_dir = _create_lifecycle_root("err")
    try:
        parser = AntigravityStreamParser()
        outcome = run_streaming_process(
            command=[discovery.executable_path, "--invalid-p02-05-flag"],
            workspace=run_dir,
            timeout_seconds=15,
            parser=parser,
        )
        assert outcome.exit_code is not None and outcome.exit_code != 0
        err = _classify_cli_error(
            outcome.exit_code,
            outcome.redacted_stdout_diagnostics,
            outcome.redacted_stderr,
        )
        assert err.error_code == "UNSUPPORTED_ARGUMENT"
        assert err.error_source == "cli"
        assert err.retryable is False
    finally:
        _cleanup_lifecycle_root(lc_base, run_dir)
