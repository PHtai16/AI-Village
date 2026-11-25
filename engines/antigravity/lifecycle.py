"""Owned subprocess lifecycle manager for start, status, cancel, timeout, and await."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any, Callable
import uuid

from engines.antigravity.models import (
    AntigravityCancelOutcome,
    AntigravityExecutionStatus,
    AntigravityStreamEvent,
    AntigravityStreamRequest,
    AntigravityStreamResult,
    ProcessLifecycleState,
)
from engines.antigravity.streaming import ProcessHandleController, Win32JobGuard, is_pid_alive

if TYPE_CHECKING:
    from core.tasks.lifecycle import HandleTerminationOutcome
    from engines.antigravity.adapter import AntigravityAdapter

_TERMINAL_STATES: frozenset[str] = frozenset({"CANCELLED", "TIMED_OUT", "SUCCEEDED", "FAILED"})


class ProcessLifecycleError(ValueError):
    """Domain error raised for invalid execution lookup or ownership conflict."""

    def __init__(self, error_code: str, message: str) -> None:
        """Initialize the error with a lifecycle error code and summary."""
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code


@dataclass
class _ManagedExecution:
    """Thread-safe state container for a single owned CLI subprocess invocation."""

    execution_id: str
    session_id: str | None
    owner_token: str | None
    request: AntigravityStreamRequest
    command_summary: str
    controller: ProcessHandleController
    done_event: threading.Event = field(default_factory=threading.Event)
    worker_thread: threading.Thread | None = None
    state: ProcessLifecycleState = "CREATED"
    pid: int | None = None
    active_tool_name: str | None = None
    conversation_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    started_monotonic: float | None = None
    finished_at: datetime | None = None
    finished_monotonic: float | None = None
    exit_code: int | None = None
    events_received: int = 0
    events_dropped: int = 0
    termination_race_note: str | None = None
    result: AntigravityStreamResult | None = None
    worker_exception: Exception | None = None


class AntigravityProcessManager:
    """Thread-safe manager tracking owned Antigravity executions without global process kills."""

    def __init__(self, adapter: AntigravityAdapter | None = None) -> None:
        """Initialize the manager around an ``AntigravityAdapter`` instance."""
        if adapter is None:
            from engines.antigravity.adapter import AntigravityAdapter as _AdapterCls

            self._adapter: AntigravityAdapter = _AdapterCls()
        else:
            self._adapter = adapter
        self._lock = threading.RLock()
        self._executions: dict[str, _ManagedExecution] = {}

    def start_execution(
        self,
        request: AntigravityStreamRequest,
        *,
        execution_id: str | None = None,
        session_id: str | None = None,
        owner_token: str | None = None,
        on_event: Callable[[AntigravityStreamEvent], None] | None = None,
        hard_timeout_seconds: float | None = None,
        grace_seconds: float = 3.0,
        max_events: int = 10000,
    ) -> str:
        """Start an owned Antigravity streaming execution asynchronously and return its ID."""
        exec_id = execution_id or f"exec-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        controller = ProcessHandleController()
        initial_summary = f"agy --output-format stream-json --print=<{len(request.prompt)}_chars>"

        with self._lock:
            if exec_id in self._executions:
                raise ProcessLifecycleError(
                    "EXECUTION_CONFLICT",
                    f"Execution ID '{exec_id}' already exists.",
                )
            record = _ManagedExecution(
                execution_id=exec_id,
                session_id=session_id,
                owner_token=owner_token,
                request=request,
                command_summary=initial_summary,
                controller=controller,
                state="CREATED",
                created_at=now,
            )
            self._executions[exec_id] = record

        def _on_process_started(pid: int | None, proc_started_at: datetime) -> None:
            with self._lock:
                record.pid = pid
                record.started_at = proc_started_at
                record.started_monotonic = time.monotonic()
                if controller.cancel_event.is_set() and record.state not in _TERMINAL_STATES:
                    record.state = "CANCELLING"
                elif record.state not in _TERMINAL_STATES:
                    record.state = "RUNNING"

        def _on_stream_event(event: AntigravityStreamEvent) -> None:
            with self._lock:
                record.events_received += 1
                if event.conversation_id:
                    record.conversation_id = event.conversation_id
                if event.event_type == "tool_call_started":
                    record.active_tool_name = event.tool_name
                elif event.event_type in ("tool_call_completed", "tool_call_failed"):
                    record.active_tool_name = None
                elif event.event_type == "event_buffer_overflow":
                    record.events_dropped += 1
            if on_event is not None:
                on_event(event)

        def _worker() -> None:
            try:
                with self._lock:
                    if not controller.cancel_event.is_set() and record.state == "CREATED":
                        record.state = "STARTING"

                result = self._adapter._execute_stream_request(
                    request,
                    on_event=_on_stream_event,
                    execution_id=exec_id,
                    controller=controller,
                    hard_timeout_seconds=hard_timeout_seconds,
                    grace_seconds=grace_seconds,
                    max_events=max_events,
                    on_process_started=_on_process_started,
                )
                with self._lock:
                    record.result = result
                    record.pid = result.pid if result.pid is not None else record.pid
                    if record.started_at is None:
                        record.started_at = result.started_at
                    record.finished_at = result.finished_at
                    record.finished_monotonic = time.monotonic()
                    record.exit_code = result.exit_code
                    record.events_dropped = result.events_dropped_count
                    record.active_tool_name = None
                    record.command_summary = result.command_summary
                    if result.conversation_id:
                        record.conversation_id = result.conversation_id
                    if result.status == "SUCCEEDED":
                        record.state = "SUCCEEDED"
                        if controller.cancel_event.is_set():
                            record.termination_race_note = "cancel_requested_after_completion"
                    elif result.status == "TIMED_OUT":
                        record.state = "TIMED_OUT"
                    elif result.status == "CANCELLED":
                        record.state = "CANCELLED"
                    else:
                        record.state = "FAILED"
            except Exception as exc:
                with self._lock:
                    record.worker_exception = exc
                    record.finished_at = datetime.now(timezone.utc)
                    record.finished_monotonic = time.monotonic()
                    record.active_tool_name = None
                    record.state = "FAILED"
            finally:
                record.done_event.set()

        worker = threading.Thread(
            target=_worker,
            name=f"agy-exec-{exec_id}",
            daemon=True,
        )
        with self._lock:
            record.worker_thread = worker
            if not controller.cancel_event.is_set():
                record.state = "STARTING"
        worker.start()
        return exec_id

    @staticmethod
    def _build_status(record: _ManagedExecution) -> AntigravityExecutionStatus:
        """Construct an ``AntigravityExecutionStatus`` snapshot from ``record`` under lock."""
        if record.started_monotonic is None:
            elapsed_ms = 0
        elif record.finished_monotonic is not None:
            elapsed_ms = max(0, int((record.finished_monotonic - record.started_monotonic) * 1000))
        else:
            elapsed_ms = max(0, int((time.monotonic() - record.started_monotonic) * 1000))

        is_terminal = record.state in _TERMINAL_STATES
        process_running = (
            record.state in ("RUNNING", "CANCEL_REQUESTED", "CANCELLING")
            and not record.done_event.is_set()
        )
        return AntigravityExecutionStatus(
            execution_id=record.execution_id,
            session_id=record.session_id,
            state=record.state,
            is_terminal=is_terminal,
            process_running=process_running,
            active_tool_name=record.active_tool_name,
            pid=record.pid,
            conversation_id=record.conversation_id,
            command_summary=record.command_summary,
            created_at=record.created_at,
            started_at=record.started_at,
            cancel_requested_at=record.controller.cancel_requested_at,
            finished_at=record.finished_at,
            elapsed_ms=elapsed_ms,
            exit_code=record.exit_code,
            events_received=record.events_received,
            events_dropped=record.events_dropped,
            termination_race_note=record.termination_race_note,
        )

    def get_execution_status(self, execution_id: str) -> AntigravityExecutionStatus:
        """Return a thread-safe live status snapshot for ``execution_id``."""
        with self._lock:
            record = self._executions.get(execution_id)
            if record is None:
                raise ProcessLifecycleError(
                    "EXECUTION_NOT_FOUND",
                    f"Unknown execution_id: {execution_id}",
                )
            return self._build_status(record)

    def list_executions(
        self,
        *,
        active_only: bool = False,
        session_id: str | None = None,
    ) -> list[AntigravityExecutionStatus]:
        """List tracked executions optionally filtered by session or non-terminal state."""
        with self._lock:
            results: list[AntigravityExecutionStatus] = []
            for record in self._executions.values():
                if session_id is not None and record.session_id != session_id:
                    continue
                if active_only and record.state in _TERMINAL_STATES:
                    continue
                results.append(self._build_status(record))
            return results

    def active_execution_ids(self, session_id: str) -> list[str]:
        """Return execution IDs for ``session_id`` that are not yet in a terminal state."""
        with self._lock:
            return [
                record.execution_id
                for record in self._executions.values()
                if record.session_id == session_id and record.state not in _TERMINAL_STATES
            ]

    def has_active_execution(self, session_id: str) -> bool:
        """Return True if ``session_id`` currently owns at least one active execution."""
        return len(self.active_execution_ids(session_id)) > 0

    def request_cancel(
        self,
        execution_id: str,
        *,
        owner_token: str | None = None,
        wait: bool = False,
        wait_timeout: float = 10.0,
    ) -> AntigravityCancelOutcome:
        """Request cooperative and Win32-job-enforced cancellation for ``execution_id``."""
        now = datetime.now(timezone.utc)
        with self._lock:
            record = self._executions.get(execution_id)
            if record is None:
                raise ProcessLifecycleError(
                    "EXECUTION_NOT_FOUND",
                    f"Unknown execution_id: {execution_id}",
                )
            if record.owner_token is not None and owner_token != record.owner_token:
                raise ProcessLifecycleError(
                    "EXECUTION_CONFLICT",
                    f"Caller is not authorized to cancel execution '{execution_id}'.",
                )

            state_at_request = record.state
            if state_at_request in _TERMINAL_STATES:
                cancel_ts = record.controller.cancel_requested_at or now
                return AntigravityCancelOutcome(
                    execution_id=execution_id,
                    accepted=False,
                    already_terminal=True,
                    idempotent_repeat=True,
                    state_at_request=state_at_request,
                    final_state=state_at_request,
                    cancel_requested_at=cancel_ts,
                    message=f"Execution '{execution_id}' already reached terminal state {state_at_request}.",
                )

            first_cancel = record.controller.signal_cancel(requested_at=now)
            if first_cancel:
                record.state = "CANCELLING" if record.controller.proc is not None else "CANCEL_REQUESTED"
            cancel_ts = record.controller.cancel_requested_at or now
            done_event = record.done_event
            controller = record.controller

        if wait:
            if not done_event.wait(timeout=min(1.0, max(0.1, wait_timeout))):
                controller.terminate_owned_process(grace_seconds=0.5)
                done_event.wait(timeout=max(0.1, wait_timeout - 1.0))

        with self._lock:
            final_state: ProcessLifecycleState | None = (
                record.state if record.state in _TERMINAL_STATES else None
            )

        msg = (
            f"Cancellation repeated for '{execution_id}'."
            if not first_cancel
            else f"Cancellation requested for '{execution_id}'."
        )
        return AntigravityCancelOutcome(
            execution_id=execution_id,
            accepted=True,
            already_terminal=False,
            idempotent_repeat=not first_cancel,
            state_at_request=state_at_request,
            final_state=final_state,
            cancel_requested_at=cancel_ts,
            message=msg,
        )

    def await_result(
        self,
        execution_id: str,
        *,
        timeout: float | None = None,
    ) -> AntigravityStreamResult:
        """Block until ``execution_id`` finishes or ``timeout`` elapses and return result."""
        with self._lock:
            record = self._executions.get(execution_id)
            if record is None:
                raise ProcessLifecycleError(
                    "EXECUTION_NOT_FOUND",
                    f"Unknown execution_id: {execution_id}",
                )
            done_event = record.done_event
            worker_thread = record.worker_thread

        finished = done_event.wait(timeout=timeout)
        if not finished:
            raise TimeoutError(f"Execution '{execution_id}' did not finish within {timeout}s.")
        if worker_thread is not None and worker_thread.is_alive():
            worker_thread.join(timeout=1.0)

        with self._lock:
            if record.worker_exception is not None:
                raise record.worker_exception
            if record.result is None:
                raise ProcessLifecycleError(
                    "EXECUTION_CONFLICT",
                    f"Execution '{execution_id}' completed without producing a result.",
                )
            return record.result


class AntigravityOwnedExecutionHandle:
    """``OwnedExecutionHandle`` adapter wrapping an active ``AntigravityProcessManager`` run."""

    def __init__(
        self,
        manager: AntigravityProcessManager,
        execution_id: str,
        owner_token: str,
    ) -> None:
        """Bind an execution ID and ownership token to ``manager``."""
        self._manager = manager
        self._execution_id = execution_id
        self._owner_token = owner_token

    @property
    def execution_id(self) -> str:
        """Return unique execution identifier."""
        return self._execution_id

    @property
    def owner_token(self) -> str:
        """Return caller ownership token."""
        return self._owner_token

    @property
    def pid(self) -> int | None:
        """Return tracked OS PID if known."""
        try:
            return self._manager.get_execution_status(self._execution_id).pid
        except ProcessLifecycleError:
            return None

    def is_running(self) -> bool:
        """Return True when the managed Antigravity execution is non-terminal and running."""
        try:
            st = self._manager.get_execution_status(self._execution_id)
        except ProcessLifecycleError:
            return False
        if st.is_terminal:
            return False
        if st.pid is not None:
            return is_pid_alive(st.pid)
        return st.state in ("CREATED", "STARTING", "RUNNING", "CANCEL_REQUESTED", "CANCELLING")

    def request_cancel(
        self,
        *,
        owner_token: str,
        wait: bool = True,
        timeout_seconds: float = 5.0,
    ) -> HandleTerminationOutcome:
        """Request owned cancellation through ``AntigravityProcessManager``."""
        from core.tasks.lifecycle import HandleTerminationOutcome

        if owner_token != self._owner_token:
            raise ProcessLifecycleError(
                "EXECUTION_CONFLICT",
                f"Caller owner_token does not match execution '{self._execution_id}'.",
            )
        outcome = self._manager.request_cancel(
            self._execution_id,
            owner_token=owner_token,
            wait=wait,
            wait_timeout=timeout_seconds,
        )
        if outcome.already_terminal:
            final_st = outcome.final_state or outcome.state_at_request
            mapped = (
                "SUCCEEDED"
                if final_st == "SUCCEEDED"
                else ("CANCELLED" if final_st == "CANCELLED" else ("TIMED_OUT" if final_st == "TIMED_OUT" else "FAILED"))
            )
            return HandleTerminationOutcome(
                execution_id=self._execution_id,
                confirmed_stopped=True,
                already_terminal=True,
                terminal_status=mapped,
                message=outcome.message,
            )

        status = self._manager.get_execution_status(self._execution_id)
        stopped = status.is_terminal and (status.pid is None or not is_pid_alive(status.pid))
        terminal = (
            status.state
            if status.state in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
            else "STILL_RUNNING"
        )
        return HandleTerminationOutcome(
            execution_id=self._execution_id,
            confirmed_stopped=stopped,
            already_terminal=False,
            terminal_status=terminal,  # type: ignore[arg-type]
            exit_code=status.exit_code,
            mechanism="antigravity_process_manager",
            message=outcome.message,
        )

    def wait_for_exit(self, *, timeout_seconds: float = 5.0) -> HandleTerminationOutcome:
        """Wait for the owned Antigravity execution to complete."""
        from core.tasks.lifecycle import HandleTerminationOutcome

        try:
            res = self._manager.await_result(self._execution_id, timeout=timeout_seconds)
        except TimeoutError as exc:
            return HandleTerminationOutcome(
                execution_id=self._execution_id,
                confirmed_stopped=False,
                terminal_status="STILL_RUNNING",
                message=str(exc),
            )
        return HandleTerminationOutcome(
            execution_id=self._execution_id,
            confirmed_stopped=res.pid is None or not is_pid_alive(res.pid),
            already_terminal=True,
            terminal_status=res.status if res.status in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"} else "FAILED",  # type: ignore[arg-type]
            exit_code=res.exit_code,
            mechanism="antigravity_process_manager",
        )


class OwnedSubprocessHandle:
    """``OwnedExecutionHandle`` wrapping a single owned ``subprocess.Popen`` + ``Win32JobGuard``."""

    def __init__(
        self,
        proc: subprocess.Popen[Any],
        *,
        execution_id: str | None = None,
        owner_token: str | None = None,
        attach_job_guard: bool = True,
    ) -> None:
        """Bind ``proc`` into a dedicated ``Win32JobGuard`` without affecting sibling processes."""
        self._proc = proc
        self._execution_id = execution_id or f"exec-{uuid.uuid4().hex[:12]}"
        self._owner_token = owner_token or f"own-{uuid.uuid4().hex[:12]}"
        self._controller = ProcessHandleController()
        self._job_guard = Win32JobGuard()
        if attach_job_guard:
            self._job_guard.attach(proc)
        self._controller.bind_process(proc, self._job_guard)
        self._cancelled = False
        self._lock = threading.Lock()

    @property
    def execution_id(self) -> str:
        """Return unique execution identifier."""
        return self._execution_id

    @property
    def owner_token(self) -> str:
        """Return ownership token bound to this subprocess."""
        return self._owner_token

    @property
    def pid(self) -> int | None:
        """Return child OS process ID."""
        return self._controller.pid

    def is_running(self) -> bool:
        """Return True only while ``proc.poll() is None`` and OS PID is alive."""
        if self._proc.poll() is not None:
            return False
        return is_pid_alive(self.pid)

    def request_cancel(
        self,
        *,
        owner_token: str,
        wait: bool = True,
        timeout_seconds: float = 5.0,
    ) -> HandleTerminationOutcome:
        """Terminate strictly this owned subprocess tree via ``ProcessHandleController``."""
        from core.tasks.lifecycle import HandleTerminationOutcome

        if owner_token != self._owner_token:
            raise ProcessLifecycleError(
                "EXECUTION_CONFLICT",
                f"Caller owner_token does not match subprocess '{self._execution_id}'.",
            )
        with self._lock:
            rc_existing = self._proc.poll()
            if rc_existing is not None and not self._cancelled:
                self._job_guard.close()
                return HandleTerminationOutcome(
                    execution_id=self._execution_id,
                    confirmed_stopped=True,
                    already_terminal=True,
                    terminal_status="SUCCEEDED" if rc_existing == 0 else "FAILED",
                    exit_code=int(rc_existing),
                    mechanism="already_exited",
                    message="Subprocess exited before cancellation request.",
                )
            self._cancelled = True
            self._controller.signal_cancel()
            if not wait:
                return HandleTerminationOutcome(
                    execution_id=self._execution_id,
                    confirmed_stopped=False,
                    terminal_status="STILL_RUNNING",
                    mechanism="signal_cancel",
                    message="Cancellation signaled without waiting.",
                )
            rc, _tree, mech = self._controller.terminate_owned_process(grace_seconds=min(0.5, max(0.05, timeout_seconds)))
            self._job_guard.close()
            alive = is_pid_alive(self.pid)
            return HandleTerminationOutcome(
                execution_id=self._execution_id,
                confirmed_stopped=not alive,
                already_terminal=False,
                terminal_status="CANCELLED" if not alive else "STILL_RUNNING",
                exit_code=rc,
                mechanism=mech,
                message="Owned subprocess stopped cleanly." if not alive else "Subprocess still alive.",
            )

    def wait_for_exit(self, *, timeout_seconds: float = 5.0) -> HandleTerminationOutcome:
        """Wait for the owned subprocess to exit naturally."""
        from core.tasks.lifecycle import HandleTerminationOutcome

        try:
            rc = int(self._proc.wait(timeout=timeout_seconds))
        except subprocess.TimeoutExpired:
            return HandleTerminationOutcome(
                execution_id=self._execution_id,
                confirmed_stopped=False,
                terminal_status="STILL_RUNNING",
                mechanism="wait_timeout",
                message="Owned subprocess still running.",
            )
        self._job_guard.close()
        return HandleTerminationOutcome(
            execution_id=self._execution_id,
            confirmed_stopped=not is_pid_alive(self.pid),
            already_terminal=True,
            terminal_status="CANCELLED" if self._cancelled else ("SUCCEEDED" if rc == 0 else "FAILED"),
            exit_code=rc,
            mechanism="natural_exit",
        )
