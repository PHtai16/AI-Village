"""Unit tests for P02.03 Antigravity streaming parser, reader, and adapter."""

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
    AntigravityStreamEvent,
    AntigravityStreamParser,
    AntigravityStreamRequest,
)


def _completed(
    args: list[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Build a synthetic CompletedProcess for mocked ``--version`` and ``--help`` probes."""
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _make_probe_runner(
    *,
    help_text: str = (
        "Usage:\n"
        "  --print\n"
        "  --print-timeout\n"
        "  --output-format (text, json, stream-json)\n"
        "  --sandbox\n"
        "  --mode (accept-edits, auto, plan)\n"
    ),
    version: str = "1.5.15",
) -> Any:
    """Create a subprocess.run replacement returning deterministic probe metadata."""

    def _runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--version" in cmd:
            return _completed(cmd, returncode=0, stdout=f"{version}\n")
        if "--help" in cmd:
            return _completed(cmd, returncode=0, stderr=help_text)
        raise AssertionError(f"Unexpected runner invocation: {cmd}")

    return _runner


class _FakeStreamProcess:
    """In-memory subprocess.Popen stand-in supporting line streams, exit codes, and timeouts."""

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
        self._final_returncode = returncode
        self._timeout_on_wait = timeout_on_wait
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self._timeout_on_wait and not self.terminated and not self.killed:
            raise subprocess.TimeoutExpired(cmd=["agy"], timeout=timeout or 1)
        self.returncode = self._final_returncode
        return self._final_returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _IncrementalLineStream:
    """Thread-safe queue-backed stream simulating real-time NDJSON emission before EOF."""

    def __init__(self) -> None:
        self._chunks: queue.Queue[str | None] = queue.Queue()
        self.closed = False

    def push_line(self, line: str) -> None:
        """Enqueue one line to be yielded by ``readline()``."""
        self._chunks.put(line)

    def finish(self) -> None:
        """Signal EOF to the reader thread."""
        self._chunks.put(None)

    def readline(self) -> str:
        item = self._chunks.get()
        if item is None:
            return ""
        return item

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_agy_binary(tmp_path: Path) -> Path:
    """Create a synthetic Antigravity binary path."""
    exe = tmp_path / "agy.exe"
    exe.write_text("dummy", encoding="utf-8")
    return exe


@pytest.fixture
def fake_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace directory for unit tests."""
    ws = tmp_path / "workspace_unit"
    ws.mkdir()
    return ws


def test_stream_task_incremental_delivery_ordering_and_tool_events(
    fake_agy_binary: Path,
    fake_workspace: Path,
) -> None:
    """Verify live callback delivery before EOF, sequence order, source tags, and tools."""
    stream = _IncrementalLineStream()
    init_received = threading.Event()
    result_allowed = threading.Event()

    lines = [
        json.dumps(
            {
                "event": "init",
                "conversation_id": "conv-123",
                "init": {
                    "cwd": str(fake_workspace),
                    "permission_mode": "request-review",
                    "tools": ["read_file", "write_file"],
                },
            }
        )
        + "\n",
        "\n",
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-123",
                    "step_index": 0,
                    "step_type": "user_input",
                    "state": "DONE",
                },
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-123",
                    "step_index": 1,
                    "step_type": "tool",
                    "state": "ACTIVE",
                    "tool_name": "read_file",
                    "tool_info": {
                        "name": "read_file",
                        "parameters": {"Path": "package.json"},
                    },
                },
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-123",
                    "step_index": 1,
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "read_file",
                    "duration_seconds": 0.02,
                    "tool_info": {
                        "name": "read_file",
                        "parameters": {"Path": "package.json"},
                        "output": '{"verification_marker": "SECRET_MARKER"}',
                    },
                },
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-123",
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "ACTIVE",
                    "tool_name": "write_file",
                    "tool_info": {
                        "name": "write_file",
                        "parameters": {"Path": "proof.txt", "Content": "secret"},
                    },
                },
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-123",
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "write_file",
                    "duration_seconds": 0.05,
                    "tool_info": {
                        "name": "write_file",
                        "parameters": {"Path": "proof.txt", "Content": "secret"},
                    },
                },
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-123",
                    "step_index": 3,
                    "step_type": "agent_response",
                    "state": "ACTIVE",
                    "text_delta": "Created proof.txt.",
                },
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-123",
                    "duration_seconds": 1.25,
                    "num_turns": 1,
                    "response": "Created proof.txt.",
                    "status": "SUCCESS",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "total_tokens": 125,
                    },
                },
            }
        )
        + "\n",
    ]

    def _producer() -> None:
        stream.push_line(lines[0])
        assert init_received.wait(timeout=3.0)
        for item in lines[1:]:
            stream.push_line(item)
        result_allowed.set()
        stream.finish()

    def _popen_factory(cmd: list[str], **_kwargs: Any) -> _FakeStreamProcess:
        assert "--sandbox" in cmd
        assert "--mode" in cmd and "accept-edits" in cmd
        assert "--output-format" in cmd and "stream-json" in cmd
        threading.Thread(target=_producer, daemon=True).start()
        return _FakeStreamProcess(stdout_stream=stream, returncode=0)

    seen_before_finish: list[str] = []
    streamed_events: list[AntigravityStreamEvent] = []

    def _on_event(evt: AntigravityStreamEvent) -> None:
        streamed_events.append(evt)
        if evt.event_type == "execution_started":
            assert not result_allowed.is_set()
            seen_before_finish.append(evt.event_type)
            init_received.set()

    adapter = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_probe_runner(),
        popen_factory=_popen_factory,
    )
    result = adapter.stream_task(
        AntigravityStreamRequest(
            prompt="Read package.json and write proof.txt.",
            workspace_path=str(fake_workspace),
        ),
        on_event=_on_event,
    )

    assert seen_before_finish == ["execution_started"]
    assert result.status == "SUCCEEDED"
    assert result.exit_code == 0
    assert result.completion_event_received is True
    assert result.conversation_id == "conv-123"
    assert result.observed_tool_names == ["read_file", "write_file"]
    assert result.first_event_at is not None
    assert result.telemetry.telemetry_status == "VERIFIED_MEASURED"
    assert result.telemetry.total_tokens == 125
    assert result.events == streamed_events

    sequences = [evt.sequence_number for evt in result.events]
    assert sequences == list(range(1, len(result.events) + 1))

    assert result.events[0].event_type == "process_started"
    assert result.events[0].source == "adapter"
    assert result.events[-1].event_type == "process_exited"
    assert result.events[-1].source == "adapter"

    provider_events = [evt for evt in result.events if evt.source == "provider_cli"]
    assert [evt.event_type for evt in provider_events] == [
        "execution_started",
        "step_update",
        "tool_call_started",
        "tool_call_completed",
        "tool_call_started",
        "tool_call_completed",
        "text_delta",
        "execution_completed",
    ]
    tool_events = [evt for evt in provider_events if evt.event_type.startswith("tool_call_")]
    assert all(evt.tool_call_id is None for evt in tool_events)


