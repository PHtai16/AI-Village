"""Workflow lifecycle controller for cancellation, timeout, idempotency, fallback, and reconciliation."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
import threading
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    RunId,
    TaskId,
    ensure_not_secret,
    new_event_id,
    validate_aware_datetime,
    validate_safe_payload,
)
from core.contracts.run import Run, RunErrorRef
from core.contracts.task import Task
from core.tasks.fallback import (
    FallbackDecision,
    FallbackEvaluationRequest,
    FallbackPolicy,
    evaluate_fallback,
)
from core.tasks.transitions import (
    TERMINAL_RUN_STATUSES,
    TERMINAL_TASK_STATUSES,
    ActorTrustKind,
    ExecutionEvidence,
    TransitionContext,
)

logger = logging.getLogger(__name__)

HandleTerminalStatus = Literal[
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "STILL_RUNNING",
    "UNOBSERVABLE",
]
CancellationStatus = Literal[
    "CANCEL_REQUESTED",
    "CANCELLING",
    "CANCEL_CONFIRMED",
    "CANCEL_FAILED",
    "ALREADY_TERMINAL",
    "PROCESS_NOT_OBSERVABLE",
    "OWNERSHIP_REJECTED",
    "PERMISSION_DENIED",
]
TimeoutOutcomeStatus = Literal[
    "TIMED_OUT_CONFIRMED",
    "NOT_ELAPSED",
    "ALREADY_TERMINAL",
    "PROCESS_STILL_RUNNING",
    "PROCESS_NOT_OBSERVABLE",
    "OWNERSHIP_REJECTED",
]
IdempotencyOutcomeStatus = Literal[
    "ACCEPTED_NEW",
    "IDEMPOTENT_REPLAY",
    "CONFLICT_REJECTED",
    "PERMISSION_DENIED",
]


class HandleTerminationOutcome(BaseModel):
    """Structured termination status returned by an owned execution handle."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1, max_length=80)
    confirmed_stopped: bool
    already_terminal: bool = False
    terminal_status: HandleTerminalStatus
    exit_code: int | None = None
    mechanism: str = Field(default="none", min_length=1, max_length=64)
    message: str = Field(default="", max_length=1000)


@runtime_checkable
class OwnedExecutionHandle(Protocol):
    """Controlled handle for an execution explicitly owned by one Task/Run."""

    @property
    def execution_id(self) -> str:
        """Return unique execution identifier."""

    @property
    def owner_token(self) -> str:
        """Return ownership token binding this execution to its Run."""

    @property
    def pid(self) -> int | None:
        """Return OS process ID if spawned and known."""

    def is_running(self) -> bool:
        """Return True only when the owned execution process is currently observable and alive."""

    def request_cancel(
        self,
        *,
        owner_token: str,
        wait: bool = True,
        timeout_seconds: float = 5.0,
    ) -> HandleTerminationOutcome:
        """Request termination strictly for this owned handle."""

    def wait_for_exit(self, *, timeout_seconds: float = 5.0) -> HandleTerminationOutcome:
        """Wait for the owned execution to reach a stopped state."""


