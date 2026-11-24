"""Unit tests for P02.05 Antigravity process lifecycle, cancellation, timeout, and errors."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any

import pytest

from engines.antigravity import (
    AntigravityAdapter,
    AntigravityProcessManager,
    AntigravitySessionConfig,
    AntigravitySessionManager,
    AntigravityStreamEvent,
    AntigravityStreamParser,
    AntigravityStreamRequest,
    ProcessLifecycleError,
    SessionIsolationError,
    run_streaming_process,
)
from engines.antigravity.adapter import _classify_cli_error


def _completed(
    args: list[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Create a synthetic CompletedProcess for probe mocking."""
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _probe_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Return full Antigravity capability flags for lifecycle unit tests."""
    if "--version" in cmd:
        return _completed(cmd, 0, stdout="1.5.15\n")
    if "--help" in cmd:
        return _completed(
            cmd,
            0,
            stderr="--print --print-timeout --output-format stream-json --sandbox --mode --conversation --settings",
        )
    raise AssertionError(f"Unexpected runner command: {cmd}")


class _QueuedPipe:
    """Thread-safe queue stream simulating incremental subprocess stdout."""

    def __init__(self) -> None:
        self._items: queue.Queue[str | None] = queue.Queue()
        self.closed = False

    def push(self, line: str) -> None:
        """Push one NDJSON line into stdout."""
        if not self.closed:
            self._items.put(line)

    def finish(self) -> None:
        """Signal EOF to the stdout reader."""
        self._items.put(None)

    def readline(self) -> str:
        item = self._items.get()
        return "" if item is None else item

    def close(self) -> None:
        self.closed = True
        self._items.put(None)


class _ControllableFakeProcess:
    """Fake Popen supporting OS PID simulation, waiting, termination, and kill fallback."""

    def __init__(
        self,
        *,
        stdout_pipe: _QueuedPipe | Any,
        stderr_text: str = "",
        pid: int = 41001,
        returncode_on_exit: int = 0,
        returncode_on_terminate: int | None = 1,
        refuse_terminate: bool = False,
        block_until_terminated: bool = False,
        timeout_on_wait: bool = False,
    ) -> None:
        self.stdout = stdout_pipe
        self.stderr = io.StringIO(stderr_text)
        self.pid = pid
        self.returncode: int | None = None
        self._exit_rc = returncode_on_exit
        self._term_rc = returncode_on_terminate if returncode_on_terminate is not None else returncode_on_exit
        self._refuse_terminate = refuse_terminate
        self._block_until_terminated = block_until_terminated
        self._timeout_on_wait = timeout_on_wait
        self._done_event = threading.Event()
        self.terminate_calls = 0
        self.kill_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        if self._timeout_on_wait and self.terminate_calls == 0 and self.kill_calls == 0:
            raise subprocess.TimeoutExpired(cmd=["agy"], timeout=timeout or 1)
        if self._block_until_terminated and not self._done_event.is_set():
            if not self._done_event.wait(timeout=timeout):
                raise subprocess.TimeoutExpired(cmd=["agy"], timeout=timeout or 1)
        self.returncode = self._term_rc if (self.terminate_calls or self.kill_calls) else self._exit_rc
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self._refuse_terminate:
            if isinstance(self.stdout, _QueuedPipe):
                self.stdout.finish()
            self._done_event.set()

    def kill(self) -> None:
        self.kill_calls += 1
        if isinstance(self.stdout, _QueuedPipe):
            self.stdout.close()
        self._done_event.set()


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    """Create a synthetic CLI binary path inside pytest tmp_path."""
    exe = tmp_path / "agy.exe"
    exe.write_text("dummy", encoding="utf-8")
    return exe


@pytest.fixture
def workspace_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create an allowed root and two isolated child workspace directories A and B."""
    root = tmp_path / "lifecycle_root"
    root.mkdir()
    ws_a = root / "session_A"
    ws_b = root / "session_B"
    ws_a.mkdir()
    ws_b.mkdir()
    return root, ws_a, ws_b