def test_stream_parser_unknown_events_missing_fields_and_no_fake_tool_used() -> None:
    """Verify unknown events remain UNKNOWN, optional fields omitted, no fake tools."""
    parser = AntigravityStreamParser()

    ev_unknown_top = parser.parse_line(
        json.dumps({"event": "heartbeat_v2", "ts": 123}),
        line_number=1,
    )
    assert ev_unknown_top is not None
    assert ev_unknown_top.event_type == "unknown_provider_event"
    assert ev_unknown_top.source == "provider_cli"
    assert ev_unknown_top.status == "UNKNOWN"
    assert ev_unknown_top.redacted_payload["top_level_keys"] == ["event", "ts"]

    ev_unknown_step = parser.parse_line(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_index": 0, "step_type": "thinking", "state": "DONE"},
            }
        ),
        line_number=2,
    )
    assert ev_unknown_step is not None
    assert ev_unknown_step.event_type == "unknown_provider_event"
    assert ev_unknown_step.status == "UNKNOWN"

    # Model prose claiming tool execution must NEVER synthesize tool_call_* events.
    ev_text_claim = parser.parse_line(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "step_type": "agent_response",
                    "state": "DONE",
                    "text_delta": "I executed read_file and write_file successfully.",
                },
            }
        ),
        line_number=3,
    )
    assert ev_text_claim is not None
    assert ev_text_claim.event_type == "text_delta"
    assert parser.observed_tool_names == []

    # Unknown tool state must not be counted as verified tool execution either.
    ev_tool_paused = parser.parse_line(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "PAUSED",
                    "tool_name": "read_file",
                },
            }
        ),
        line_number=4,
    )
    assert ev_tool_paused is not None
    assert ev_tool_paused.event_type == "unknown_provider_event"
    assert parser.observed_tool_names == []

    # Missing optional telemetry/conversation_id on result remains UNAVAILABLE_FROM_PROVIDER.
    ev_result = parser.parse_line(
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "Done"}}),
        line_number=5,
    )
    assert ev_result is not None
    assert ev_result.event_type == "execution_completed"
    assert ev_result.conversation_id is None
    assert ev_result.telemetry is not None
    assert ev_result.telemetry.telemetry_status == "UNAVAILABLE_FROM_PROVIDER"
    assert ev_result.telemetry.input_tokens is None