@runtime_checkable
class LifecyclePersistencePort(Protocol):
    """Persistence port isolating ``WorkflowLifecycleController`` from SQLite details."""

    def get_task(self, task_id: str) -> Task | None:
        """Load latest Task snapshot."""

    def get_run(self, run_id: str) -> Run | None:
        """Load latest Run snapshot."""

    def append_event(self, event: Event) -> int:
        """Append an Event to durable journal and return assigned sequence number."""

    def submit_run_atomically(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request_fingerprint: str,
        run: Run,
        timestamp: datetime,
    ) -> tuple[Literal["NEW", "DUPLICATE", "CONFLICT"], Run | None, str]:
        """Atomically check idempotency key, insert Run + COMMITTED key, or return duplicate/conflict."""

    def register_execution(
        self,
        *,
        execution_id: str,
        task_id: str,
        run_id: str,
        owner_token: str,
        pid: int | None,
        lifecycle_state: str,
        timeout_deadline_at: datetime | None,
        timestamp: datetime,
    ) -> None:
        """Record an owned execution bound to ``(task_id, run_id)``."""

    def update_execution_state(
        self,
        *,
        execution_id: str,
        lifecycle_state: str,
        pid: int | None = None,
        cancel_requested_at: datetime | None = None,
        timestamp: datetime,
    ) -> None:
        """Update execution lifecycle state in durable storage."""

    def get_execution_owner_token(self, execution_id: str) -> str | None:
        """Return stored owner_token for ``execution_id`` if registered."""

    def list_executions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return stored execution rows for ``run_id``."""

    def apply_paired_transition(
        self,
        operation: Literal["start", "complete_run_only", "fail", "timeout", "cancel"],
        task_id: str,
        run_id: str,
        context: TransitionContext,
        *,
        target_task_status: Literal["FAILED", "BLOCKED"] = "FAILED",
    ) -> tuple[bool, Task | None, Run | None, str | None, str | None]:
        """Execute paired Task/Run State Machine transition and persist atomically with Events."""

    def recover_task_state(self, task_id: str) -> dict[str, Any]:
        """Inspect persisted Task, Runs, replay consistency, and interrupted RUNNING work."""


class IdempotentSubmissionRequest(BaseModel):
    """Controlled Run submission request protected by persistent idempotency key."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    task_id: TaskId
    run: Run
    payload: dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime
    actor_id: AgentId = "core.master"

    @field_validator("scope", "idempotency_key")
    @classmethod
    def _validate_keys(cls, value: str, info: object) -> str:
        """Ensure idempotency scope and key are non-blank and free of secrets."""
        return ensure_not_secret(value, getattr(info, "field_name", "idempotency"))

    @field_validator("submitted_at")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware submission timestamp."""
        return validate_aware_datetime(value, "submitted_at")

    @field_validator("payload")
    @classmethod
    def _validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject secret-bearing submission payloads."""
        validate_safe_payload(value, "submission.payload")
        return value


class IdempotentSubmissionResult(BaseModel):
    """Result separating newly accepted submissions from idempotent replays and conflicts."""

    model_config = ConfigDict(extra="forbid")

    status: IdempotencyOutcomeStatus
    created_new: bool
    is_duplicate: bool
    task_id: TaskId
    run_id: RunId | None = None
    run: Run | None = None
    request_fingerprint: str
    message: str


class CancellationResult(BaseModel):
    """Structured cancellation result separating request acknowledgement from confirmation."""

    model_config = ConfigDict(extra="forbid")

    status: CancellationStatus
    cancel_requested: bool
    cancel_confirmed: bool
    task_id: TaskId
    run_id: RunId
    execution_id: str | None = None
    task_status: str | None = None
    run_status: str | None = None
    race_outcome: str | None = None
    message: str


class TimeoutResult(BaseModel):
    """Structured timeout result enforcing stopped-process evidence before TIMED_OUT."""

    model_config = ConfigDict(extra="forbid")

    status: TimeoutOutcomeStatus
    timed_out_confirmed: bool
    task_id: TaskId
    run_id: RunId
    execution_id: str | None = None
    task_status: str | None = None
    run_status: str | None = None
    message: str