def test_lifecycle_start_status_active_tool_and_normal_completion(
    fake_binary: Path,
    workspace_dirs: tuple[Path, Path, Path],
) -> None:
    """Verify start, live RUNNING status, active_tool_name tracking, and clean SUCCEEDED."""
    _, ws_a, _ = workspace_dirs
    pipe = _QueuedPipe()
    fake_proc = _ControllableFakeProcess(stdout_pipe=pipe, pid=50123, returncode_on_exit=0)
    tool_started_seen = threading.Event()
    finish_allowed = threading.Event()

    adapter = AntigravityAdapter(
        cli_path=str(fake_binary),
        runner=_probe_runner,
        popen_factory=lambda _cmd, **_kw: fake_proc,
    )
    mgr = AntigravityProcessManager(adapter=adapter)

    def _on_event(evt: AntigravityStreamEvent) -> None:
        if evt.event_type == "tool_call_started":
            tool_started_seen.set()
            assert finish_allowed.wait(timeout=3.0)

    exec_id = mgr.start_execution(
        AntigravityStreamRequest(prompt="Check lifecycle", workspace_path=str(ws_a)),
        on_event=_on_event,
    )

    pipe.push(json.dumps({"event": "init", "conversation_id": "conv-lc-1", "init": {}}) + "\n")
    pipe.push(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-lc-1",
                    "step_index": 1,
                    "step_type": "tool",
                    "state": "ACTIVE",
                    "tool_name": "read_file",
                    "tool_info": {"name": "read_file", "parameters": {"Path": "a.txt"}},
                },
            }
        )
        + "\n"
    )
    assert tool_started_seen.wait(timeout=3.0)

    status_mid = mgr.get_execution_status(exec_id)
    assert status_mid.state == "RUNNING"
    assert status_mid.is_terminal is False
    assert status_mid.process_running is True
    assert status_mid.pid == 50123
    assert status_mid.active_tool_name == "read_file"
    assert status_mid.conversation_id == "conv-lc-1"
    assert status_mid.elapsed_ms >= 0

    pipe.push(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-lc-1",
                    "step_index": 1,
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "read_file",
                },
            }
        )
        + "\n"
    )
    pipe.push(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-lc-1",
                    "status": "SUCCESS",
                    "response": "Done",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            }
        )
        + "\n"
    )
    pipe.finish()
    finish_allowed.set()

    final_res = mgr.await_result(exec_id, timeout=5.0)
    assert final_res.status == "SUCCEEDED"
    assert final_res.execution_id == exec_id
    assert final_res.pid == 50123
    assert final_res.exit_code == 0

    status_end = mgr.get_execution_status(exec_id)
    assert status_end.state == "SUCCEEDED"
    assert status_end.is_terminal is True
    assert status_end.process_running is False
    assert status_end.active_tool_name is None

    # Requesting cancel after completion is idempotent and must NOT overwrite SUCCEEDED.
    post_cancel = mgr.request_cancel(exec_id)
    assert post_cancel.accepted is False
    assert post_cancel.already_terminal is True
    assert post_cancel.final_state == "SUCCEEDED"
    assert mgr.get_execution_status(exec_id).state == "SUCCEEDED"