def test_stream_parser_malformed_truncated_oversized_unterminated_and_unicode(
    fake_agy_binary: Path,
    fake_workspace: Path,
) -> None:
    """Verify invalid JSON, truncated lines, line size limit, EOF flush, and Unicode."""
    parser = AntigravityStreamParser(max_line_bytes=200)
    assert parser.parse_line("   \n", line_number=1) is None

    ev_bad = parser.parse_line('{"event": "step_update"', line_number=2)
    assert ev_bad is not None
    assert ev_bad.event_type == "stream_parse_failed"
    assert ev_bad.source == "adapter"
    assert ev_bad.redacted_payload["reason"] == "invalid_json"

    ev_non_obj = parser.parse_line("[1, 2, 3]", line_number=3)
    assert ev_non_obj is not None
    assert ev_non_obj.event_type == "stream_parse_failed"
    assert ev_non_obj.redacted_payload["reason"] == "non_object_json"

    oversized = json.dumps({"event": "init", "pad": "x" * 300})
    ev_big = parser.parse_line(oversized, line_number=4)
    assert ev_big is not None
    assert ev_big.event_type == "stream_parse_failed"
    assert ev_big.redacted_payload["reason"] == "line_exceeds_max_bytes"
    assert parser.parse_error_count == 3

    # End-to-end test: truncated line in the middle plus unterminated final Unicode result.
    ndjson_without_trailing_newline = (
        json.dumps({"event": "init", "conversation_id": "u1", "init": {}})
        + "\n"
        + "{corrupt_json_line\n"
        + json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "step_type": "agent_response",
                    "state": "ACTIVE",
                    "text_delta": "Xin chào tiếng Việt 🚀",
                },
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {"event": "result", "result": {"status": "SUCCESS", "response": "Hoàn tất ✅"}},
            ensure_ascii=False,
        )
    )
    assert not ndjson_without_trailing_newline.endswith("\n")

    def _popen_factory(_cmd: list[str], **_kwargs: Any) -> _FakeStreamProcess:
        return _FakeStreamProcess(
            stdout_stream=io.StringIO(ndjson_without_trailing_newline),
            returncode=0,
        )

    adapter = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_probe_runner(),
        popen_factory=_popen_factory,
    )
    res = adapter.stream_task(
        AntigravityStreamRequest(prompt="Unicode test", workspace_path=str(fake_workspace))
    )
    assert res.status == "SUCCEEDED"
    assert res.parse_error_count == 1
    assert res.completion_event_received is True
    assert res.response_text == "Hoàn tất ✅"
    assert any(evt.event_type == "stream_parse_failed" for evt in res.events)
    deltas = [evt for evt in res.events if evt.event_type == "text_delta"]
    assert len(deltas) == 1
    assert deltas[0].redacted_payload["text_delta"] == "Xin chào tiếng Việt 🚀"


