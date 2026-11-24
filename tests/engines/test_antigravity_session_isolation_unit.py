"""Unit tests for P02.04 Antigravity session isolation and leakage prevention."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import queue
import subprocess
import threading
from typing import Any

from pydantic import ValidationError
import pytest

from engines.antigravity import (
    AntigravityAdapter,
    AntigravitySessionConfig,
    AntigravitySessionEvent,
    AntigravitySessionManager,
    AntigravityStreamRequest,
    SessionIsolationError,
    build_ephemeral_deny_settings,
    format_command_summary,
    normalize_path_for_agy_permission,
    resolve_within_workspace,
)


def _completed(
    args: list[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Create a synthetic CompletedProcess for capability probe mocking."""
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _make_session_probe_runner(
    *,
    help_text: str = (
        "Usage:\n"
        "  --print\n"
        "  --print-timeout\n"
        "  --output-format (text, json, stream-json)\n"
        "  --sandbox\n"
        "  --mode (accept-edits, auto, plan)\n"
        "  --conversation\n"
        "  --settings\n"
    ),
    version: str = "1.5.15",
) -> Any:
    """Return a mock subprocess.run callable reporting CLI session capabilities."""

    def _runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, returncode=0, stdout=f"{version}\n")
        if "--help" in cmd:
            return _completed(cmd, returncode=0, stderr=help_text)
        raise AssertionError(f"Unexpected runner call: {cmd}")

    return _runner


class _FakeStreamProcess:
    """Mock Popen process serving preconfigured NDJSON stdout and stderr streams."""

    def __init__(
        self,
        *,
        stdout_stream: Any,
        stderr_text: str = "",
        returncode: int = 0,
        timeout_on_wait: bool = False,
    ) -> None:
        self.stdout = stdout_stream
        self.stderr = io.StringIO(stderr_text)
        self.returncode: int | None = None
        self._final_rc = returncode
        self._timeout_on_wait = timeout_on_wait
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self._timeout_on_wait and not self.terminated and not self.killed:
            raise subprocess.TimeoutExpired(cmd=["agy"], timeout=timeout or 1)
        self.returncode = self._final_rc
        return self._final_rc

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _QueuedStream:
    """Thread-safe queue-backed line stream to interleave concurrent A/B events."""

    def __init__(self) -> None:
        self._items: queue.Queue[str | None] = queue.Queue()

    def push(self, line: str) -> None:
        """Enqueue a line ending in newline."""
        self._items.put(line)

    def close_stream(self) -> None:
        """Signal EOF to reader thread."""
        self._items.put(None)

    def readline(self) -> str:
        item = self._items.get()
        return "" if item is None else item

    def close(self) -> None:
        pass


@pytest.fixture
def fake_agy_binary(tmp_path: Path) -> Path:
    """Create a synthetic CLI binary path inside pytest tmp_path."""
    exe = tmp_path / "agy.exe"
    exe.write_text("dummy", encoding="utf-8")
    return exe


