"""Real-time NDJSON streaming parser and pipe reader for Antigravity CLI output."""

import ctypes
from ctypes import wintypes as wt
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Literal
import uuid

from pydantic import BaseModel, Field

from engines.antigravity.diagnostics import format_command_summary, redact_secrets
from engines.antigravity.models import AntigravityStreamEvent, AntigravityTelemetry

logger = logging.getLogger(__name__)

_STDERR_CAPTURE_LIMIT = 65536
_TRUNCATED_SUFFIX = "...[truncated]"
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFO = 9
_PROCESS_QUERY_LIMITED_INFO = 0x1000
_STILL_ACTIVE = 259
PopenFactory = Callable[..., Any]


class _JobObjectBasicLimitInfo(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInfo(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInfo),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_KERNEL32_CACHE: Any | None = None


def _get_kernel32() -> Any | None:
    """Return configured Win32 ``kernel32`` DLL bindings on Windows, else None."""
    global _KERNEL32_CACHE
    if os.name != "nt":
        return None
    if _KERNEL32_CACHE is not None:
        return _KERNEL32_CACHE
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
    dll.CreateJobObjectW.restype = wt.HANDLE
    dll.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
    dll.SetInformationJobObject.restype = wt.BOOL
    dll.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    dll.AssignProcessToJobObject.restype = wt.BOOL
    dll.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    dll.TerminateJobObject.restype = wt.BOOL
    dll.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    dll.OpenProcess.restype = wt.HANDLE
    dll.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    dll.GetExitCodeProcess.restype = wt.BOOL
    dll.CloseHandle.argtypes = [wt.HANDLE]
    dll.CloseHandle.restype = wt.BOOL
    _KERNEL32_CACHE = dll
    return dll


def is_pid_alive(pid: int | None) -> bool:
    """Return True if the specified OS PID is still running, without affecting other PIDs."""
    if pid is None or pid <= 0:
        return False
    kernel32 = _get_kernel32()
    if kernel32 is not None:
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFO, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = wt.DWORD(0)
            ok = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
            return ok and int(exit_code.value) == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class Win32JobGuard:
    """Per-invocation Win32 Job Object isolating child process-tree termination."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job_handle: Any | None = None
        self.attached = False

    def attach(self, proc: Any) -> bool:
        """Bind a freshly spawned process handle into an exclusive Job Object."""
        kernel32 = _get_kernel32()
        raw_handle = getattr(proc, "_handle", None)
        if kernel32 is None or not _is_real_int(raw_handle) or int(raw_handle) <= 0:
            return False

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False

        info = _JobObjectExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok_limit = bool(
            kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFO,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        )
        if not ok_limit:
            kernel32.CloseHandle(job)
            return False

        ok_assign = bool(kernel32.AssignProcessToJobObject(job, wt.HANDLE(int(raw_handle))))
        if not ok_assign:
            kernel32.CloseHandle(job)
            return False

        with self._lock:
            self._job_handle = job
            self.attached = True
        return True

    def terminate_tree(self, exit_code: int = 1) -> bool:
        """Terminate only the subprocess tree attached to this Job Object."""
        kernel32 = _get_kernel32()
        with self._lock:
            job = self._job_handle
            self._job_handle = None
        if kernel32 is None or not job:
            return False
        try:
            return bool(kernel32.TerminateJobObject(job, int(exit_code)))
        finally:
            kernel32.CloseHandle(job)

    def close(self) -> None:
        """Close the Job Object handle cleanly during post-exit cleanup."""
        kernel32 = _get_kernel32()
        with self._lock:
            job = self._job_handle
            self._job_handle = None
        if kernel32 is not None and job:
            kernel32.CloseHandle(job)


class ProcessHandleController:
    """Thread-safe controller owning a single spawned process and its termination."""

    def __init__(self, cancel_event: threading.Event | None = None) -> None:
        self._lock = threading.RLock()
        self._term_lock = threading.Lock()
        self.cancel_event = cancel_event or threading.Event()
        self.cancel_requested_at: datetime | None = None
        self.proc: Any | None = None
        self.pid: int | None = None
        self.job_guard = Win32JobGuard()
        self.completed_cleanly = False
        self.termination_done = False
        self.exit_code: int | None = None
        self.process_tree_terminated = False
        self.termination_mechanism: Literal[
            "none",
            "win32_job_object",
            "terminate_only",
            "kill_fallback",
        ] = "none"

    def bind_process(self, proc: Any, job_guard: Win32JobGuard) -> None:
        """Attach an owned ``Popen`` process and its ``Win32JobGuard``."""
        with self._lock:
            self.proc = proc
            self.job_guard = job_guard
            raw_pid = getattr(proc, "pid", None)
            self.pid = int(raw_pid) if _is_real_int(raw_pid) and int(raw_pid) > 0 else None

    def mark_completed(self) -> None:
        """Record natural CLI completion before any subsequent cancellation request."""
        with self._lock:
            self.completed_cleanly = True

    def signal_cancel(self, *, requested_at: datetime | None = None) -> bool:
        """Set cancel flag idempotently and initiate non-blocking process termination."""
        ts = requested_at or datetime.now(timezone.utc)
        with self._lock:
            first_request = not self.cancel_event.is_set()
            if first_request:
                self.cancel_requested_at = ts
            self.cancel_event.set()
            proc = self.proc
            already_completed = self.completed_cleanly

        if proc is not None and not already_completed:
            try:
                proc.terminate()
            except OSError:
                pass
        return first_request

    def terminate_owned_process(
        self,
        grace_seconds: float = 3.0,
    ) -> tuple[int | None, bool, Literal["none", "win32_job_object", "terminate_only", "kill_fallback"]]:
        """Gracefully terminate the owned process, escalating to Win32 Job Object or kill."""
        with self._term_lock:
            if self.termination_done:
                return (
                    self.exit_code,
                    self.process_tree_terminated,
                    self.termination_mechanism,
                )

            proc = self.proc
            guard = self.job_guard
            if proc is None:
                self.termination_done = True
                return (None, True, "none")

            mechanism: Literal[
                "none",
                "win32_job_object",
                "terminate_only",
                "kill_fallback",
            ] = "terminate_only"
            tree_terminated = False
            rc: int | None = None

            try:
                proc.terminate()
                rc = int(proc.wait(timeout=max(0.05, grace_seconds)))
                if guard.attached:
                    guard.terminate_tree(rc if rc != 0 else 1)
                    mechanism = "win32_job_object"
                    tree_terminated = True
            except subprocess.TimeoutExpired:
                if guard.attached:
                    guard.terminate_tree(1)
                    mechanism = "win32_job_object"
                    tree_terminated = True
                    try:
                        rc = int(proc.wait(timeout=3.0))
                    except (subprocess.TimeoutExpired, OSError):
                        rc = None
                else:
                    try:
                        proc.kill()
                        rc = int(proc.wait(timeout=3.0))
                        mechanism = "kill_fallback"
                    except (subprocess.TimeoutExpired, OSError):
                        mechanism = "kill_fallback"
                        rc = None
            except OSError:
                if guard.attached:
                    guard.terminate_tree(1)
                    mechanism = "win32_job_object"
                    tree_terminated = True

            self.termination_done = True
            self.exit_code = rc
            self.process_tree_terminated = tree_terminated
            self.termination_mechanism = mechanism
            return (rc, tree_terminated, mechanism)


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
    pid: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    cancel_requested_at: datetime | None = None
    process_tree_terminated: bool = False
    termination_mechanism: Literal[
        "none",
        "win32_job_object",
        "terminate_only",
        "kill_fallback",
    ] = "none"
    events_dropped_count: int = Field(default=0, ge=0)
    termination_race_note: str | None = None
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
    except (OSError, ValueError) as exc:
        logger.debug("Stderr stream closed during read: %s", redact_secrets(str(exc)))


def _pump_stdout_lines(
    stream: Any,
    line_queue: queue.Queue[tuple[int, str, datetime] | None],
    stop_event: threading.Event | None = None,
) -> None:
    """Read ``stdout`` line by line into ``line_queue`` until EOF or stop signal."""
    line_number = 0
    try:
        for line in iter(stream.readline, ""):
            if stop_event is not None and stop_event.is_set():
                break
            line_number += 1
            item = (line_number, line, datetime.now(timezone.utc))
            while stop_event is None or not stop_event.is_set():
                try:
                    line_queue.put(item, timeout=0.05)
                    break
                except queue.Full:
                    continue
    except (OSError, ValueError) as exc:
        logger.debug("Stdout stream closed during read: %s", redact_secrets(str(exc)))
    finally:
        for _ in range(20):
            try:
                line_queue.put(None, timeout=0.05)
                break
            except queue.Full:
                if stop_event is not None and stop_event.is_set():
                    break


def run_streaming_process(
    *,
    command: list[str],
    workspace: Path,
    timeout_seconds: int,
    parser: AntigravityStreamParser,
    on_event: Callable[[AntigravityStreamEvent], None] | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
    cancel_event: threading.Event | None = None,
    controller: ProcessHandleController | None = None,
    hard_timeout_seconds: float | None = None,
    grace_seconds: float = 3.0,
    max_events: int = 10000,
    max_queue_size: int = 2048,
    on_process_started: Callable[[int | None, datetime], None] | None = None,
) -> StreamingProcessOutcome:
    """Spawn CLI subprocess and incrementally stream, parse, cancel, or time out safely.

    Args:
        command: Full CLI argument vector.
        workspace: Working directory path for subprocess execution.
        timeout_seconds: Logical execution timeout before grace termination.
        parser: Stateful parser converting stdout lines into normalized events.
        on_event: Optional callback invoked synchronously as each event arrives.
        popen_factory: Process spawner matching ``subprocess.Popen`` for test injection.
        cancel_event: Optional thread event requesting cooperative termination.
        controller: Optional handle controller shared with ``AntigravityProcessManager``.
        hard_timeout_seconds: Optional hard deadline overriding ``timeout_seconds + 15``.
        grace_seconds: Graceful shutdown wait window before Job Object or kill fallback.
        max_events: Bounded capacity for retained events before overflow reporting.
        max_queue_size: Bounded line queue capacity protecting memory on high-volume output.
        on_process_started: Optional hook receiving the OS PID right after launch.

    Returns:
        ``StreamingProcessOutcome`` containing ordered events, PID, termination details,
        and redacted diagnostics.
    """
    started_at = datetime.now(timezone.utc)
    t0 = time.monotonic()
    effective_hard = (
        float(hard_timeout_seconds)
        if hard_timeout_seconds is not None
        else float(timeout_seconds + 15)
    )
    deadline = t0 + max(0.05, effective_hard)
    command_summary = format_command_summary(command)
    logger.info("Starting streaming Antigravity CLI in %s (%s)", workspace, command_summary)

    active_controller = controller or ProcessHandleController(cancel_event=cancel_event)

    def _is_cancel_requested() -> bool:
        return active_controller.cancel_event.is_set() or (
            cancel_event is not None and cancel_event.is_set()
        )

    events: list[AntigravityStreamEvent] = []
    first_event_at: datetime | None = None
    events_dropped_count = 0
    overflow_emitted = False

    def _emit(event: AntigravityStreamEvent, *, force: bool = False) -> None:
        nonlocal first_event_at, events_dropped_count, overflow_emitted
        if first_event_at is None and event.source == "provider_cli":
            first_event_at = event.timestamp
        if force or len(events) < max_events:
            events.append(event)
            if on_event is not None:
                on_event(event)
            return

        events_dropped_count += 1
        if not overflow_emitted:
            overflow_emitted = True
            ov_evt = parser.make_adapter_event(
                event_type="event_buffer_overflow",
                status="FAILED",
                timestamp=event.timestamp,
                redacted_payload={"max_events": max_events},
            )
            events.append(ov_evt)
            if on_event is not None:
                on_event(ov_evt)

    if _is_cancel_requested():
        cancel_ts = active_controller.cancel_requested_at or started_at
        _emit(
            parser.make_adapter_event(
                event_type="process_cancelled",
                status="FAILED",
                timestamp=cancel_ts,
                redacted_payload={"reason": "cancelled_before_start"},
            ),
            force=True,
        )
        return StreamingProcessOutcome(
            started_at=started_at,
            finished_at=cancel_ts,
            duration_ms=0,
            cancelled=True,
            cancel_requested_at=cancel_ts,
            process_tree_terminated=True,
            termination_mechanism="none",
            termination_race_note="cancelled_before_start",
            events=events,
        )

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

    job_guard = Win32JobGuard()
    job_guard.attach(proc)
    active_controller.bind_process(proc, job_guard)
    pid = active_controller.pid

    if on_process_started is not None:
        on_process_started(pid, started_at)

    _emit(
        parser.make_adapter_event(
            event_type="process_started",
            status="STARTED",
            timestamp=started_at,
            redacted_payload={"command_summary": command_summary, "pid": pid},
        ),
        force=True,
    )

    stderr_chunks: list[str] = []
    line_queue: queue.Queue[tuple[int, str, datetime] | None] = queue.Queue(
        maxsize=max(1, max_queue_size)
    )
    stop_reading = threading.Event()
    diag_lines: list[str] = []
    timed_out = False
    cancelled = False
    tree_terminated = False
    mechanism: Literal["none", "win32_job_object", "terminate_only", "kill_fallback"] = "none"
    race_note: str | None = None
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
                args=(proc.stdout, line_queue, stop_reading),
                daemon=True,
            )
            stdout_thread.start()
        else:
            line_queue.put(None)

        while True:
            if _is_cancel_requested():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = line_queue.get(timeout=min(0.05, remaining))
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
                    msg = (
                        parsed_event.redacted_payload.get("message")
                        or parsed_event.redacted_payload.get("response_preview")
                    )
                    if isinstance(msg, str) and msg:
                        diag_lines.append(sanitize_stream_text(msg, 300))
                is_terminal_provider = parsed_event.event_type in (
                    "execution_completed",
                    "provider_error",
                )
                _emit(parsed_event, force=is_terminal_provider)

            if _is_cancel_requested() and not parser.completion_event_received:
                cancelled = True
                break

        if cancelled:
            if parser.completion_event_received:
                race_note = "cancel_requested_after_completion"
                cancelled = False
                active_controller.mark_completed()
                try:
                    exit_code = int(proc.wait(timeout=max(0.1, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stop_reading.set()
                    exit_code, tree_terminated, mechanism = (
                        active_controller.terminate_owned_process(grace_seconds=grace_seconds)
                    )
            else:
                stop_reading.set()
                exit_code, tree_terminated, mechanism = (
                    active_controller.terminate_owned_process(grace_seconds=grace_seconds)
                )
        elif timed_out:
            stop_reading.set()
            exit_code, tree_terminated, mechanism = active_controller.terminate_owned_process(
                grace_seconds=grace_seconds
            )
            if _is_cancel_requested():
                race_note = "cancel_requested_after_timeout"
        else:
            wait_remaining = max(0.05, deadline - time.monotonic())
            try:
                exit_code = int(proc.wait(timeout=wait_remaining))
                if parser.completion_event_received:
                    active_controller.mark_completed()
                    if _is_cancel_requested():
                        race_note = "cancel_requested_after_completion"
                elif _is_cancel_requested():
                    cancelled = True
                    stop_reading.set()
                    exit_code, tree_terminated, mechanism = (
                        active_controller.terminate_owned_process(grace_seconds=grace_seconds)
                    )
            except subprocess.TimeoutExpired:
                if _is_cancel_requested() and not parser.completion_event_received:
                    cancelled = True
                else:
                    timed_out = True
                stop_reading.set()
                exit_code, tree_terminated, mechanism = (
                    active_controller.terminate_owned_process(grace_seconds=grace_seconds)
                )
    except Exception:
        stop_reading.set()
        active_controller.terminate_owned_process(grace_seconds=grace_seconds)
        raise
    finally:
        stop_reading.set()
        if cancelled or timed_out:
            for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if stdout_thread is not None:
            stdout_thread.join(timeout=1.5)
        if stderr_thread is not None:
            stderr_thread.join(timeout=1.5)
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        job_guard.close()

    finished_at = datetime.now(timezone.utc)
    duration_ms = max(0, int((time.monotonic() - t0) * 1000))

    if cancelled:
        _emit(
            parser.make_adapter_event(
                event_type="process_cancelled",
                status="FAILED",
                timestamp=finished_at,
                redacted_payload={
                    "exit_code": exit_code,
                    "pid": pid,
                    "process_tree_terminated": tree_terminated,
                    "termination_mechanism": mechanism,
                },
            ),
            force=True,
        )
    elif timed_out:
        _emit(
            parser.make_adapter_event(
                event_type="process_timed_out",
                status="FAILED",
                timestamp=finished_at,
                redacted_payload={
                    "timeout_seconds": timeout_seconds,
                    "pid": pid,
                    "process_tree_terminated": tree_terminated,
                    "termination_mechanism": mechanism,
                },
            ),
            force=True,
        )
    else:
        _emit(
            parser.make_adapter_event(
                event_type="process_exited",
                status="COMPLETED" if exit_code == 0 else "FAILED",
                timestamp=finished_at,
                redacted_payload={"exit_code": exit_code, "pid": pid},
            ),
            force=True,
        )

    redacted_err = redact_secrets("".join(stderr_chunks))[:2000]
    return StreamingProcessOutcome(
        started_at=started_at,
        first_event_at=first_event_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        exit_code=exit_code,
        pid=pid,
        timed_out=timed_out,
        cancelled=cancelled,
        cancel_requested_at=active_controller.cancel_requested_at,
        process_tree_terminated=tree_terminated,
        termination_mechanism=mechanism,  # type: ignore[arg-type]
        events_dropped_count=events_dropped_count,
        termination_race_note=race_note,
        events=events,
        redacted_stderr=redacted_err,
        redacted_stdout_diagnostics="\n".join(diag_lines)[:2000],
    )