def test_stream_security_allowlist_redaction_bounds_and_validation(
    fake_agy_binary: Path,
    fake_workspace: Path,
) -> None:
    """Verify cwd, tool parameters/outputs, and secret patterns never leak into events."""
    with pytest.raises(ValidationError):
        AntigravityStreamRequest(prompt="   ", workspace_path=str(fake_workspace))

    secret_api_key = "AIza" + "SecretKey1234567890123456"
    secret_openai = "sk-" + "LiveSecretToken123456789012"
    secret_cwd = r"C:\Users\private_operator\secret_workspace"
    raw_file_content = f"TOKEN={secret_openai}\nSECRET_MARKER_XYZ\n"

    stream_text = "\n".join(
        [
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "sec-1",
                    "init": {
                        "cwd": secret_cwd,
                        "permission_mode": "request-review",
                        "tools": ["read_file", "write_file"],
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "step_type": "tool",
                        "state": "DONE",
                        "tool_name": "read_file",
                        "tool_info": {
                            "name": "read_file",
                            "parameters": {"AbsolutePath": r"C:\secret\env.txt"},
                            "output": raw_file_content,
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 2,
                        "step_type": "agent_response",
                        "state": "ACTIVE",
                        "text_delta": f"Authorization: Bearer abcdef1234567890 key={secret_api_key} " + ("A" * 200),
                    },
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": f"Done api_key={secret_api_key} " + ("B" * 200),
                    },
                }
            ),
        ]
    )

    def _popen_factory(_cmd: list[str], **_kwargs: Any) -> _FakeStreamProcess:
        return _FakeStreamProcess(
            stdout_stream=io.StringIO(stream_text),
            stderr_text=f"diagnostic token={secret_openai}",
            returncode=0,
        )

    adapter = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_probe_runner(),
        popen_factory=_popen_factory,
    )
    res = adapter.stream_task(
        AntigravityStreamRequest(
            prompt="Security check",
            workspace_path=str(fake_workspace),
            max_payload_chars=80,
        )
    )

    assert res.status == "SUCCEEDED"
    serialized = res.model_dump_json()
    assert secret_api_key not in serialized
    assert secret_openai not in serialized
    assert secret_cwd.replace("\\", "\\\\") not in serialized
    assert "SECRET_MARKER_XYZ" not in serialized
    assert r"env.txt" not in serialized

    init_event = next(evt for evt in res.events if evt.event_type == "execution_started")
    assert "cwd" not in init_event.redacted_payload
    assert init_event.redacted_payload == {
        "permission_mode": "request-review",
        "tools_count": 2,
    }

    tool_event = next(evt for evt in res.events if evt.event_type == "tool_call_completed")
    assert tool_event.redacted_payload["parameter_keys"] == ["AbsolutePath"]
    assert tool_event.redacted_payload["has_output"] is True
    assert tool_event.redacted_payload["output_length"] == len(raw_file_content)
    assert "output" not in tool_event.redacted_payload
    assert "parameters" not in tool_event.redacted_payload

    delta_event = next(evt for evt in res.events if evt.event_type == "text_delta")
    assert len(delta_event.redacted_payload["text_delta"]) <= 80
    assert delta_event.redacted_payload["text_delta"].endswith("...[truncated]")