def test_cancellation_while_running_double_cancel_zero_exit_and_kill_fallback(
    fake_binary: Path,
    workspace_dirs: tuple[Path, Path, Path],
) -> None:
    """Verify real cancellation, idempotency, kill fallback, and no false success on exit 0."""
    _, ws_a, _ = workspace_dirs
    pipe = _QueuedPipe()
    # Even if a killed process returns exit code 0, CANCELLED must NEVER become SUCCEEDED.
    fake_proc = _ControllableFakeProcess(
        stdout_pipe=pipe,
        pid=50222,
        returncode_on_terminate=0,
        refuse_terminate=True,
        block_until_terminated=True,
    )
    init_seen = threading.Event()

    adapter = AntigravityAdapter(
        cli_path=str(fake_binary),
        runner=_probe_runner,
        popen_factory=lambda _cmd, **_kw: fake_proc,
    )
    mgr = AntigravityProcessManager(adapter=adapter)

    exec_id = mgr.start_execution(
        AntigravityStreamRequest(prompt="Cancel me cleanly", workspace_path=str(ws_a)),
        owner_token="owner-A",
        grace_seconds=0.1,
        on_event=lambda e: init_seen.set() if e.event_type == "execution_started" else None,
    )
    pipe.push(json.dumps({"event": "init", "conversation_id": "conv-can", "init": {}}) + "\n")
    assert init_seen.wait(timeout=3.0)

    # Wrong owner cannot cancel someone else's execution.
    with pytest.raises(ProcessLifecycleError) as exc_own:
        mgr.request_cancel(exec_id, owner_token="wrong-owner")
    assert exc_own.value.error_code == "EXECUTION_CONFLICT"

    # First cancel + immediate double cancel while stopping must both succeed idempotently.
    first_outcome = mgr.request_cancel(exec_id, owner_token="owner-A", wait=False)
    second_outcome = mgr.request_cancel(exec_id, owner_token="owner-A", wait=True, wait_timeout=5.0)
    assert first_outcome.accepted is True
    assert first_outcome.idempotent_repeat is False
    assert second_outcome.final_state == "CANCELLED"

    res = mgr.await_result(exec_id, timeout=5.0)
    assert res.status == "CANCELLED"
    assert res.error is not None and res.error.error_code == "CANCELLED"
    assert res.error.error_source == "cancellation"
    assert res.termination_mechanism == "kill_fallback"
    assert fake_proc.terminate_calls >= 1 and fake_proc.kill_calls >= 1
    assert any(e.event_type == "process_cancelled" and e.source == "adapter" for e in res.events)

    # Cancel before process even spawns via cooperative cancel_event.
    cancel_evt = threading.Event()
    cancel_evt.set()
    parser = AntigravityStreamParser()
    pre_outcome = run_streaming_process(
        command=[str(fake_binary), "--print=hi"],
        workspace=ws_a,
        timeout_seconds=10,
        parser=parser,
        cancel_event=cancel_evt,
        popen_factory=lambda *_a, **_k: pytest.fail("Should not spawn process when pre-cancelled"),
    )
    assert pre_outcome.cancelled is True
    assert pre_outcome.termination_race_note == "cancelled_before_start"
    assert pre_outcome.events[0].event_type == "process_cancelled"


def test_timeout_timeout_vs_cancel_race_and_bounded_buffer_cleanup(
    fake_binary: Path,
    workspace_dirs: tuple[Path, Path, Path],
) -> None:
    """Verify hard timeout, completion-vs-cancel race, and bounded event overflow cleanup."""
    _, ws_a, _ = workspace_dirs

    # 1. Hard timeout terminates hung process cleanly.
    timeout_proc = _ControllableFakeProcess(
        stdout_pipe=_QueuedPipe(),
        pid=50333,
        timeout_on_wait=True,
        block_until_terminated=True,
    )
    adapter_to = AntigravityAdapter(
        cli_path=str(fake_binary),
        runner=_probe_runner,
        popen_factory=lambda _cmd, **_kw: timeout_proc,
    )
    mgr_to = AntigravityProcessManager(adapter=adapter_to)
    exec_to = mgr_to.start_execution(
        AntigravityStreamRequest(prompt="Time out fast", workspace_path=str(ws_a)),
        hard_timeout_seconds=0.15,
        grace_seconds=0.1,
    )
    res_to = mgr_to.await_result(exec_to, timeout=5.0)
    assert res_to.status == "TIMED_OUT"
    assert res_to.error is not None and res_to.error.error_code == "TIMED_OUT"
    assert timeout_proc.terminate_calls >= 1
    assert timeout_proc.stdout.closed is True

    # 2. Race: result:SUCCESS already received right before cancel fires -> keep SUCCEEDED.
    race_pipe = _QueuedPipe()
    race_proc = _ControllableFakeProcess(stdout_pipe=race_pipe, pid=50444, returncode_on_exit=0)
    ctrl_holder = None
    parser = AntigravityStreamParser()

    race_pipe.push(json.dumps({"event": "init", "conversation_id": "conv-race", "init": {}}) + "\n")
    race_pipe.push(
        json.dumps({"event": "result", "result": {"conversation_id": "conv-race", "status": "SUCCESS", "response": "WIN"}})
        + "\n"
    )
    race_pipe.finish()

    cancel_at_result = threading.Event()
    race_outcome = run_streaming_process(
        command=[str(fake_binary), "--output-format", "stream-json", "--print=race"],
        workspace=ws_a,
        timeout_seconds=10,
        parser=parser,
        cancel_event=cancel_at_result,
        on_event=lambda e: cancel_at_result.set() if e.event_type == "execution_completed" else None,
        popen_factory=lambda _cmd, **_kw: race_proc,
    )
    assert race_outcome.cancelled is False
    assert race_outcome.termination_race_note == "cancel_requested_after_completion"

    # 3. Bounded buffer capacity (max_events=4) emits single overflow event and drops excess while keeping terminal events.
    spam_lines = [json.dumps({"event": "init", "conversation_id": "conv-buf", "init": {}})]
    for idx in range(8):
        spam_lines.append(
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"step_index": idx, "step_type": "agent_response", "state": "ACTIVE", "text_delta": f"c{idx}"},
                }
            )
        )
    spam_lines.append(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "END"}}))
    spam_proc = _ControllableFakeProcess(stdout_pipe=io.StringIO("\n".join(spam_lines) + "\n"), pid=50555)
    buf_outcome = run_streaming_process(
        command=[str(fake_binary), "--print=spam"],
        workspace=ws_a,
        timeout_seconds=10,
        parser=AntigravityStreamParser(),
        max_events=4,
        popen_factory=lambda _cmd, **_kw: spam_proc,
    )
    assert buf_outcome.events_dropped_count > 0
    assert any(e.event_type == "event_buffer_overflow" for e in buf_outcome.events)
    assert buf_outcome.events[-2].event_type == "execution_completed"
    assert buf_outcome.events[-1].event_type == "process_exited"


