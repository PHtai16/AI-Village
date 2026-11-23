"""Real-time NDJSON streaming parser and pipe reader for Antigravity CLI output."""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Callable
import uuid

from pydantic import BaseModel, Field

from engines.antigravity.diagnostics import format_command_summary, redact_secrets
from engines.antigravity.models import AntigravityStreamEvent, AntigravityTelemetry

logger = logging.getLogger(__name__)

_STDERR_CAPTURE_LIMIT = 65536
_TRUNCATED_SUFFIX = "...[truncated]"
PopenFactory = Callable[..., Any]


def sanitize_stream_text(text: str | None, max_chars: int = 512) -> str:
    """Redact known secret patterns and cap string length for stream event payloads.

    Args:
        text: Unsanitized text extracted from CLI output or exception messages.
        max_chars: Maximum allowed character count after sanitization.

    Returns:
        Secret-redacted string bounded to ``max_chars`` characters.
    """
    sanitized = redact_secrets(text)
    if max_chars <= 0:
        return ""
    if len(sanitized) <= max_chars:
        return sanitized
    if max_chars <= len(_TRUNCATED_SUFFIX):
        return sanitized[:max_chars]
    head_len = max_chars - len(_TRUNCATED_SUFFIX)
    return f"{sanitized[:head_len]}{_TRUNCATED_SUFFIX}"


def _is_real_int(value: Any) -> bool:
    """Return True when ``value`` is an integer excluding booleans."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_real_number(value: Any) -> bool:
    """Return True when ``value`` is an int or float excluding booleans."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_telemetry_from_payload(
    duration_ms: int,
    payload: dict[str, Any] | None,
) -> AntigravityTelemetry:
    """Construct verified telemetry from a CLI JSON dictionary without fabricating zeros.

    Args:
        duration_ms: Measured execution time in milliseconds.
        payload: Parsed dictionary potentially containing ``duration_seconds`` and ``usage``.

    Returns:
        ``AntigravityTelemetry`` marked ``VERIFIED_MEASURED`` when token counts exist,
        otherwise ``UNAVAILABLE_FROM_PROVIDER``.
    """
    safe_duration = max(0, duration_ms)
    if not payload:
        return AntigravityTelemetry(
            duration_ms=safe_duration,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
        )

    raw_provider_dur = payload.get("duration_seconds")
    provider_duration = float(raw_provider_dur) if _is_real_number(raw_provider_dur) else None
    usage = payload.get("usage")
    if isinstance(usage, dict) and any(
        _is_real_int(usage.get(field))
        for field in ("input_tokens", "output_tokens", "total_tokens")
    ):
        return AntigravityTelemetry(
            duration_ms=safe_duration,
            provider_duration_seconds=provider_duration,
            input_tokens=usage.get("input_tokens") if _is_real_int(usage.get("input_tokens")) else None,
            output_tokens=usage.get("output_tokens") if _is_real_int(usage.get("output_tokens")) else None,
            thinking_tokens=usage.get("thinking_tokens") if _is_real_int(usage.get("thinking_tokens")) else None,
            cache_read_tokens=usage.get("cache_read_tokens") if _is_real_int(usage.get("cache_read_tokens")) else None,
            total_tokens=usage.get("total_tokens") if _is_real_int(usage.get("total_tokens")) else None,
            telemetry_status="VERIFIED_MEASURED",
        )

    return AntigravityTelemetry(
        duration_ms=safe_duration,
        provider_duration_seconds=provider_duration,
        telemetry_status="UNAVAILABLE_FROM_PROVIDER",
    )