class ReconciliationResult(BaseModel):
    """Post-restart reconciliation report for Tasks/Runs without fabricating terminal states."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    recovery_status: str
    reconciled_status: str
    process_observable: bool
    reconciliation_required: bool
    interrupted_run_ids: list[str] = Field(default_factory=list)
    event_id: str | None = None
    message: str


def compute_submission_fingerprint(request: IdempotentSubmissionRequest) -> str:
    """Compute canonical SHA-256 fingerprint over task, runtime, routing, and payload."""
    canonical = {
        "task_id": request.task_id,
        "runtime_id": request.run.runtime_id,
        "provider_id": request.run.provider_id,
        "connection_id": request.run.connection_id,
        "model_id": request.run.model_id,
        "correlation_id": request.run.correlation_id,
        "payload": request.payload,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class WorkflowLifecycleController:
    """Thread-safe coordinator binding Task/Run State Machines, owned processes, and SQLite."""

    def __init__(
        self,
        persistence: LifecyclePersistencePort,
        *,
        permission_hook: Callable[[str, str, str | None, str | None, datetime, str | None], tuple[bool, str]] | None = None,
    ) -> None:
        """Initialize lifecycle controller around a durable ``LifecyclePersistencePort``."""
        self._persistence = persistence
        self._permission_hook = permission_hook
        self._lock = threading.RLock()
        self._run_locks: dict[tuple[str, str], threading.Lock] = {}
        self._handles: dict[tuple[str, str], OwnedExecutionHandle] = {}
        self._cancel_requested_flags: set[tuple[str, str]] = set()
        self._deadlines: dict[tuple[str, str], datetime] = {}

    def _run_lock(self, task_id: str, run_id: str) -> threading.Lock:
        """Return a deterministic per-(task_id, run_id) lock serializing terminal transitions."""
        key = (task_id, run_id)
        with self._lock:
            lock = self._run_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._run_locks[key] = lock
            return lock

    def _check_permission(
        self,
        *,
        action: str,
        actor_id: str,
        task_id: str | None,
        run_id: str | None,
        timestamp: datetime,
        resource: str | None = None,
    ) -> tuple[bool, str]:
        """Invoke optional Governance permission hook before mutating workflow state."""
        if self._permission_hook is None:
            return True, "ALLOWED_NO_HOOK"
        return self._permission_hook(action, actor_id, task_id, run_id, timestamp, resource)

    def submit_idempotent_run(
        self,
        request: IdempotentSubmissionRequest,
    ) -> IdempotentSubmissionResult:
        """Submit a new Run idempotently using durable SQLite reservation."""
        allowed, perm_msg = self._check_permission(
            action="agent.execute",
            actor_id=request.actor_id,
            task_id=request.task_id,
            run_id=request.run.run_id,
            timestamp=request.submitted_at,
            resource=f"task:{request.task_id}",
        )
        fingerprint = compute_submission_fingerprint(request)
        if not allowed:
            return IdempotentSubmissionResult(
                status="PERMISSION_DENIED",
                created_new=False,
                is_duplicate=False,
                task_id=request.task_id,
                run_id=None,
                run=None,
                request_fingerprint=fingerprint,
                message=perm_msg,
            )

        if request.run.task_id != request.task_id:
            return IdempotentSubmissionResult(
                status="CONFLICT_REJECTED",
                created_new=False,
                is_duplicate=False,
                task_id=request.task_id,
                run_id=None,
                run=None,
                request_fingerprint=fingerprint,
                message=f"Run.task_id '{request.run.task_id}' does not match '{request.task_id}'.",
            )

        outcome_kind, persisted_run, detail_msg = self._persistence.submit_run_atomically(
            scope=request.scope,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            run=request.run,
            timestamp=request.submitted_at,
        )
        if outcome_kind == "CONFLICT":
            return IdempotentSubmissionResult(
                status="CONFLICT_REJECTED",
                created_new=False,
                is_duplicate=True,
                task_id=request.task_id,
                run_id=persisted_run.run_id if persisted_run is not None else None,
                run=None,
                request_fingerprint=fingerprint,
                message=detail_msg,
            )
        if outcome_kind == "DUPLICATE":
            return IdempotentSubmissionResult(
                status="IDEMPOTENT_REPLAY",
                created_new=False,
                is_duplicate=True,
                task_id=request.task_id,
                run_id=persisted_run.run_id if persisted_run is not None else None,
                run=persisted_run,
                request_fingerprint=fingerprint,
                message=detail_msg,
            )

        assert persisted_run is not None
        return IdempotentSubmissionResult(
            status="ACCEPTED_NEW",
            created_new=True,
            is_duplicate=False,
            task_id=request.task_id,
            run_id=persisted_run.run_id,
            run=persisted_run,
            request_fingerprint=fingerprint,
            message=detail_msg,
        )

    def register_owned_execution(
        self,
        task_id: str,
        run_id: str,
        handle: OwnedExecutionHandle,
        *,
        timestamp: datetime,
        timeout_deadline_at: datetime | None = None,
    ) -> None:
        """Bind an owned execution handle to ``(task_id, run_id)`` and persist its metadata."""
        validate_aware_datetime(timestamp, "timestamp")
        if timeout_deadline_at is not None:
            validate_aware_datetime(timeout_deadline_at, "timeout_deadline_at")
        run = self._persistence.get_run(run_id)
        if run is None or run.task_id != task_id:
            raise ValueError(f"Run '{run_id}' does not belong to Task '{task_id}'.")

        self._persistence.register_execution(
            execution_id=handle.execution_id,
            task_id=task_id,
            run_id=run_id,
            owner_token=handle.owner_token,
            pid=handle.pid,
            lifecycle_state="RUNNING" if handle.is_running() else "REGISTERED",
            timeout_deadline_at=timeout_deadline_at,
            timestamp=timestamp,
        )
        with self._lock:
            key = (task_id, run_id)
            self._handles[key] = handle
            if timeout_deadline_at is not None:
                self._deadlines[key] = timeout_deadline_at

    def check_checkpoint_can_continue(self, task_id: str, run_id: str) -> bool:
        """Return False before starting a new workflow stage if cancel was requested or terminal."""
        with self._lock:
            if (task_id, run_id) in self._cancel_requested_flags:
                return False
        task = self._persistence.get_task(task_id)
        run = self._persistence.get_run(run_id)
        if task is None or run is None or run.task_id != task_id:
            return False
        if task.status in TERMINAL_TASK_STATUSES or run.status in TERMINAL_RUN_STATUSES:
            return False
        return True

    def complete_owned_run(
        self,
        task_id: str,
        run_id: str,
        context: TransitionContext,
    ) -> tuple[bool, Task | None, Run | None, str | None, str | None]:
        """Commit natural Run completion while serializing against concurrent cancel/timeout."""
        with self._run_lock(task_id, run_id):
            task = self._persistence.get_task(task_id)
            run = self._persistence.get_run(run_id)
            if task is None or run is None or run.task_id != task_id:
                return False, task, run, "WRONG_TASK_RUN_RELATIONSHIP", "Run does not belong to Task."
            if run.status in TERMINAL_RUN_STATUSES or task.status in TERMINAL_TASK_STATUSES:
                return False, task, run, "TERMINAL_STATE_MUTATION", f"Run already terminal ({run.status})."

            ok, new_task, new_run, err_code, err_msg = self._persistence.apply_paired_transition(
                "complete_run_only",
                task_id,
                run_id,
                context,
            )
            if ok:
                handle = self._handles.get((task_id, run_id))
                if handle is not None:
                    self._persistence.update_execution_state(
                        execution_id=handle.execution_id,
                        lifecycle_state="SUCCEEDED",
                        pid=handle.pid,
                        timestamp=context.timestamp,
                    )
            return ok, new_task, new_run, err_code, err_msg

    def request_cancel(
        self,
        task_id: str,
        run_id: str,
        *,
        actor_id: str,
        actor_kind: ActorTrustKind,
        timestamp: datetime,
        reason: str,
        owner_token: str | None = None,
        wait: bool = True,
        wait_timeout: float = 5.0,
    ) -> CancellationResult:
        """Request cancellation, stop only the owned process, and confirm Run.CANCELLED only with proof."""
        validate_aware_datetime(timestamp, "timestamp")
        ensure_not_secret(reason, "reason")

        allowed, perm_msg = self._check_permission(
            action="task.cancel",
            actor_id=actor_id,
            task_id=task_id,
            run_id=run_id,
            timestamp=timestamp,
            resource=f"task:{task_id}/run:{run_id}",
        )
        if not allowed:
            return CancellationResult(
                status="PERMISSION_DENIED",
                cancel_requested=False,
                cancel_confirmed=False,
                task_id=task_id,
                run_id=run_id,
                message=perm_msg,
            )

        with self._run_lock(task_id, run_id):
            task = self._persistence.get_task(task_id)
            run = self._persistence.get_run(run_id)
            if task is None or run is None or run.task_id != task_id:
                return CancellationResult(
                    status="OWNERSHIP_REJECTED",
                    cancel_requested=False,
                    cancel_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    message=f"Run '{run_id}' does not belong to Task '{task_id}'.",
                )

            if task.status in TERMINAL_TASK_STATUSES or run.status in TERMINAL_RUN_STATUSES:
                return CancellationResult(
                    status="ALREADY_TERMINAL",
                    cancel_requested=False,
                    cancel_confirmed=run.status == "CANCELLED",
                    task_id=task_id,
                    run_id=run_id,
                    task_status=task.status,
                    run_status=run.status,
                    race_outcome="already_terminal_before_cancel",
                    message=f"Task ({task.status}) / Run ({run.status}) already terminal; refusing overwrite.",
                )

            handle = self._handles.get((task_id, run_id))
            if handle is not None and owner_token is not None and handle.owner_token != owner_token:
                return CancellationResult(
                    status="OWNERSHIP_REJECTED",
                    cancel_requested=False,
                    cancel_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    execution_id=handle.execution_id,
                    task_status=task.status,
                    run_status=run.status,
                    message="Caller owner_token does not match registered execution owner.",
                )

            with self._lock:
                self._cancel_requested_flags.add((task_id, run_id))

            # Record explicit cancel_requested Event distinct from confirmed run.cancelled.
            req_event = Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="workflow.cancel_requested",
                source="core_runtime",
                task_id=task_id,
                run_id=run_id,
                actor_id=actor_id,
                redacted_payload={
                    "reason": reason,
                    "execution_id": handle.execution_id if handle else None,
                    "has_observable_handle": handle is not None,
                },
            )
            self._persistence.append_event(req_event)

            if handle is None:
                # Check if DB knows about executions from a previous process.
                execs = self._persistence.list_executions_for_run(run_id)
                if execs or run.status == "RUNNING":
                    return CancellationResult(
                        status="PROCESS_NOT_OBSERVABLE",
                        cancel_requested=True,
                        cancel_confirmed=False,
                        task_id=task_id,
                        run_id=run_id,
                        execution_id=execs[-1]["execution_id"] if execs else None,
                        task_status=task.status,
                        run_status=run.status,
                        message="No live owned handle in this process; refusing fabricated CANCELLED.",
                    )

                # Safe checkpoint cancel before any process started (e.g. Run still PENDING).
                cancel_ctx = TransitionContext(
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                    timestamp=timestamp,
                    reason=reason,
                    execution_evidence=ExecutionEvidence(
                        source="core_runtime",
                        run_id=run_id,
                        reason=reason,
                        confirmed_cancelled=True,
                        cancellation_source="core_runtime",
                    ),
                )
                ok, new_task, new_run, _, err_msg = self._persistence.apply_paired_transition(
                    "cancel", task_id, run_id, cancel_ctx
                )
                return CancellationResult(
                    status="CANCEL_CONFIRMED" if ok else "CANCEL_FAILED",
                    cancel_requested=True,
                    cancel_confirmed=ok,
                    task_id=task_id,
                    run_id=run_id,
                    task_status=new_task.status if new_task else task.status,
                    run_status=new_run.status if new_run else run.status,
                    message="Cancelled at checkpoint before execution start." if ok else (err_msg or "Cancel failed."),
                )

            self._persistence.update_execution_state(
                execution_id=handle.execution_id,
                lifecycle_state="CANCELLING",
                pid=handle.pid,
                cancel_requested_at=timestamp,
                timestamp=timestamp,
            )

            outcome = handle.request_cancel(
                owner_token=handle.owner_token,
                wait=wait,
                timeout_seconds=wait_timeout,
            )
            if not wait and not outcome.confirmed_stopped:
                return CancellationResult(
                    status="CANCELLING",
                    cancel_requested=True,
                    cancel_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    execution_id=handle.execution_id,
                    task_status=task.status,
                    run_status=run.status,
                    message="Cancellation signaled; awaiting owned process termination confirmation.",
                )

            if outcome.already_terminal and outcome.terminal_status == "SUCCEEDED":
                self._persistence.update_execution_state(
                    execution_id=handle.execution_id,
                    lifecycle_state="SUCCEEDED",
                    pid=handle.pid,
                    timestamp=timestamp,
                )
                return CancellationResult(
                    status="ALREADY_TERMINAL",
                    cancel_requested=True,
                    cancel_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    execution_id=handle.execution_id,
                    task_status=task.status,
                    run_status=run.status,
                    race_outcome="execution_completed_before_cancel",
                    message="Owned execution already completed before cancellation could take effect.",
                )

            if not outcome.confirmed_stopped:
                return CancellationResult(
                    status="CANCEL_FAILED",
                    cancel_requested=True,
                    cancel_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    execution_id=handle.execution_id,
                    task_status=task.status,
                    run_status=run.status,
                    message=outcome.message or "Owned process did not confirm termination.",
                )

            self._persistence.update_execution_state(
                execution_id=handle.execution_id,
                lifecycle_state="CANCEL_CONFIRMED",
                pid=handle.pid,
                cancel_requested_at=timestamp,
                timestamp=timestamp,
            )
            cancel_ctx = TransitionContext(
                actor_id=actor_id,
                actor_kind=actor_kind,
                timestamp=timestamp,
                reason=reason,
                execution_evidence=ExecutionEvidence(
                    source="process_manager",
                    run_id=run_id,
                    reason=f"{reason} (mechanism={outcome.mechanism})",
                    confirmed_cancelled=True,
                    cancellation_source="process_manager",
                ),
            )
            ok, new_task, new_run, _, err_msg = self._persistence.apply_paired_transition(
                "cancel", task_id, run_id, cancel_ctx
            )
            return CancellationResult(
                status="CANCEL_CONFIRMED" if ok else "CANCEL_FAILED",
                cancel_requested=True,
                cancel_confirmed=ok,
                task_id=task_id,
                run_id=run_id,
                execution_id=handle.execution_id,
                task_status=new_task.status if new_task else task.status,
                run_status=new_run.status if new_run else run.status,
                message="Owned execution terminated and Task/Run cancelled atomically." if ok else (err_msg or "Cancel transition rejected."),
            )

    def handle_timeout(
        self,
        task_id: str,
        run_id: str,
        *,
        actor_id: str,
        timestamp: datetime,
        reason: str = "Workflow execution exceeded deadline.",
        target_task_status: Literal["FAILED", "BLOCKED"] = "FAILED",
        wait_timeout: float = 5.0,
    ) -> TimeoutResult:
        """Stop owned process cleanly when deadline elapses before committing Run.TIMED_OUT."""
        validate_aware_datetime(timestamp, "timestamp")
        ensure_not_secret(reason, "reason")

        with self._run_lock(task_id, run_id):
            task = self._persistence.get_task(task_id)
            run = self._persistence.get_run(run_id)
            if task is None or run is None or run.task_id != task_id:
                return TimeoutResult(
                    status="OWNERSHIP_REJECTED",
                    timed_out_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    message=f"Run '{run_id}' does not belong to Task '{task_id}'.",
                )

            if task.status in TERMINAL_TASK_STATUSES or run.status in TERMINAL_RUN_STATUSES:
                return TimeoutResult(
                    status="ALREADY_TERMINAL",
                    timed_out_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    task_status=task.status,
                    run_status=run.status,
                    message=f"Run already terminal ({run.status}); ignoring duplicate timeout.",
                )

            deadline = self._deadlines.get((task_id, run_id))
            if deadline is not None and timestamp < deadline:
                return TimeoutResult(
                    status="NOT_ELAPSED",
                    timed_out_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    task_status=task.status,
                    run_status=run.status,
                    message=f"Deadline {deadline.isoformat()} has not elapsed at {timestamp.isoformat()}.",
                )

            handle = self._handles.get((task_id, run_id))
            if handle is None:
                return TimeoutResult(
                    status="PROCESS_NOT_OBSERVABLE",
                    timed_out_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    task_status=task.status,
                    run_status=run.status,
                    message="Cannot confirm Run.TIMED_OUT without an owned handle verifying process stop.",
                )

            outcome = handle.request_cancel(
                owner_token=handle.owner_token,
                wait=True,
                timeout_seconds=wait_timeout,
            )
            if not outcome.confirmed_stopped:
                return TimeoutResult(
                    status="PROCESS_STILL_RUNNING",
                    timed_out_confirmed=False,
                    task_id=task_id,
                    run_id=run_id,
                    execution_id=handle.execution_id,
                    task_status=task.status,
                    run_status=run.status,
                    message="Owned process failed to terminate on timeout; refusing false TIMED_OUT.",
                )

            self._persistence.update_execution_state(
                execution_id=handle.execution_id,
                lifecycle_state="TIMED_OUT",
                pid=handle.pid,
                timestamp=timestamp,
            )
            timeout_ctx = TransitionContext(
                actor_id=actor_id,
                actor_kind="core_runtime",
                timestamp=timestamp,
                reason=reason,
                execution_evidence=ExecutionEvidence(
                    source="process_manager",
                    run_id=run_id,
                    reason=f"{reason} (mechanism={outcome.mechanism})",
                    error_reference=RunErrorRef(
                        error_code="TIMED_OUT",
                        stage="workflow_timeout",
                        message=reason,
                        retryable=False,
                    ),
                ),
            )
            ok, new_task, new_run, _, err_msg = self._persistence.apply_paired_transition(
                "timeout",
                task_id,
                run_id,
                timeout_ctx,
                target_task_status=target_task_status,
            )
            return TimeoutResult(
                status="TIMED_OUT_CONFIRMED" if ok else "PROCESS_STILL_RUNNING",
                timed_out_confirmed=ok,
                task_id=task_id,
                run_id=run_id,
                execution_id=handle.execution_id,
                task_status=new_task.status if new_task else task.status,
                run_status=new_run.status if new_run else run.status,
                message="Owned process stopped and Run.TIMED_OUT committed." if ok else (err_msg or "Timeout transition rejected."),
            )

    def reconcile_interrupted_task(
        self,
        task_id: str,
        *,
        timestamp: datetime,
        actor_id: AgentId = "core.master",
    ) -> ReconciliationResult:
        """Reconcile persisted RUNNING work after restart without fabricating COMPLETED or FAILED."""
        validate_aware_datetime(timestamp, "timestamp")
        report = self._persistence.recover_task_state(task_id)
        recovery_status = str(report["recovery_status"])
        interrupted = report.get("interrupted_work", [])
        interrupted_run_ids = [
            item["entity_id"] for item in interrupted if item.get("entity_type") == "run"
        ]

        observable = False
        for rid in interrupted_run_ids:
            handle = self._handles.get((task_id, rid))
            if handle is not None and handle.is_running():
                observable = True
            else:
                for exec_row in self._persistence.list_executions_for_run(rid):
                    if exec_row.get("lifecycle_state") in {"REGISTERED", "RUNNING", "CANCELLING"}:
                        self._persistence.update_execution_state(
                            execution_id=str(exec_row["execution_id"]),
                            lifecycle_state="UNOBSERVABLE",
                            pid=exec_row.get("pid"),
                            timestamp=timestamp,
                        )

        needs_reconciliation = bool(interrupted) and not observable
        snapshot_status = report.get("snapshot_status") or "UNKNOWN"
        reconciled_status = "NEEDS_RECONCILIATION" if needs_reconciliation else str(snapshot_status)

        ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="workflow.reconciliation_recorded",
            source="core_runtime",
            task_id=task_id,
            actor_id=actor_id,
            redacted_payload={
                "recovery_status": recovery_status,
                "reconciled_status": reconciled_status,
                "process_observable": observable,
                "interrupted_run_ids": interrupted_run_ids,
            },
        )
        self._persistence.append_event(ev)

        return ReconciliationResult(
            task_id=task_id,
            recovery_status=recovery_status,
            reconciled_status=reconciled_status,
            process_observable=observable,
            reconciliation_required=needs_reconciliation,
            interrupted_run_ids=interrupted_run_ids,
            event_id=ev.event_id,
            message=(
                "Interrupted RUNNING execution is unobservable after restart; flagged NEEDS_RECONCILIATION."
                if needs_reconciliation
                else f"Task reconciled cleanly in state {reconciled_status}."
            ),
        )

    def evaluate_and_record_fallback(
        self,
        policy: FallbackPolicy,
        request: FallbackEvaluationRequest,
    ) -> FallbackDecision:
        """Evaluate fallback policy fail-closed and record the decision Event to journal."""
        allowed, perm_msg = self._check_permission(
            action="connection.change_provider"
            if (
                request.candidate_target is not None
                and request.candidate_target.provider_id != request.current_target.provider_id
            )
            else "agent.execute",
            actor_id=request.actor_id,
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.requested_at,
            resource=f"task:{request.task_id}/run:{request.run_id}",
        )
        if not allowed:
            deny_ev = Event(
                timestamp=request.requested_at,
                event_type="workflow.fallback_evaluated",
                source="governance",
                task_id=request.task_id,
                run_id=request.run_id,
                actor_id=request.actor_id,
                redacted_payload={
                    "decision": "DENY",
                    "reason_code": "PERMISSION_DENIED",
                    "message": perm_msg,
                },
            )
            self._persistence.append_event(deny_ev)
            return FallbackDecision(
                decision="DENY",
                reason_code="PERMISSION_DENIED",
                message=perm_msg,
                audit_event=deny_ev,
            )

        decision = evaluate_fallback(policy, request)
        self._persistence.append_event(decision.audit_event)
        return decision