def test_error_classification_redaction_and_missing_completion(
    fake_binary: Path,
    workspace_dirs: tuple[Path, Path, Path],
) -> None:
    """Verify AUTH_ERROR, QUOTA_EXCEEDED, RATE_LIMITED with retry_after, unknown error, and missing completion."""
    _, ws_a, _ = workspace_dirs

    err_auth = _classify_cli_error(1, "", "Error: authentication failed AIzaSecretKey123456789012345")
    assert err_auth.error_code == "AUTH_ERROR"
    assert err_auth.error_source == "provider"
    assert "AIzaSecretKey" not in err_auth.message
    assert "AIzaSecretKey" not in err_auth.redacted_stderr

    err_flag = _classify_cli_error(2, "", "flags provided but not defined: -bogus\nUsage of agy.exe:")
    assert err_flag.error_code == "UNSUPPORTED_ARGUMENT"
    assert err_flag.error_source == "cli"
    assert err_flag.retryable is False

    # Benign prose word "author" must NOT trigger AUTH_ERROR.
    err_benign = _classify_cli_error(1, "", "Plugin author contact unavailable api_key=leak_val_123")
    assert err_benign.error_code == "PROVIDER_ERROR"
    assert "leak_val_123" not in err_benign.redacted_stderr

    err_quota = _classify_cli_error(1, "", "Error: quota exceeded for billing project")
    assert err_quota.error_code == "QUOTA_EXCEEDED"
    assert err_quota.error_source == "provider"

    err_rate = _classify_cli_error(1, "", "HTTP 429 Too Many Requests: rate limit hit, retry-after: 12.5s")
    assert err_rate.error_code == "RATE_LIMITED"
    assert err_rate.retryable is True
    assert err_rate.retry_after_seconds == 12.5

    err_rate_no_delay = _classify_cli_error(1, "", "HTTP 429 rate limit hit")
    assert err_rate_no_delay.error_code == "RATE_LIMITED"
    assert err_rate_no_delay.retry_after_seconds is None

    # Clean exit_code == 0 without terminal completion event -> MISSING_COMPLETION_EVENT + stream_interrupted.
    partial_proc = _ControllableFakeProcess(
        stdout_pipe=io.StringIO(json.dumps({"event": "init", "conversation_id": "conv-part", "init": {}}) + "\n"),
        returncode_on_exit=0,
    )
    adapter = AntigravityAdapter(
        cli_path=str(fake_binary),
        runner=_probe_runner,
        popen_factory=lambda _cmd, **_kw: partial_proc,
    )
    res_partial = adapter.stream_task(
        AntigravityStreamRequest(prompt="Incomplete stream", workspace_path=str(ws_a))
    )
    assert res_partial.status == "FAILED"
    assert res_partial.error is not None
    assert res_partial.error.error_code == "MISSING_COMPLETION_EVENT"
    assert res_partial.error.error_source == "adapter"
    assert any(e.event_type == "stream_interrupted" for e in res_partial.events)