def _sanitize_denied_actions(raw_denied: Any, max_chars: int) -> list[dict[str, Any]]:
    """Return a bounded, secret-redacted list of denied action records."""
    if not isinstance(raw_denied, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in raw_denied:
        if not isinstance(item, dict):
            continue
        safe_item: dict[str, Any] = {}
        for key in ("action", "display_name", "reason"):
            val = item.get(key)
            if isinstance(val, str):
                safe_item[key] = sanitize_stream_text(val, min(max_chars, 256))
        if safe_item:
            sanitized.append(safe_item)
    return sanitized


class StreamingProcessOutcome(BaseModel):
    """Low-level process execution and collected event outcome from stream reading."""

    started_at: datetime
    first_event_at: datetime | None = None
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    exit_code: int | None = None
    timed_out: bool = False
    launch_error: str | None = None
    events: list[AntigravityStreamEvent] = Field(default_factory=list)
    redacted_stderr: str = ""
    redacted_stdout_diagnostics: str = ""


class AntigravityStreamParser:
    """Stateful allowlist parser converting CLI NDJSON lines into normalized events."""

    def __init__(
        self,
        *,
        max_payload_chars: int = 512,
        max_line_bytes: int = 262144,
    ) -> None:
        """Initialize parser state for one CLI streaming invocation.

        Args:
            max_payload_chars: Maximum string length kept inside ``redacted_payload``.
            max_line_bytes: Maximum allowed UTF-8 byte length per NDJSON line.
        """
        self.max_payload_chars = max_payload_chars
        self.max_line_bytes = max_line_bytes
        self._sequence = 0
        self.conversation_id: str | None = None
        self.observed_tool_names: list[str] = []
        self.completion_event_received = False
        self.parse_error_count = 0
        self.final_provider_status: str | None = None
        self.final_response_text: str | None = None
        self.final_num_turns: int | None = None
        self.final_denied_actions: list[dict[str, Any]] = []
        self.final_telemetry: AntigravityTelemetry | None = None

    def _allocate_ids(self) -> tuple[int, str]:
        """Advance and return the monotonic sequence number and unique event ID."""
        self._sequence += 1
        return self._sequence, f"evt-{self._sequence:05d}-{uuid.uuid4().hex[:8]}"

    def make_adapter_event(
        self,
        *,
        event_type: str,
        status: str,
        redacted_payload: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
        raw_event_reference: str | None = None,
    ) -> AntigravityStreamEvent:
        """Construct an event explicitly attributed to ``source='adapter'``."""
        seq, event_id = self._allocate_ids()
        return AntigravityStreamEvent(
            event_id=event_id,
            sequence_number=seq,
            timestamp=timestamp or datetime.now(timezone.utc),
            event_type=event_type,  # type: ignore[arg-type]
            source="adapter",
            provider_event_type=None,
            conversation_id=self.conversation_id,
            tool_name=None,
            tool_call_id=None,
            status=status,  # type: ignore[arg-type]
            redacted_payload=redacted_payload or {},
            telemetry=None,
            raw_event_reference=raw_event_reference,
        )

    def _record_conversation_id(self, candidate: Any) -> None:
        """Store a verified non-empty conversation ID string if supplied."""
        if isinstance(candidate, str) and candidate.strip():
            self.conversation_id = sanitize_stream_text(candidate.strip(), 128)

    def parse_line(
        self,
        line: str,
        *,
        line_number: int,
        timestamp: datetime | None = None,
    ) -> AntigravityStreamEvent | None:
        """Parse one stdout line into a normalized event without inventing data.

        Args:
            line: Raw line read from CLI ``stdout``.
            line_number: 1-based line number from the stdout stream.
            timestamp: Optional UTC observation timestamp for the line.

        Returns:
            Normalized ``AntigravityStreamEvent`` or ``None`` for blank lines.
        """
        raw_trimmed = line.rstrip("\r\n")
        if not raw_trimmed.strip():
            return None

        event_ts = timestamp or datetime.now(timezone.utc)
        line_bytes = len(raw_trimmed.encode("utf-8", errors="replace"))
        ref = f"stdout:line={line_number}:bytes={line_bytes}"

        if line_bytes > self.max_line_bytes:
            self.parse_error_count += 1
            return self.make_adapter_event(
                event_type="stream_parse_failed",
                status="FAILED",
                timestamp=event_ts,
                redacted_payload={
                    "reason": "line_exceeds_max_bytes",
                    "line_bytes": line_bytes,
                    "max_line_bytes": self.max_line_bytes,
                },
                raw_event_reference=ref,
            )

        try:
            obj = json.loads(raw_trimmed)
        except json.JSONDecodeError as exc:
            self.parse_error_count += 1
            return self.make_adapter_event(
                event_type="stream_parse_failed",
                status="FAILED",
                timestamp=event_ts,
                redacted_payload={
                    "reason": "invalid_json",
                    "error": sanitize_stream_text(exc.msg, self.max_payload_chars),
                },
                raw_event_reference=ref,
            )

        if not isinstance(obj, dict):
            self.parse_error_count += 1
            return self.make_adapter_event(
                event_type="stream_parse_failed",
                status="FAILED",
                timestamp=event_ts,
                redacted_payload={
                    "reason": "non_object_json",
                    "json_type": type(obj).__name__,
                },
                raw_event_reference=ref,
            )

        self._record_conversation_id(obj.get("conversation_id"))
        top_event = obj.get("event")

        match top_event:
            case "init":
                return self._parse_init_event(obj, ref, event_ts)
            case "step_update":
                return self._parse_step_update_event(obj, ref, event_ts)
            case "result":
                return self._parse_result_event(obj, ref, event_ts)
            case "error":
                return self._parse_error_event(obj, ref, event_ts)
            case _:
                seq, event_id = self._allocate_ids()
                provider_label = str(top_event) if top_event is not None else "missing_event_field"
                return AntigravityStreamEvent(
                    event_id=event_id,
                    sequence_number=seq,
                    timestamp=event_ts,
                    event_type="unknown_provider_event",
                    source="provider_cli",
                    provider_event_type=sanitize_stream_text(provider_label, 128),
                    conversation_id=self.conversation_id,
                    status="UNKNOWN",
                    redacted_payload={
                        "top_level_keys": sorted(str(key) for key in obj.keys()),
                    },
                    raw_event_reference=ref,
                )

    def _parse_init_event(
        self,
        obj: dict[str, Any],
        ref: str,
        timestamp: datetime,
    ) -> AntigravityStreamEvent:
        """Map CLI ``init`` event using strict field allowlisting (omitting ``cwd``)."""
        init_data = obj.get("init") if isinstance(obj.get("init"), dict) else {}
        payload: dict[str, Any] = {}
        perm_mode = init_data.get("permission_mode")
        if isinstance(perm_mode, str):
            payload["permission_mode"] = sanitize_stream_text(perm_mode, 128)
        tools = init_data.get("tools")
        if isinstance(tools, list):
            payload["tools_count"] = len(tools)

        seq, event_id = self._allocate_ids()
        return AntigravityStreamEvent(
            event_id=event_id,
            sequence_number=seq,
            timestamp=timestamp,
            event_type="execution_started",
            source="provider_cli",
            provider_event_type="init",
            conversation_id=self.conversation_id,
            status="STARTED",
            redacted_payload=payload,
            raw_event_reference=ref,
        )

    def _parse_step_update_event(
        self,
        obj: dict[str, Any],
        ref: str,
        timestamp: datetime,
    ) -> AntigravityStreamEvent:
        """Normalize CLI ``step_update`` events across response, tool, and unknown steps."""
        su = obj.get("step_update")
        if not isinstance(su, dict):
            self.parse_error_count += 1
            return self.make_adapter_event(
                event_type="stream_parse_failed",
                status="FAILED",
                timestamp=timestamp,
                redacted_payload={"reason": "invalid_step_update_payload"},
                raw_event_reference=ref,
            )

        self._record_conversation_id(su.get("conversation_id"))
        step_type = su.get("step_type")
        state = su.get("state")
        step_index = su.get("step_index") if _is_real_int(su.get("step_index")) else None
        raw_duration = su.get("duration_seconds")
        duration_sec = float(raw_duration) if _is_real_number(raw_duration) else None
        step_dur_ms = max(0, int(duration_sec * 1000)) if duration_sec is not None else 0
        telemetry = (
            build_telemetry_from_payload(step_dur_ms, su)
            if isinstance(su.get("usage"), dict)
            else None
        )

        if step_type == "tool":
            return self._parse_tool_step(
                su=su,
                step_index=step_index,
                state=state,
                duration_sec=duration_sec,
                telemetry=telemetry,
                ref=ref,
                timestamp=timestamp,
            )

        if step_type == "agent_response":
            return self._parse_agent_response_step(
                su=su,
                step_index=step_index,
                state=state,
                duration_sec=duration_sec,
                telemetry=telemetry,
                ref=ref,
                timestamp=timestamp,
            )

        seq, event_id = self._allocate_ids()
        if step_type in ("user_input", "system_message"):
            mapped_status = _map_step_state_to_status(state)
            payload: dict[str, Any] = {"step_type": step_type}
            if step_index is not None:
                payload["step_index"] = step_index
            if isinstance(state, str):
                payload["state"] = sanitize_stream_text(state, 64)
            if duration_sec is not None:
                payload["duration_seconds"] = duration_sec
            provider_ev = (
                f"step_update:{step_type}:{state}"
                if isinstance(state, str)
                else f"step_update:{step_type}"
            )
            return AntigravityStreamEvent(
                event_id=event_id,
                sequence_number=seq,
                timestamp=timestamp,
                event_type="step_update",
                source="provider_cli",
                provider_event_type=sanitize_stream_text(provider_ev, 128),
                conversation_id=self.conversation_id,
                status=mapped_status,  # type: ignore[arg-type]
                redacted_payload=payload,
                telemetry=telemetry,
                raw_event_reference=ref,
            )

        unknown_payload: dict[str, Any] = {
            "payload_keys": sorted(str(key) for key in su.keys()),
        }
        if step_index is not None:
            unknown_payload["step_index"] = step_index
        if step_type is not None:
            unknown_payload["step_type"] = sanitize_stream_text(str(step_type), 128)
        if state is not None:
            unknown_payload["state"] = sanitize_stream_text(str(state), 64)
        provider_ev = f"step_update:{step_type}" if step_type is not None else "step_update:unknown"
        return AntigravityStreamEvent(
            event_id=event_id,
            sequence_number=seq,
            timestamp=timestamp,
            event_type="unknown_provider_event",
            source="provider_cli",
            provider_event_type=sanitize_stream_text(provider_ev, 128),
            conversation_id=self.conversation_id,
            status="UNKNOWN",
            redacted_payload=unknown_payload,
            telemetry=telemetry,
            raw_event_reference=ref,
        )

    def _parse_tool_step(
        self,
        *,
        su: dict[str, Any],
        step_index: int | None,
        state: Any,
        duration_sec: float | None,
        telemetry: AntigravityTelemetry | None,
        ref: str,
        timestamp: datetime,
    ) -> AntigravityStreamEvent:
        """Map CLI ``step_type='tool'`` while keeping argument values and outputs out."""
        tool_info = su.get("tool_info") if isinstance(su.get("tool_info"), dict) else {}
        raw_name = (
            su.get("tool_name")
            if isinstance(su.get("tool_name"), str) and su.get("tool_name", "").strip()
            else tool_info.get("name")
        )
        tool_name = (
            sanitize_stream_text(raw_name.strip(), 128)
            if isinstance(raw_name, str) and raw_name.strip()
            else None
        )
        raw_call_id = su.get("tool_call_id") or tool_info.get("tool_call_id")
        tool_call_id = (
            sanitize_stream_text(raw_call_id.strip(), 128)
            if isinstance(raw_call_id, str) and raw_call_id.strip()
            else None
        )

        normalized_state = state.upper() if isinstance(state, str) else ""
        if normalized_state in {"ACTIVE", "RUNNING", "PENDING", "STARTED"}:
            event_type, status = "tool_call_started", "IN_PROGRESS"
        elif normalized_state in {"DONE", "COMPLETED", "SUCCESS"}:
            event_type, status = "tool_call_completed", "COMPLETED"
        elif normalized_state in {"ERROR", "FAILED", "DENIED"}:
            event_type, status = "tool_call_failed", "FAILED"
        else:
            event_type, status = "unknown_provider_event", "UNKNOWN"

        if tool_name and event_type != "unknown_provider_event":
            if tool_name not in self.observed_tool_names:
                self.observed_tool_names.append(tool_name)

        params = tool_info.get("parameters")
        param_keys = sorted(str(key) for key in params.keys()) if isinstance(params, dict) else []
        raw_output = tool_info.get("output")
        has_output = isinstance(raw_output, str)

        payload: dict[str, Any] = {
            "step_type": "tool",
            "parameter_keys": param_keys,
            "has_output": has_output,
        }
        if has_output:
            payload["output_length"] = len(raw_output)
        if step_index is not None:
            payload["step_index"] = step_index
        if isinstance(state, str):
            payload["state"] = sanitize_stream_text(state, 64)
        if duration_sec is not None:
            payload["duration_seconds"] = duration_sec

        seq, event_id = self._allocate_ids()
        provider_ev = (
            f"step_update:tool:{state}" if isinstance(state, str) else "step_update:tool"
        )
        return AntigravityStreamEvent(
            event_id=event_id,
            sequence_number=seq,
            timestamp=timestamp,
            event_type=event_type,  # type: ignore[arg-type]
            source="provider_cli",
            provider_event_type=sanitize_stream_text(provider_ev, 128),
            conversation_id=self.conversation_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            status=status,  # type: ignore[arg-type]
            redacted_payload=payload,
            telemetry=telemetry,
            raw_event_reference=ref,
        )

    def _parse_agent_response_step(
        self,
        *,
        su: dict[str, Any],
        step_index: int | None,
        state: Any,
        duration_sec: float | None,
        telemetry: AntigravityTelemetry | None,
        ref: str,
        timestamp: datetime,
    ) -> AntigravityStreamEvent:
        """Map CLI ``step_type='agent_response'`` text deltas and completion steps."""
        text_delta = su.get("text_delta")
        has_delta = isinstance(text_delta, str) and len(text_delta) > 0
        payload: dict[str, Any] = {"step_type": "agent_response"}
        if step_index is not None:
            payload["step_index"] = step_index
        if isinstance(state, str):
            payload["state"] = sanitize_stream_text(state, 64)
        if duration_sec is not None:
            payload["duration_seconds"] = duration_sec

        if has_delta:
            event_type = "text_delta"
            payload["text_delta"] = sanitize_stream_text(text_delta, self.max_payload_chars)
            payload["delta_length"] = len(text_delta)
            provider_ev = (
                f"step_update:agent_response:text_delta:{state}"
                if isinstance(state, str)
                else "step_update:agent_response:text_delta"
            )
        else:
            event_type = "step_update"
            provider_ev = (
                f"step_update:agent_response:{state}"
                if isinstance(state, str)
                else "step_update:agent_response"
            )

        seq, event_id = self._allocate_ids()
        return AntigravityStreamEvent(
            event_id=event_id,
            sequence_number=seq,
            timestamp=timestamp,
            event_type=event_type,  # type: ignore[arg-type]
            source="provider_cli",
            provider_event_type=sanitize_stream_text(provider_ev, 128),
            conversation_id=self.conversation_id,
            status=_map_step_state_to_status(state),  # type: ignore[arg-type]
            redacted_payload=payload,
            telemetry=telemetry,
            raw_event_reference=ref,
        )

    def _parse_result_event(
        self,
        obj: dict[str, Any],
        ref: str,
        timestamp: datetime,
    ) -> AntigravityStreamEvent:
        """Map CLI terminal ``result`` event into completion or provider_error."""
        res = obj.get("result")
        if not isinstance(res, dict):
            self.parse_error_count += 1
            return self.make_adapter_event(
                event_type="stream_parse_failed",
                status="FAILED",
                timestamp=timestamp,
                redacted_payload={"reason": "invalid_result_payload"},
                raw_event_reference=ref,
            )

        self._record_conversation_id(res.get("conversation_id"))
        raw_status = res.get("status")
        self.final_provider_status = str(raw_status) if raw_status is not None else None
        raw_response = res.get("response")
        if isinstance(raw_response, str):
            self.final_response_text = redact_secrets(raw_response)
        raw_turns = res.get("num_turns")
        if _is_real_int(raw_turns):
            self.final_num_turns = raw_turns
        self.final_denied_actions = _sanitize_denied_actions(
            res.get("denied_actions"),
            self.max_payload_chars,
        )

        raw_dur = res.get("duration_seconds")
        dur_ms = max(0, int(float(raw_dur) * 1000)) if _is_real_number(raw_dur) else 0
        self.final_telemetry = build_telemetry_from_payload(dur_ms, res)

        payload: dict[str, Any] = {
            "provider_status": self.final_provider_status,
            "denied_actions_count": len(self.final_denied_actions),
        }
        if self.final_num_turns is not None:
            payload["num_turns"] = self.final_num_turns
        if _is_real_number(raw_dur):
            payload["duration_seconds"] = float(raw_dur)
        if isinstance(raw_response, str):
            payload["response_preview"] = sanitize_stream_text(
                raw_response,
                self.max_payload_chars,
            )

        seq, event_id = self._allocate_ids()
        if raw_status == "SUCCESS":
            self.completion_event_received = True
            return AntigravityStreamEvent(
                event_id=event_id,
                sequence_number=seq,
                timestamp=timestamp,
                event_type="execution_completed",
                source="provider_cli",
                provider_event_type="result:SUCCESS",
                conversation_id=self.conversation_id,
                status="COMPLETED",
                redacted_payload=payload,
                telemetry=self.final_telemetry,
                raw_event_reference=ref,
            )

        provider_ev = f"result:{self.final_provider_status or 'UNKNOWN'}"
        return AntigravityStreamEvent(
            event_id=event_id,
            sequence_number=seq,
            timestamp=timestamp,
            event_type="provider_error",
            source="provider_cli",
            provider_event_type=sanitize_stream_text(provider_ev, 128),
            conversation_id=self.conversation_id,
            status="FAILED",
            redacted_payload=payload,
            telemetry=self.final_telemetry,
            raw_event_reference=ref,
        )

    def _parse_error_event(
        self,
        obj: dict[str, Any],
        ref: str,
        timestamp: datetime,
    ) -> AntigravityStreamEvent:
        """Map an explicit top-level CLI error event with redacted bounded text."""
        raw_err = obj.get("error")
        message: str | None = None
        if isinstance(raw_err, dict):
            msg_val = raw_err.get("message") or raw_err.get("error")
            if isinstance(msg_val, str):
                message = msg_val
        elif isinstance(raw_err, str):
            message = raw_err
        elif isinstance(obj.get("message"), str):
            message = obj.get("message")

        payload: dict[str, Any] = {}
        if message:
            payload["message"] = sanitize_stream_text(message, self.max_payload_chars)
        code_val = obj.get("code")
        if isinstance(code_val, (str, int)) and not isinstance(code_val, bool):
            payload["code"] = sanitize_stream_text(str(code_val), 64)

        seq, event_id = self._allocate_ids()
        return AntigravityStreamEvent(
            event_id=event_id,
            sequence_number=seq,
            timestamp=timestamp,
            event_type="provider_error",
            source="provider_cli",
            provider_event_type="error",
            conversation_id=self.conversation_id,
            status="FAILED",
            redacted_payload=payload,
            raw_event_reference=ref,
        )


def _map_step_state_to_status(state: Any) -> str:
    """Map CLI ``step_update.state`` values onto normalized event statuses."""
    if not isinstance(state, str):
        return "UNKNOWN"
    upper = state.upper()
    if upper in {"DONE", "COMPLETED", "SUCCESS"}:
        return "COMPLETED"
    if upper in {"ACTIVE", "RUNNING", "PENDING", "STARTED"}:
        return "IN_PROGRESS"
    if upper in {"ERROR", "FAILED", "DENIED"}:
        return "FAILED"
    return "UNKNOWN"


def _drain_stderr(stream: Any, chunks: list[str], limit: int) -> None:
    """Read ``stderr`` to EOF inside a worker thread to prevent pipe buffer deadlocks."""
    captured = 0
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if captured < limit:
                remaining = limit - captured
                chunks.append(chunk[:remaining])
                captured += min(len(chunk), remaining)
    except OSError as exc:
        logger.debug("Stderr stream closed during read: %s", redact_secrets(str(exc)))


def _pump_stdout_lines(
    stream: Any,
    line_queue: queue.Queue[tuple[int, str, datetime] | None],
) -> None:
    """Read ``stdout`` line by line into ``line_queue`` until EOF."""
    line_number = 0
    try:
        for line in iter(stream.readline, ""):
            line_number += 1
            line_queue.put((line_number, line, datetime.now(timezone.utc)))
    except OSError as exc:
        logger.debug("Stdout stream closed during read: %s", redact_secrets(str(exc)))
    finally:
        line_queue.put(None)


def _terminate_process(proc: Any) -> int | None:
    """Terminate a timed-out subprocess cleanly without hanging."""
    try:
        proc.terminate()
    except OSError:
        return None
    try:
        return int(proc.wait(timeout=3))
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
            return int(proc.wait(timeout=3))
        except (subprocess.TimeoutExpired, OSError):
            return None


def run_streaming_process(
    *,
    command: list[str],
    workspace: Path,
    timeout_seconds: int,
    parser: AntigravityStreamParser,
    on_event: Callable[[AntigravityStreamEvent], None] | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
) -> StreamingProcessOutcome:
    """Spawn CLI subprocess and incrementally stream, parse, and emit events.

    Args:
        command: Full CLI argument vector.
        workspace: Working directory path for subprocess execution.
        timeout_seconds: Logical execution timeout before grace termination.
        parser: Stateful parser converting stdout lines into normalized events.
        on_event: Optional callback invoked synchronously as each event arrives.
        popen_factory: Process spawner matching ``subprocess.Popen`` for test injection.

    Returns:
        ``StreamingProcessOutcome`` containing ordered events, timestamps, and stderr.
    """
    started_at = datetime.now(timezone.utc)
    t0 = time.monotonic()
    deadline = t0 + timeout_seconds + 15
    command_summary = format_command_summary(command)
    logger.info("Starting streaming Antigravity CLI in %s (%s)", workspace, command_summary)

    events: list[AntigravityStreamEvent] = []
    first_event_at: datetime | None = None

    def _emit(event: AntigravityStreamEvent) -> None:
        nonlocal first_event_at
        if first_event_at is None and event.source == "provider_cli":
            first_event_at = event.timestamp
        events.append(event)
        if on_event is not None:
            on_event(event)

    try:
        proc = popen_factory(
            command,
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        finished_at = datetime.now(timezone.utc)
        duration_ms = max(0, int((time.monotonic() - t0) * 1000))
        return StreamingProcessOutcome(
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            launch_error=redact_secrets(str(exc)),
            events=events,
        )

    _emit(
        parser.make_adapter_event(
            event_type="process_started",
            status="STARTED",
            timestamp=started_at,
            redacted_payload={"command_summary": command_summary},
        )
    )

    stderr_chunks: list[str] = []
    line_queue: queue.Queue[tuple[int, str, datetime] | None] = queue.Queue()
    diag_lines: list[str] = []
    timed_out = False
    exit_code: int | None = None

    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None

    try:
        if proc.stderr is not None:
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(proc.stderr, stderr_chunks, _STDERR_CAPTURE_LIMIT),
                daemon=True,
            )
            stderr_thread.start()

        if proc.stdout is not None:
            stdout_thread = threading.Thread(
                target=_pump_stdout_lines,
                args=(proc.stdout, line_queue),
                daemon=True,
            )
            stdout_thread.start()
        else:
            line_queue.put(None)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = line_queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue

            if item is None:
                break

            line_number, line, recv_ts = item
            parsed_event = parser.parse_line(line, line_number=line_number, timestamp=recv_ts)
            if parsed_event is not None:
                if parsed_event.event_type == "stream_parse_failed" and len(diag_lines) < 20:
                    diag_lines.append(sanitize_stream_text(line.strip(), 300))
                elif parsed_event.event_type == "provider_error" and len(diag_lines) < 20:
                    msg = parsed_event.redacted_payload.get("message") or parsed_event.redacted_payload.get("response_preview")
                    if isinstance(msg, str) and msg:
                        diag_lines.append(sanitize_stream_text(msg, 300))
                _emit(parsed_event)

        if timed_out:
            exit_code = _terminate_process(proc)
        else:
            wait_remaining = max(1.0, deadline - time.monotonic())
            try:
                exit_code = int(proc.wait(timeout=wait_remaining))
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = _terminate_process(proc)
    except Exception:
        _terminate_process(proc)
        raise
    finally:
        if stdout_thread is not None:
            stdout_thread.join(timeout=2.0)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2.0)
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    finished_at = datetime.now(timezone.utc)
    duration_ms = max(0, int((time.monotonic() - t0) * 1000))

    if timed_out:
        _emit(
            parser.make_adapter_event(
                event_type="process_timed_out",
                status="FAILED",
                timestamp=finished_at,
                redacted_payload={"timeout_seconds": timeout_seconds},
            )
        )
    else:
        _emit(
            parser.make_adapter_event(
                event_type="process_exited",
                status="COMPLETED" if exit_code == 0 else "FAILED",
                timestamp=finished_at,
                redacted_payload={"exit_code": exit_code},
            )
        )

    redacted_err = redact_secrets("".join(stderr_chunks))[:2000]
    return StreamingProcessOutcome(
        started_at=started_at,
        first_event_at=first_event_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        exit_code=exit_code,
        timed_out=timed_out,
        events=events,
        redacted_stderr=redacted_err,
        redacted_stdout_diagnostics="\n".join(diag_lines)[:2000],
    )
