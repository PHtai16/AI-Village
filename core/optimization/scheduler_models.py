"""Pure AST domain contracts, fail-closed errors, and ports for P11.03 Execution Scheduler."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import re
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    EventId,
    LeaseId,
    ProjectId,
    QueueEntryId,
    RunId,
    ScheduleId,
    SnakeId,
    TaskId,
    ensure_not_secret,
    new_queue_entry_id,
    new_schedule_id,
    scrub_secret_text,
    validate_aware_datetime,
)
from core.contracts.run import Run
from core.contracts.task import Task, TaskPriority
from core.governance.models import ActorKind
from core.tasks.fallback import FallbackPolicy, FallbackTarget

_TZ_AREA_LOC_RE = re.compile(r"^(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+)+)$")

QuotaScopeKind = Literal[
    "CONNECTION_VERIFIED",
    "PROJECT_SHARED",
    "ACCOUNT_SHARED",
    "ORG_SHARED",
    "UNKNOWN_SHARED",
]
QueueEntryStatus = Literal[
    "QUEUED",
    "READY",
    "RESERVED",
    "RUNNING",
    "BLOCKED_BY_QUOTA",
    "BLOCKED_BY_RATE_LIMIT",
    "BLOCKED_GOVERNANCE",
    "CANCELLED",
    "COMPLETED",
    "FAILED",
]
LeaseState = Literal["RESERVED", "ACTIVE", "RELEASED", "EXPIRED", "REVOKED"]
ScheduleStatus = Literal["SCHEDULED", "TRIGGERED", "PAUSED", "COMPLETED", "CANCELLED"]
SchedulerDecisionOutcome = Literal[
    "RESERVED",
    "NO_ELIGIBLE_WORK",
    "BLOCKED_BY_CONCURRENCY",
    "BLOCKED_BY_RATE_LIMIT",
    "BLOCKED_BY_QUOTA",
    "BLOCKED_GOVERNANCE",
    "KEY_ROTATION_FORBIDDEN",
]


def _validate_timezone_name(value: str) -> str:
    """Validate IANA timezone string cleanly without requiring Windows tzdata package."""
    cleaned = ensure_not_secret(value, "timezone_name")
    if not _TZ_AREA_LOC_RE.fullmatch(cleaned):
        raise ValueError(
            f"timezone_name must be 'UTC' or valid IANA Area/Location identifier, got {value!r}."
        )
    return cleaned


class SchedulerError(RuntimeError):
    """Base fail-closed exception raised by the P11.03 Execution Scheduler."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = scrub_secret_text(message)[:500]
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class SchedulerValidationError(SchedulerError):
    """Raised when scheduler inputs, policies, or schedule definitions fail validation."""


class SchedulerReferenceError(SchedulerError):
    """Raised when referenced Task, Run, Connection Policy, Lease, or Schedule is missing."""


class SchedulerGovernanceError(SchedulerError):
    """Raised when Governance denies scheduling, queue reads, or key/connection rotation."""


class SchedulerConflictError(SchedulerError):
    """Raised on duplicate active queue entry, idempotency conflict, or optimistic lock mismatch."""


class SchedulerStateError(SchedulerError):
    """Raised when attempting an illegal queue, lease, or run-state transition."""


class SchedulerPersistenceError(SchedulerError):
    """Raised fail-closed when durable Scheduler repository is unavailable or SQLite fails."""


class KeyRotationForbiddenError(SchedulerGovernanceError):
    """Raised explicitly when trying to rotate API keys or connections to evade rate/quota limits."""


class RegisterQuotaPolicyRequest(BaseModel):
    """Input declaring concurrency, rate limit, and quota limits for a Connection scope."""

    model_config = ConfigDict(extra="forbid")

    provider_id: SnakeId
    connection_id: SnakeId
    project_ref: str | None = Field(default=None, min_length=1, max_length=128)
    account_ref: str | None = Field(default=None, min_length=1, max_length=128)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    quota_scope_kind: QuotaScopeKind = "UNKNOWN_SHARED"
    independent_quota_proof: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=256)
    rate_limit_max_requests: int | None = Field(default=None, ge=1, le=100000)
    rate_limit_window_seconds: int | None = Field(default=None, ge=1, le=86400)
    quota_limit_units: int | None = Field(default=None, ge=0)
    base_backoff_seconds: int = Field(default=5, ge=1, le=3600)
    max_backoff_seconds: int = Field(default=300, ge=1, le=86400)
    max_retries: int = Field(default=3, ge=0, le=20)
    enabled: bool = True
    timestamp: datetime

    @field_validator("project_ref", "account_ref", "model_id")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "policy_field"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")

    @model_validator(mode="after")
    def _validate_bounds(self) -> RegisterQuotaPolicyRequest:
        if (self.rate_limit_max_requests is None) != (self.rate_limit_window_seconds is None):
            raise ValueError(
                "rate_limit_max_requests and rate_limit_window_seconds must either both be set or both None."
            )
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= base_backoff_seconds.")
        if self.quota_scope_kind == "CONNECTION_VERIFIED" and not self.independent_quota_proof:
            raise ValueError(
                "quota_scope_kind='CONNECTION_VERIFIED' requires explicit independent_quota_proof=True."
            )
        return self