def test_stream_failures_exit_mismatch_timeout_and_preflight_blocked(
    fake_agy_binary: Path,
    fake_workspace: Path,
    tmp_path: Path,
) -> None:
    """Verify completion mismatch, nonzero exit, timeout, error events, and preflight BLOCKED."""
    # 1. Clean exit_code == 0 WITHOUT completion event must FAIL with MISSING_COMPLETION_EVENT.
    incomplete_stream = json.dumps({"event": "init", "conversation_id": "inc-1", "init": {}}) + "\n"
    adapter_incomplete = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_probe_runner(),
        popen_factory=lambda _c, **_k: _FakeStreamProcess(
            stdout_stream=io.StringIO(incomplete_stream),
            returncode=0,
        ),
    )
    res_incomplete = adapter_incomplete.stream_task(
        AntigravityStreamRequest(prompt="Incomplete", workspace_path=str(fake_workspace))
    )
    assert res_incomplete.status == "FAILED"
    assert res_incomplete.completion_event_received is False
    assert res_incomplete.error is not None
    assert res_incomplete.error.error_code == "MISSING_COMPLETION_EVENT"
    assert any(
        evt.event_type == "stream_interrupted" and evt.source == "adapter"
        for evt in res_incomplete.events
    )

    # 2. Provider terminal failure status + top-level error event -> FAILED + provider_error.
    provider_fail_stream = "\n".join(
        [
            json.dumps({"event": "error", "error": {"message": "token=secret_123456"}}),
            json.dumps(
                {
                    "event": "result",
                    "result": {"status": "ERROR", "response": "Tool execution failed"},
                }
            ),
        ]
    )
    adapter_provider_fail = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_probe_runner(),
        popen_factory=lambda _c, **_k: _FakeStreamProcess(
            stdout_stream=io.StringIO(provider_fail_stream),
            returncode=0,
        ),
    )
    res_provider_fail = adapter_provider_fail.stream_task(
        AntigravityStreamRequest(prompt="Fail cleanly", workspace_path=str(fake_workspace))
    )
    assert res_provider_fail.status == "FAILED"
    assert res_provider_fail.completion_event_received is False
    assert res_provider_fail.error is not None
    assert res_provider_fail.error.error_code == "PROVIDER_ERROR"
    assert [evt.event_type for evt in res_provider_fail.events if evt.source == "provider_cli"] == [
        "provider_error",
        "provider_error",
    ]
    assert "secret_123456" not in res_provider_fail.model_dump_json()

    # 3. Nonzero exit code with stderr classification and redaction.
    adapter_nonzero = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_probe_runner(),
        popen_factory=lambda _c, **_k: _FakeStreamProcess(
            stdout_stream=io.StringIO(""),
            stderr_text="Error: unknown flag: --bogus api_key=leak999999",
            returncode=2,
        ),
    )
    res_nonzero = adapter_nonzero.stream_task(
        AntigravityStreamRequest(prompt="Flag error", workspace_path=str(fake_workspace))
    )
    assert res_nonzero.status == "FAILED"
    assert res_nonzero.exit_code == 2
    assert res_nonzero.error is not None
    assert res_nonzero.error.error_code == "UNSUPPORTED_ARGUMENT"
    assert "leak999999" not in res_nonzero.redacted_stderr

    # 4. Timeout terminates subprocess and emits adapter process_timed_out event.
    proc_timeout = _FakeStreamProcess(
        stdout_stream=io.StringIO(""),
        returncode=1,
        timeout_on_wait=True,
    )
    adapter_timeout = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_probe_runner(),
        popen_factory=lambda _c, **_k: proc_timeout,
    )
    res_timeout = adapter_timeout.stream_task(
        AntigravityStreamRequest(prompt="Wait forever", workspace_path=str(fake_workspace))
    )
    assert res_timeout.status == "TIMED_OUT"
    assert res_timeout.error is not None
    assert res_timeout.error.error_code == "TIMED_OUT"
    assert any(
        evt.event_type == "process_timed_out" and evt.source == "adapter"
        for evt in res_timeout.events
    )

    # 5. Preflight BLOCKED cases: missing CLI and missing stream-json flag.
    missing_res = AntigravityAdapter(cli_path=str(tmp_path / "missing.exe")).stream_task(
        AntigravityStreamRequest(prompt="Missing CLI", workspace_path=str(fake_workspace))
    )
    assert missing_res.status == "BLOCKED"
    assert missing_res.error is not None
    assert missing_res.error.error_code == "EXECUTABLE_NOT_FOUND"

    no_stream_adapter = AntigravityAdapter(
        cli_path=str(fake_agy_binary),
        runner=_make_probe_runner(help_text="Usage: --print --sandbox --mode --output-format json"),
    )
    no_stream_res = no_stream_adapter.stream_task(
        AntigravityStreamRequest(prompt="No stream-json", workspace_path=str(fake_workspace))
    )
    assert no_stream_res.status == "BLOCKED"
    assert no_stream_res.error is not None
    assert no_stream_res.error.error_code == "UNSUPPORTED_MODE"