@pytest.fixture
def session_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create an allowed root and two isolated child workspace directories A and B."""
    root = tmp_path / "sessions_root"
    root.mkdir()
    ws_a = root / "session_A"
    ws_b = root / "session_B"
    ws_a.mkdir()
    ws_b.mkdir()
    return root, ws_a, ws_b


def test_session_id_validation_duplicate_and_workspace_boundaries(
    session_dirs: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    """Verify ID validation, duplicate checks, non-temp default rejection, and overlaps."""
    root, ws_a, ws_b = session_dirs

    for bad_id in ("ab", "bad id", "../esc", "sess/a", "a..b", "AIzaSecretKey12345678901234"):
        with pytest.raises(ValidationError):
            AntigravitySessionConfig(
                session_id=bad_id,
                workspace_path=str(ws_a),
                allowed_root_path=str(root),
                allow_temp_workspace=True,
            )

    with pytest.raises(ValidationError):
        AntigravitySessionConfig(
            session_id="valid_session",
            workspace_path="   ",
            allowed_root_path=str(root),
        )

    manager = AntigravitySessionManager()

    # Default allow_temp_workspace=False must reject workspaces inside OS %TEMP%.
    with pytest.raises(SessionIsolationError) as exc_temp:
        manager.create_session(
            AntigravitySessionConfig(
                session_id="temp_rejected",
                workspace_path=str(ws_a),
                allowed_root_path=str(root),
                allow_temp_workspace=False,
            )
        )
    assert exc_temp.value.error_code == "INVALID_WORKSPACE_BOUNDARY"

    # Root directory itself cannot be used as a session workspace.
    with pytest.raises(SessionIsolationError) as exc_root:
        manager.create_session(
            AntigravitySessionConfig(
                session_id="root_self",
                workspace_path=str(root),
                allowed_root_path=str(root),
                allow_temp_workspace=True,
            )
        )
    assert exc_root.value.error_code == "INVALID_WORKSPACE_BOUNDARY"

    # Workspace outside allowed_root must be rejected.
    outside_ws = tmp_path / "outside"
    outside_ws.mkdir()
    with pytest.raises(SessionIsolationError) as exc_out:
        manager.create_session(
            AntigravitySessionConfig(
                session_id="outside_root",
                workspace_path=str(outside_ws),
                allowed_root_path=str(root),
                allow_temp_workspace=True,
            )
        )
    assert exc_out.value.error_code == "INVALID_WORKSPACE_BOUNDARY"

    ctx_a = manager.create_session(
        AntigravitySessionConfig(
            session_id="session_A",
            workspace_path=str(ws_a),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )
    assert ctx_a.session_id == "session_A"
    assert ctx_a.provider_conversation_id is None
    assert ctx_a.conversation_binding_source == "none"

    # Duplicate session ID fails cleanly.
    with pytest.raises(SessionIsolationError) as exc_dup:
        manager.create_session(
            AntigravitySessionConfig(
                session_id="session_A",
                workspace_path=str(ws_b),
                allowed_root_path=str(root),
                allow_temp_workspace=True,
            )
        )
    assert exc_dup.value.error_code == "DUPLICATE_SESSION_ID"

    # Nested workspace overlapping active session A must be rejected.
    nested_in_a = ws_a / "sub"
    nested_in_a.mkdir()
    with pytest.raises(SessionIsolationError) as exc_overlap:
        manager.create_session(
            AntigravitySessionConfig(
                session_id="session_nested",
                workspace_path=str(nested_in_a),
                allowed_root_path=str(root),
                allow_temp_workspace=True,
            )
        )
    assert exc_overlap.value.error_code == "WORKSPACE_OVERLAP"


def test_path_traversal_and_symlink_junction_rejection(
    monkeypatch: pytest.MonkeyPatch,
    session_dirs: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    """Verify traversal rejection and symlink/junction detection in verification & sessions."""
    root, ws_a, ws_b = session_dirs

    with pytest.raises(ValueError):
        resolve_within_workspace(ws_a, "../session_B/secret.txt")
    with pytest.raises(ValueError):
        resolve_within_workspace(ws_a, str(ws_b / "secret.txt"))

    # Deterministic check simulating an NTFS junction or symlink component inside root.
    link_ws = root / "link_session"
    link_ws.mkdir()
    outside_target = tmp_path / "outside_target"
    outside_target.mkdir()

    monkeypatch.setattr(
        "engines.antigravity.verification.is_symlink_or_junction",
        lambda path: path.name == "link_session",
    )
    manager = AntigravitySessionManager()
    with pytest.raises(SessionIsolationError) as exc_sym:
        manager.create_session(
            AntigravitySessionConfig(
                session_id="sym_session",
                workspace_path=str(link_ws),
                allowed_root_path=str(root),
                allow_temp_workspace=True,
            )
        )
    assert exc_sym.value.error_code == "SYMLINK_ESCAPE_REJECTED"

    monkeypatch.undo()
    (ws_a / "escape_dir").mkdir()
    monkeypatch.setattr(
        "engines.antigravity.verification.is_symlink_or_junction",
        lambda path: path.name == "escape_dir",
    )
    with pytest.raises(ValueError) as exc_ver_sym:
        resolve_within_workspace(ws_a, "escape_dir/file.txt")
    assert "SYMLINK_ESCAPE_REJECTED" in str(exc_ver_sym.value)


def test_conversation_id_mapping_resume_missing_and_collision_detection(
    fake_agy_binary: Path,
    session_dirs: tuple[Path, Path, Path],
) -> None:
    """Verify fresh vs resumed --conversation binding, missing ID, and collision guard."""
    root, ws_a, ws_b = session_dirs
    seen_commands: list[list[str]] = []
    stream_batches: list[str] = [
        "\n".join(
            [
                json.dumps({"event": "init", "conversation_id": "conv-A", "init": {}}),
                json.dumps({"event": "result", "result": {"conversation_id": "conv-A", "status": "SUCCESS", "response": "ACK_A"}}),
            ]
        ),
        # Turn 2: Resuming A with --conversation conv-A.
        "\n".join(
            [
                json.dumps({"event": "init", "conversation_id": "conv-A", "init": {}}),
                json.dumps({"event": "result", "result": {"conversation_id": "conv-A", "status": "SUCCESS", "response": "RECALLED"}}),
            ]
        ),
        # Turn 3: Session B emits NO conversation_id -> must stay None / binding "none".
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "NO_CONV"}}),
        # Turn 4: Session B unexpectedly returns Session A's conversation_id -> violation!
        "\n".join(
            [
                json.dumps({"event": "init", "conversation_id": "conv-A", "init": {}}),
                json.dumps({"event": "result", "result": {"conversation_id": "conv-A", "status": "SUCCESS", "response": "COLLISION"}}),
            ]
        ),
    ]

    def _popen(cmd: list[str], **_kwargs: Any) -> _FakeStreamProcess:
        seen_commands.append(cmd)
        payload = stream_batches.pop(0)
        return _FakeStreamProcess(stdout_stream=io.StringIO(payload), returncode=0)

    adapter = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_session_probe_runner(),
        popen_factory=_popen,
    )
    manager = AntigravitySessionManager(adapter=adapter)
    manager.create_session(
        AntigravitySessionConfig(
            session_id="session_A",
            workspace_path=str(ws_a),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )
    manager.create_session(
        AntigravitySessionConfig(
            session_id="session_B",
            workspace_path=str(ws_b),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )

    # Cannot resume before a conversation_id is observed.
    with pytest.raises(SessionIsolationError) as exc_no_conv:
        manager.run_turn("session_A", "Resume prematurely", resume_conversation=True)
    assert exc_no_conv.value.error_code == "CROSS_SESSION_CONVERSATION_VIOLATION"

    turn_a1 = manager.run_turn("session_A", "Turn 1 on A")
    assert turn_a1.provider_conversation_id == "conv-A"
    assert turn_a1.resumed_conversation is False
    assert "--continue" not in seen_commands[0] and "-c" not in seen_commands[0]
    assert "--conversation" not in seen_commands[0]
    assert "--settings" in seen_commands[0]
    settings_json = json.loads(seen_commands[0][seen_commands[0].index("--settings") + 1])
    norm_b = normalize_path_for_agy_permission(ws_b)
    assert f"read_file({norm_b})" in settings_json["permissions"]["deny"]
    assert f"write_file({norm_b})" in settings_json["permissions"]["deny"]
    assert "command(*)" in settings_json["permissions"]["deny"]

    # Turn 2 resumes A explicitly using --conversation conv-A (never --continue).
    turn_a2 = manager.run_turn("session_A", "Turn 2 on A", resume_conversation=True)
    assert turn_a2.resumed_conversation is True
    assert turn_a2.provider_conversation_id == "conv-A"
    assert "--conversation" in seen_commands[1]
    assert seen_commands[1][seen_commands[1].index("--conversation") + 1] == "conv-A"
    assert "--continue" not in seen_commands[1]

    # Turn 3 on B without conversation_id keeps conversation_binding_source="none".
    turn_b1 = manager.run_turn("session_B", "Turn 1 on B")
    assert turn_b1.provider_conversation_id is None
    assert manager.get_session("session_B").conversation_binding_source == "none"

    # Turn 4 on B returning conv-A must flag CROSS_SESSION_CONVERSATION_VIOLATION.
    turn_b2 = manager.run_turn("session_B", "Turn 2 collision on B")
    assert turn_b2.isolation_error is not None
    assert turn_b2.isolation_error.error_code == "CROSS_SESSION_CONVERSATION_VIOLATION"


def test_concurrent_ab_event_correlation_callback_and_parser_isolation(
    fake_agy_binary: Path,
    session_dirs: tuple[Path, Path, Path],
) -> None:
    """Verify concurrent A/B threads never cross-mix callbacks, parser state, or tokens."""
    root, ws_a, ws_b = session_dirs
    stream_a = _QueuedStream()
    stream_b = _QueuedStream()
    barrier = threading.Barrier(2, timeout=5.0)

    def _popen(cmd: list[str], cwd: str | None = None, **_kwargs: Any) -> _FakeStreamProcess:
        resolved_cwd = Path(cwd or "").resolve()
        if resolved_cwd == ws_a.resolve():
            return _FakeStreamProcess(stdout_stream=stream_a, returncode=0)
        return _FakeStreamProcess(stdout_stream=stream_b, returncode=0)

    adapter = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_session_probe_runner(),
        popen_factory=_popen,
    )
    manager = AntigravitySessionManager(adapter=adapter)
    manager.create_session(
        AntigravitySessionConfig(
            session_id="session_A",
            workspace_path=str(ws_a),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )
    manager.create_session(
        AntigravitySessionConfig(
            session_id="session_B",
            workspace_path=str(ws_b),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )

    # Interleave lines into A and B simultaneously; A has a parse error, B does not.
    stream_a.push(json.dumps({"event": "init", "conversation_id": "conv-conc-A", "init": {}}) + "\n")
    stream_b.push(json.dumps({"event": "init", "conversation_id": "conv-conc-B", "init": {}}) + "\n")
    stream_a.push("{broken_json_only_in_A\n")
    stream_a.push(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-conc-A",
                    "step_index": 1,
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "read_file",
                    "tool_info": {"name": "read_file", "parameters": {"Path": "a.txt"}},
                },
            }
        )
        + "\n"
    )
    stream_b.push(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-conc-B",
                    "step_index": 1,
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "write_file",
                    "tool_info": {"name": "write_file", "parameters": {"Path": "b.txt"}},
                },
            }
        )
        + "\n"
    )
    stream_a.push(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-conc-A",
                    "status": "SUCCESS",
                    "response": "DONE_A",
                    "usage": {"input_tokens": 100, "output_tokens": 11, "total_tokens": 111},
                },
            }
        )
        + "\n"
    )
    stream_b.push(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-conc-B",
                    "status": "SUCCESS",
                    "response": "DONE_B",
                    "usage": {"input_tokens": 200, "output_tokens": 22, "total_tokens": 222},
                },
            }
        )
        + "\n"
    )
    stream_a.close_stream()
    stream_b.close_stream()

    cb_a: list[AntigravitySessionEvent] = []
    cb_b: list[AntigravitySessionEvent] = []

    def _run_a() -> Any:
        barrier.wait()
        return manager.run_turn("session_A", "Run A", on_event=cb_a.append)

    def _run_b() -> Any:
        barrier.wait()
        return manager.run_turn("session_B", "Run B", on_event=cb_b.append)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(_run_a)
        fb = pool.submit(_run_b)
        res_a, res_b = fa.result(), fb.result()

    assert res_a.session_id == "session_A"
    assert res_b.session_id == "session_B"
    assert res_a.provider_conversation_id == "conv-conc-A"
    assert res_b.provider_conversation_id == "conv-conc-B"
    assert res_a.stream_result.observed_tool_names == ["read_file"]
    assert res_b.stream_result.observed_tool_names == ["write_file"]
    assert res_a.stream_result.parse_error_count == 1
    assert res_b.stream_result.parse_error_count == 0
    assert res_a.stream_result.telemetry.total_tokens == 111
    assert res_b.stream_result.telemetry.total_tokens == 222

    # Separate sequence numbers starting at 1 in both streams, never leaked across callbacks.
    assert [e.event.sequence_number for e in cb_a] == list(range(1, len(cb_a) + 1))
    assert [e.event.sequence_number for e in cb_b] == list(range(1, len(cb_b) + 1))
    assert all(e.session_id == "session_A" and e.session_id_source == "adapter_session_context" for e in cb_a)
    assert all(e.session_id == "session_B" and e.session_id_source == "adapter_session_context" for e in cb_b)
    assert not any("conv-conc-B" in json.dumps(e.model_dump(mode="json")) for e in cb_a)
    assert not any("conv-conc-A" in json.dumps(e.model_dump(mode="json")) for e in cb_b)


def test_cross_session_artifact_redaction_cleanup_failures_and_compat(
    fake_agy_binary: Path,
    session_dirs: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    """Verify artifact mismatch, --settings redaction, cleanup, failures, and compat."""
    root, ws_a, ws_b = session_dirs

    # --settings payload path details must be redacted from command_summary.
    settings_json = json.dumps(build_ephemeral_deny_settings([ws_a], deny_shell_commands=True))
    cmd_summary = format_command_summary(
        [str(fake_agy_binary), "--settings", settings_json, "--print=secret prompt"]
    )
    assert "--settings <redacted_rules>" in cmd_summary
    assert str(ws_a.name) not in cmd_summary
    assert "secret prompt" not in cmd_summary

    # Backward compatibility: P02.03 stream_task without session flags still works on old --help.
    compat_ndjson = "\n".join(
        [
            json.dumps({"event": "init", "conversation_id": "compat-1", "init": {}}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "OK"}}),
        ]
    )
    compat_adapter = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_session_probe_runner(
            help_text="Usage: --print --output-format stream-json --sandbox --mode"
        ),
        popen_factory=lambda _c, **_k: _FakeStreamProcess(
            stdout_stream=io.StringIO(compat_ndjson),
            returncode=0,
        ),
    )
    compat_res = compat_adapter.stream_task(
        AntigravityStreamRequest(prompt="Legacy P02.03 call", workspace_path=str(ws_a))
    )
    assert compat_res.status == "SUCCEEDED"

    # Session turn blocked if --settings unsupported.
    manager_old = AntigravitySessionManager(adapter=compat_adapter)
    manager_old.create_session(
        AntigravitySessionConfig(
            session_id="session_old",
            workspace_path=str(ws_a),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )
    blocked_turn = manager_old.run_turn("session_old", "Requires settings")
    assert blocked_turn.stream_result.status == "BLOCKED"
    assert blocked_turn.stream_result.error is not None
    assert blocked_turn.stream_result.error.error_code == "UNSUPPORTED_MODE"

    # Failure propagation: missing CLI -> BLOCKED; nonzero / timeout -> FAILED / TIMED_OUT.
    missing_mgr = AntigravitySessionManager(
        adapter=AntigravityAdapter(cli_path=str(tmp_path / "missing.exe"))
    )
    missing_mgr.create_session(
        AntigravitySessionConfig(
            session_id="session_missing",
            workspace_path=str(ws_a),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )
    missing_turn = missing_mgr.run_turn("session_missing", "Missing exe")
    assert missing_turn.stream_result.status == "BLOCKED"

    # Cross-session artifact check + timeout + cleanup_workspace=True leaves sibling intact.
    timeout_adapter = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_session_probe_runner(),
        popen_factory=lambda _c, **_k: _FakeStreamProcess(
            stdout_stream=io.StringIO(""),
            returncode=1,
            timeout_on_wait=True,
        ),
    )
    mgr = AntigravitySessionManager(adapter=timeout_adapter)
    mgr.create_session(
        AntigravitySessionConfig(
            session_id="session_A",
            workspace_path=str(ws_a),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )
    mgr.create_session(
        AntigravitySessionConfig(
            session_id="session_B",
            workspace_path=str(ws_b),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )

    timeout_turn = mgr.run_turn("session_A", "Hang")
    assert timeout_turn.stream_result.status == "TIMED_OUT"

    # Write artifact in B, verify from A -> must fail with artifact_exists=False.
    (ws_b / "proof_B.txt").write_text("name=B\n", encoding="utf-8")
    (ws_a / "proof_A.txt").write_text("wrong_content\n", encoding="utf-8")

    ok_stream = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_session_probe_runner(),
        popen_factory=lambda _c, **_k: _FakeStreamProcess(
            stdout_stream=io.StringIO(compat_ndjson),
            returncode=0,
        ),
    )
    ok_mgr = AntigravitySessionManager(adapter=ok_stream)
    ok_mgr.create_session(
        AntigravitySessionConfig(
            session_id="session_A_ok",
            workspace_path=str(ws_a),
            allowed_root_path=str(root),
            allow_temp_workspace=True,
        )
    )
    ok_turn = ok_mgr.run_turn("session_A_ok", "Check artifact")
    cross_ver = ok_mgr.verify_session_artifact(
        "session_A_ok",
        artifact_relpath="proof_B.txt",
        turn_result=ok_turn,
    )
    assert cross_ver.artifact_exists is False
    assert cross_ver.business_verified is False

    mismatch_ver = ok_mgr.verify_session_artifact(
        "session_A_ok",
        artifact_relpath="proof_A.txt",
        turn_result=ok_turn,
        expected_content="expected_content\n",
    )
    assert mismatch_ver.hash_verified is False
    assert mismatch_ver.business_verified is False

    # Closing session_A with cleanup removes ONLY ws_a and leaves ws_b untouched.
    mgr.close_session("session_A", cleanup_workspace=True)
    assert not ws_a.exists()
    assert ws_b.exists() and (ws_b / "proof_B.txt").is_file()
    assert mgr.get_session("session_A").closed is True
    with pytest.raises(SessionIsolationError) as exc_closed:
        mgr.run_turn("session_A", "After close")
    assert exc_closed.value.error_code == "INVALID_SESSION_ID"