class QuotaBucketState(BaseModel):
    """Canonical shared execution bucket enforcing real provider/project/unproven boundaries."""

    model_config = ConfigDict(extra="forbid")

    quota_bucket_key: str
    provider_id: SnakeId
    quota_scope_kind: QuotaScopeKind
    max_concurrency: int = Field(ge=1)
    rate_limit_max_requests: int | None = None
    rate_limit_window_seconds: int | None = None
    quota_limit_units: int | None = None
    quota_used_units: int = Field(default=0, ge=0)
    base_backoff_seconds: int = Field(ge=1)
    max_backoff_seconds: int = Field(ge=1)
    max_retries: int = Field(ge=0)
    record_version: int = Field(default=1, ge=1)
    updated_at: datetime


class ConnectionQuotaPolicy(BaseModel):
    """Persisted connection policy bound deterministically to one canonical ``QuotaBucketState``."""

    model_config = ConfigDict(extra="forbid")

    policy_id: str
    provider_id: SnakeId
    connection_id: SnakeId
    project_ref: str | None = None
    account_ref: str | None = None
    model_id: str | None = None
    quota_scope_kind: QuotaScopeKind
    quota_bucket_key: str
    independent_quota_proof: bool
    max_concurrency: int = Field(ge=1)
    rate_limit_max_requests: int | None = None
    rate_limit_window_seconds: int | None = None
    quota_limit_units: int | None = None
    base_backoff_seconds: int = Field(ge=1)
    max_backoff_seconds: int = Field(ge=1)
    max_retries: int = Field(ge=0)
    enabled: bool = True
    record_version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime


class EnqueueTaskRequest(BaseModel):
    """Validated request placing an eligible Task/Run onto the persistent Scheduler queue."""

    model_config = ConfigDict(extra="forbid")

    queue_entry_id: QueueEntryId = Field(default_factory=new_queue_entry_id)
    task_id: TaskId
    run_id: RunId | None = None
    schedule_id: ScheduleId | None = None
    provider_id: SnakeId
    connection_id: SnakeId
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    priority: TaskPriority | None = None
    fairness_aging_seconds: int = Field(default=60, ge=1, le=86400)
    requested_quota_units: int = Field(default=1, ge=0, le=100000)
    assigned_worker_id: AgentId | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    timestamp: datetime

    @field_validator("model_id", "idempotency_key")
    @classmethod
    def _validate_safe(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "enqueue_field"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class QueueEntry(BaseModel):
    """Persistent Task execution queue record distinct from domain ``Task`` and ``Run``."""

    model_config = ConfigDict(extra="forbid")

    queue_entry_id: QueueEntryId
    task_id: TaskId
    run_id: RunId | None = None
    schedule_id: ScheduleId | None = None
    provider_id: SnakeId
    connection_id: SnakeId
    model_id: str | None = None
    quota_bucket_key: str
    priority: TaskPriority
    priority_rank: int = Field(ge=1)
    fairness_aging_seconds: int = Field(ge=1)
    requested_quota_units: int = Field(ge=0)
    status: QueueEntryStatus
    block_reason_code: str | None = None
    block_reason: str | None = None
    next_retry_at: datetime | None = None
    retry_count: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    request_fingerprint: str
    requested_by: str
    assigned_worker_id: AgentId | None = None
    enqueued_at: datetime
    reserved_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime
    record_version: int = Field(default=1, ge=1)


class ExecutionLease(BaseModel):
    """Atomic execution slot reservation granted to at most one Worker at a time."""

    model_config = ConfigDict(extra="forbid")

    lease_id: LeaseId
    queue_entry_id: QueueEntryId
    task_id: TaskId
    run_id: RunId | None = None
    worker_id: AgentId
    owner_token: str
    provider_id: SnakeId
    connection_id: SnakeId
    model_id: str | None = None
    quota_bucket_key: str
    state: LeaseState
    quota_units_reserved: int = Field(ge=0)
    acquired_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    released_at: datetime | None = None
    release_reason: str | None = None
    record_version: int = Field(default=1, ge=1)


class SchedulerDecision(BaseModel):
    """Auditable explanation of a reservation, block, or key-rotation denial decision."""

    model_config = ConfigDict(extra="forbid")

    outcome: SchedulerDecisionOutcome
    reason_code: str
    queue_entry_id: QueueEntryId | None = None
    task_id: TaskId | None = None
    run_id: RunId | None = None
    quota_bucket_key: str | None = None
    retry_after_seconds: int | None = None
    next_retry_at: datetime | None = None
    reason: str
    audit_event_id: EventId | None = None


class ExecutionReservationResult(BaseModel):
    """Result returned when attempting to claim the next eligible execution slot."""

    model_config = ConfigDict(extra="forbid")

    decision: SchedulerDecision
    lease: ExecutionLease | None = None
    queue_entry: QueueEntry | None = None
    blocked_entries: list[QueueEntry] = Field(default_factory=list)


class ExecutionStartConfirmRequest(BaseModel):
    """Confirms RESERVED lease actually started in Runtime with a RUNNING ``Run``."""

    model_config = ConfigDict(extra="forbid")

    lease_id: LeaseId
    owner_token: str = Field(min_length=8, max_length=128)
    run_id: RunId
    expected_record_version: int = Field(ge=1)
    timestamp: datetime

    @field_validator("owner_token")
    @classmethod
    def _validate_tok(cls, value: str) -> str:
        return ensure_not_secret(value, "owner_token")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ExecutionReleaseRequest(BaseModel):
    """Releases an active/reserved execution lease cleanly upon completion, failure, or cancel."""

    model_config = ConfigDict(extra="forbid")

    lease_id: LeaseId
    owner_token: str = Field(min_length=8, max_length=128)
    terminal_queue_status: Literal["COMPLETED", "FAILED", "CANCELLED", "READY"] = "COMPLETED"
    reason: str = Field(min_length=3, max_length=500)
    timestamp: datetime

    @field_validator("owner_token", "reason")
    @classmethod
    def _validate_safe(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "release_field"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ConnectionSwitchEvaluationRequest(BaseModel):
    """Evaluates whether a fallback/connection change is allowed without key rotation."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId
    current_target: FallbackTarget
    candidate_target: FallbackTarget
    attempt_number: int = Field(ge=1)
    operation_is_idempotent: bool
    failure_reason_code: str = Field(min_length=1, max_length=64)
    available_target_capabilities: list[str] = Field(default_factory=list, max_length=64)
    fallback_policy: FallbackPolicy
    timestamp: datetime

    @field_validator("failure_reason_code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        return ensure_not_secret(value, "failure_reason_code")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class QueueVisibilitySnapshot(BaseModel):
    """Truthful Mission Control visibility snapshot across queued, reserved, running, and blocked work."""

    model_config = ConfigDict(extra="forbid")

    total_entries: int = Field(ge=0)
    ready_entries: list[QueueEntry] = Field(default_factory=list)
    reserved_entries: list[QueueEntry] = Field(default_factory=list)
    running_entries: list[QueueEntry] = Field(default_factory=list)
    blocked_by_quota: list[QueueEntry] = Field(default_factory=list)
    blocked_by_rate_limit: list[QueueEntry] = Field(default_factory=list)
    blocked_by_governance: list[QueueEntry] = Field(default_factory=list)
    buckets: list[QuotaBucketState] = Field(default_factory=list)


class ActiveExecutionItem(BaseModel):
    """Single active/reserved execution tracked across Connection scope and Run."""

    model_config = ConfigDict(extra="forbid")

    lease_id: LeaseId
    queue_entry_id: QueueEntryId
    task_id: TaskId
    run_id: RunId | None = None
    worker_id: AgentId
    provider_id: SnakeId
    connection_id: SnakeId
    model_id: str | None = None
    quota_bucket_key: str
    lease_state: Literal["RESERVED", "ACTIVE"]
    started_at: datetime | None = None
    acquired_at: datetime
    expires_at: datetime


class ActiveExecutionSnapshot(BaseModel):
    """Reconciled view separating RESERVED leases from Runtime-confirmed RUNNING executions."""

    model_config = ConfigDict(extra="forbid")

    items: list[ActiveExecutionItem] = Field(default_factory=list)
    total_reserved: int = Field(default=0, ge=0)
    total_running: int = Field(default=0, ge=0)


class RegisterScheduledTaskRequest(BaseModel):
    """Validated metadata registering a scheduled Task for real future Calendar/Work execution."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: ScheduleId = Field(default_factory=new_schedule_id)
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=3, max_length=2000)
    project_id: ProjectId | None = None
    task_id: TaskId | None = None
    provider_id: SnakeId
    connection_id: SnakeId
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    timezone_name: str = Field(min_length=3, max_length=64)
    scheduled_at: datetime
    recurrence_rule: str | None = Field(default=None, min_length=3, max_length=128)
    recurrence_interval_seconds: int | None = Field(default=None, ge=60, le=31536000)
    requires_approval: bool = False
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    policy_note: str = Field(min_length=3, max_length=500)
    created_at: datetime

    @field_validator("title", "description", "policy_note", "model_id", "recurrence_rule", "approval_id")
    @classmethod
    def _validate_texts(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "schedule_field"))

    @field_validator("timezone_name")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        return _validate_timezone_name(value)

    @field_validator("scheduled_at", "created_at")
    @classmethod
    def _validate_ts(cls, value: datetime, info: object) -> datetime:
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))