def test_cross_session_cancellation_and_cleanup_guard(
    fake_binary: Path,
    workspace_dirs: tuple[Path, Path, Path],
) -> None:
    """Verify cancelling Session A keeps Session B alive and close_session protects active turns."""
    root, ws_a, ws_b = workspace_dirs
    pipe_a = _QueuedPipe()
    pipe_b = _QueuedPipe()
    proc_a = _ControllableFakeProcess(stdout_pipe=pipe_a, pid=61001, block_until_terminated=True)
    proc_b = _ControllableFakeProcess(stdout_pipe=pipe_b, pid=61002, returncode_on_exit=0)

    def _popen(_cmd: list[str], cwd: str | None = None, **_kwargs: Any) -> _ControllableFakeProcess:
        if Path(cwd or "").resolve() == ws_a.resolve():
            return proc_a
        return proc_b

    adapter = AntigravityAdapter(
        cli_path=str(fake_binary),
        runner=_probe_runner,
        popen_factory=_popen,
    )
    session_mgr = AntigravitySessionManager(adapter=adapter)
    session_mgr.create_session(
        AntigravitySessionConfig(
            session_id="sess_A",
            workspace_path=str(ws_a),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )
    session_mgr.create_session(
        AntigravitySessionConfig(
            session_id="sess_B",
            workspace_path=str(ws_b),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )

    init_a = threading.Event()
    init_b = threading.Event()
    exec_a = session_mgr.start_turn(
        "sess_A",
        "Run A",
        on_event=lambda e: init_a.set() if e.event.event_type == "execution_started" else None,
    )
    exec_b = session_mgr.start_turn(
        "sess_B",
        "Run B",
        on_event=lambda e: init_b.set() if e.event.event_type == "execution_started" else None,
    )

    pipe_a.push(json.dumps({"event": "init", "conversation_id": "conv-A", "init": {}}) + "\n")
    pipe_b.push(json.dumps({"event": "init", "conversation_id": "conv-B", "init": {}}) + "\n")
    assert init_a.wait(timeout=3.0) and init_b.wait(timeout=3.0)

    # Attempting to close/delete Session A while exec_a is active MUST fail closed unless cancel_active=True.
    with pytest.raises(SessionIsolationError) as exc_busy:
        session_mgr.close_session("sess_A", cleanup_workspace=True, cancel_active=False)
    assert exc_busy.value.error_code == "EXECUTION_CONFLICT"
    assert ws_a.exists()

    # Cancel ONLY Session A; verify Session B continues unaffected and SUCCEEDS.
    cancel_a = session_mgr.cancel_turn(exec_a, wait=True, wait_timeout=5.0)
    assert cancel_a.final_state == "CANCELLED"
    turn_a = session_mgr.await_turn(exec_a, timeout=5.0)
    assert turn_a.stream_result.status == "CANCELLED"
    assert proc_a.terminate_calls >= 1
    assert proc_b.terminate_calls == 0

    pipe_b.push(
        json.dumps(
            {"event": "result", "result": {"conversation_id": "conv-B", "status": "SUCCESS", "response": "B_OK"}}
        )
        + "\n"
    )
    pipe_b.finish()
    turn_b = session_mgr.await_turn(exec_b, timeout=5.0)
    assert turn_b.stream_result.status == "SUCCEEDED"
    assert turn_b.provider_conversation_id == "conv-B"

    # Now verify cancel_active=True cleanly stops an active turn and cleans ONLY its own workspace.
    pipe_a2 = _QueuedPipe()
    proc_a2 = _ControllableFakeProcess(stdout_pipe=pipe_a2, pid=61003, block_until_terminated=True)
    # Rebind for second turn on sess_A if closed? Let's test on a fresh session sess_C!
    ws_c = root / "session_C"
    ws_c.mkdir()
    adapter._popen_factory = lambda _c, **_k: proc_a2  # type: ignore[assignment]
    session_mgr.create_session(
        AntigravitySessionConfig(
            session_id="sess_C",
            workspace_path=str(ws_c),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )
    init_c = threading.Event()
    session_mgr.start_turn(
        "sess_C",
        "Run C",
        on_event=lambda e: init_c.set() if e.event.event_type == "execution_started" else None,
    )
    pipe_a2.push(json.dumps({"event": "init", "conversation_id": "conv-C", "init": {}}) + "\n")
    assert init_c.wait(timeout=3.0)

    session_mgr.close_session("sess_C", cleanup_workspace=True, cancel_active=True)
    assert proc_a2.terminate_calls >= 1
    assert not ws_c.exists()
    assert ws_b.exists()

    with pytest.raises(ProcessLifecycleError):
        session_mgr.get_turn_status("exec-does-not-exist")