class ScheduledTaskMetadata(BaseModel):
    """Persisted scheduled execution metadata cleanly separated from Task/Run models."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: ScheduleId
    title: str
    description: str
    project_id: ProjectId | None = None
    task_id: TaskId | None = None
    provider_id: SnakeId
    connection_id: SnakeId
    model_id: str | None = None
    owner_actor_id: str
    owner_actor_kind: ActorKind
    timezone_name: str
    scheduled_at: datetime
    recurrence_rule: str | None = None
    recurrence_interval_seconds: int | None = None
    next_execution_at: datetime | None = None
    previous_execution_at: datetime | None = None
    status: ScheduleStatus
    requires_approval: bool = False
    approval_id: str | None = None
    policy_note: str
    audit_event_id: EventId
    record_version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime


class ScheduleEvaluationOutcome(BaseModel):
    """Result from evaluating due schedules without running unauthorized/unapproved jobs."""

    model_config = ConfigDict(extra="forbid")

    schedule: ScheduledTaskMetadata
    triggered: bool
    enqueued_entry: QueueEntry | None = None
    skip_reason_code: str | None = None
    skip_reason: str | None = None


@runtime_checkable
class SchedulerTransactionContext(Protocol):
    """Atomic transactional primitives executed inside SQLite BEGIN IMMEDIATE for Scheduler."""

    def get_policy(self, provider_id: str, connection_id: str) -> ConnectionQuotaPolicy | None:
        """Load connection quota/rate/concurrency policy if registered."""

    def get_bucket(self, quota_bucket_key: str) -> QuotaBucketState | None:
        """Load shared quota bucket row under lock."""

    def upsert_bucket_and_policy(
        self,
        bucket: QuotaBucketState,
        policy: ConnectionQuotaPolicy,
    ) -> ConnectionQuotaPolicy:
        """Insert or update shared quota bucket alongside connection policy."""

    def get_task(self, task_id: str) -> Task | None:
        """Fetch current domain Task row."""

    def get_run(self, run_id: str) -> Run | None:
        """Fetch current domain Run row."""

    def has_pending_clarification_intake(self, task_id: str) -> bool:
        """Return True if Task still has unresolved clarification intake requests."""

    def is_approval_approved(self, approval_id: str) -> bool:
        """Return True ONLY if ApprovalRequest exists in status APPROVED."""

    def find_queue_by_idempotency(self, idempotency_key: str) -> QueueEntry | None:
        """Lookup existing queue entry by idempotency key."""

    def find_active_queue_for_task(self, task_id: str) -> QueueEntry | None:
        """Lookup non-terminal queue entry already bound to ``task_id``."""

    def insert_queue_entry(self, entry: QueueEntry) -> QueueEntry:
        """Persist new QueueEntry row."""

    def list_candidate_queue_entries(self) -> list[QueueEntry]:
        """List non-terminal, non-reserved/non-running queue entries ordered chronologically."""

    def count_active_leases_for_bucket(self, quota_bucket_key: str) -> int:
        """Count RESERVED + ACTIVE leases currently holding slots in ``quota_bucket_key``."""

    def count_rate_events_since(self, quota_bucket_key: str, window_start: datetime) -> int:
        """Count dispatches within sliding rate-limit window since ``window_start``."""

    def update_queue_blocked(
        self,
        *,
        queue_entry_id: str,
        expected_record_version: int,
        status: QueueEntryStatus,
        block_reason_code: str,
        block_reason: str,
        retry_count: int,
        next_retry_at: datetime | None,
        updated_at: datetime,
    ) -> QueueEntry:
        """Mark queue entry blocked by quota, rate limit, or governance under optimistic lock."""

    def cancel_or_fail_queue_entry(
        self,
        *,
        queue_entry_id: str,
        expected_record_version: int,
        status: Literal["CANCELLED", "FAILED"],
        reason_code: str,
        reason: str,
        updated_at: datetime,
    ) -> QueueEntry:
        """Mark ineligible or exhausted queue entry terminal without dispatching."""

    def reserve_slot_atomically(
        self,
        *,
        queue_entry_id: str,
        expected_queue_version: int,
        lease: ExecutionLease,
        new_bucket_used_units: int,
        rate_event_id: str,
        now: datetime,
    ) -> tuple[QueueEntry, ExecutionLease]:
        """Atomically increment bucket quota, insert rate event + lease, and reserve queue entry."""

    def get_lease_for_update(self, lease_id: str) -> ExecutionLease | None:
        """Fetch ExecutionLease row under transaction lock."""

    def confirm_lease_started(
        self,
        *,
        lease_id: str,
        expected_lease_version: int,
        run_id: str,
        started_at: datetime,
    ) -> tuple[QueueEntry, ExecutionLease]:
        """Transition RESERVED lease -> ACTIVE and queue -> RUNNING after verifying Run RUNNING."""

    def release_lease_and_queue(
        self,
        *,
        lease_id: str,
        expected_lease_version: int,
        lease_state: Literal["RELEASED", "REVOKED"],
        queue_status: Literal["COMPLETED", "FAILED", "CANCELLED", "READY"],
        reason: str,
        released_at: datetime,
    ) -> tuple[QueueEntry, ExecutionLease]:
        """Release ExecutionLease slot and synchronize QueueEntry cleanly."""

    def expire_overdue_leases(
        self,
        *,
        now: datetime,
    ) -> list[tuple[ExecutionLease, QueueEntry]]:
        """Mark overdue RESERVED/ACTIVE leases EXPIRED and requeue entries READY."""

    def insert_scheduled_task(self, record: ScheduledTaskMetadata) -> ScheduledTaskMetadata:
        """Persist new ScheduledTaskMetadata row."""

    def list_due_schedules(self, *, now: datetime) -> list[ScheduledTaskMetadata]:
        """List SCHEDULED jobs whose ``next_execution_at <= now``."""

    def update_schedule_execution(
        self,
        *,
        schedule_id: str,
        expected_record_version: int,
        status: ScheduleStatus,
        previous_execution_at: datetime,
        next_execution_at: datetime | None,
        task_id: str | None,
        updated_at: datetime,
    ) -> ScheduledTaskMetadata:
        """Advance schedule timestamps and status under optimistic lock."""

    def append_event(self, event: Event) -> Event:
        """Append audit Event inside current transaction."""


@runtime_checkable
class SchedulerRepository(Protocol):
    """Repository port decoupling ``ExecutionSchedulerService`` from SQLite internals."""

    def execute_transaction(
        self,
        processor: Callable[[SchedulerTransactionContext], Any],
    ) -> Any:
        """Run ``processor`` inside a single atomic BEGIN IMMEDIATE SQLite transaction."""

    def get_policy(self, provider_id: str, connection_id: str) -> ConnectionQuotaPolicy | None:
        """Fetch persisted ConnectionQuotaPolicy if any."""

    def list_buckets(self) -> list[QuotaBucketState]:
        """List persisted shared quota buckets."""

    def get_queue_entry(self, queue_entry_id: str) -> QueueEntry | None:
        """Fetch QueueEntry by ID."""

    def list_queue_entries(self, *, include_terminal: bool = False) -> list[QueueEntry]:
        """List persisted queue entries."""

    def get_lease(self, lease_id: str) -> ExecutionLease | None:
        """Fetch ExecutionLease by ID."""

    def list_active_leases(self) -> list[ExecutionLease]:
        """List RESERVED and ACTIVE leases across restarts."""

    def get_schedule(self, schedule_id: str) -> ScheduledTaskMetadata | None:
        """Fetch ScheduledTaskMetadata by ID."""

    def list_schedules(self, *, owner_actor_id: str | None = None) -> list[ScheduledTaskMetadata]:
        """List scheduled tasks optionally filtered by owner."""
