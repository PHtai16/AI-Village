"""Domain contracts, exceptions, and persistence port for P05.05 Worker Session Lifecycle."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import hashlib
import json
from typing import Any, Literal, Protocol, runtime_checkable
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.artifact import Artifact
from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    ChecklistItemId,
    EventId,
    RunId,
    SessionId,
    Sha256Hex,
    SnakeId,
    TaskId,
    ensure_not_secret,
    reject_secret_text,
    validate_agent_id,
    validate_aware_datetime,
    validate_safe_payload,
    validate_safe_relative_path,
)
from core.contracts.run import Run, RunStatus
from core.contracts.session import Session, SessionStatus
from core.contracts.task import Task
from core.orchestration.planning_models import PlanId, StepId
from core.orchestration.team_models import TeamProposalId
from core.tasks.transitions import ActorTrustKind
from engines.antigravity.diagnostics import redact_secrets
from engines.contract import EngineResult

ReconciliationStatus = Literal["NONE", "NEEDS_RECONCILIATION", "RECONCILED"]
WorkerExecutionOutcomeStatus = Literal[
    "CLOSED",
    "FAILED",
    "TERMINATED",
    "BLOCKED",
    "IDEMPOTENT_REPLAY",
    "NEEDS_RECONCILIATION",
]
VerificationTier = Literal["OFFLINE_INTEGRATION_VERIFIED", "LIVE_VERIFIED", "UNVERIFIED"]

VALID_SESSION_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    "INITIALIZED": frozenset({"ACTIVE", "FAILED", "TERMINATED"}),
    "ACTIVE": frozenset({"IDLE", "CLOSED", "TERMINATED", "FAILED"}),
    "IDLE": frozenset({"ACTIVE", "CLOSED", "TERMINATED", "FAILED"}),
    "CLOSED": frozenset(),
    "TERMINATED": frozenset(),
    "FAILED": frozenset(),
}

TERMINAL_SESSION_STATUSES: frozenset[SessionStatus] = frozenset({"CLOSED", "TERMINATED", "FAILED"})


class WorkerSessionError(RuntimeError):
    """Base fail-closed exception raised by Worker Session Lifecycle operations."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class WorkerPreflightBlockedError(WorkerSessionError):
    """Raised fail-closed when Worker execution preflight rejects launch."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        report: WorkerExecutionPreflightReport | None = None,
    ) -> None:
        super().__init__(error_code, message)
        self.report = report


class SessionTransitionError(WorkerSessionError):
    """Raised when an invalid or unverified Session state transition is attempted."""


class ProviderConversationCollisionError(WorkerSessionError):
    """Raised when a Provider conversation ID collides with another Session or Agent."""


class SessionIsolationViolationError(WorkerSessionError):
    """Raised when workspace, context, Task, Run, or identity isolation is violated."""


class StaleSessionVersionError(WorkerSessionError):
    """Raised on optimistic lock conflict while updating a persisted Worker Session."""


class DuplicateInvocationConflictError(WorkerSessionError):
    """Raised when an idempotency key is reused with a conflicting execution fingerprint."""


class WorkerSessionNotFoundError(WorkerSessionError):
    """Raised when querying or transitioning a nonexistent Worker Session."""


class PreflightCheckResult(BaseModel):
    """Single deterministic check evaluated during Worker Execution Preflight."""

    model_config = ConfigDict(extra="forbid")

    check_name: str = Field(min_length=1, max_length=64)
    passed: bool
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        """Reject raw secrets inside preflight check diagnostics."""
        return ensure_not_secret(value, "preflight_check.message")


class WorkerExecutionPreflightReport(BaseModel):
    """Complete fail-closed preflight report built before Engine invocation."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    task_id: TaskId
    run_id: RunId | None = None
    plan_id: PlanId
    plan_revision: int | None = Field(default=None, ge=1)
    team_proposal_id: TeamProposalId
    step_id: StepId
    agent_id: AgentId
    pinned_manifest_revision: int | None = Field(default=None, ge=1)
    pinned_manifest_sha256: Sha256Hex | None = None
    resolved_provider_id: SnakeId | None = None
    resolved_connection_id: SnakeId | None = None
    resolved_model_id: str | None = Field(default=None, min_length=1, max_length=128)
    resolved_workspace_path: str | None = Field(default=None, min_length=1, max_length=512)
    checks: list[PreflightCheckResult] = Field(default_factory=list)
    blocking_codes: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    idempotent_replay_session_id: SessionId | None = None


class WorkerSessionBinding(BaseModel):
    """Durable Worker execution metadata binding logical Session to Plan/Team/Manifest pin."""

    model_config = ConfigDict(extra="forbid")

    session_id: SessionId
    task_id: TaskId
    run_id: RunId
    agent_id: AgentId
    plan_id: PlanId
    plan_revision: int = Field(ge=1)
    team_proposal_id: TeamProposalId
    step_id: StepId
    checklist_item_id: ChecklistItemId | None = None
    pinned_manifest_revision: int = Field(ge=1)
    pinned_manifest_sha256: Sha256Hex
    provider_id: SnakeId
    connection_id: SnakeId | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    workspace_scope: str = Field(min_length=1, max_length=512)
    private_context_namespace: str = Field(min_length=1, max_length=256)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    request_fingerprint: Sha256Hex | None = None
    execution_id: str | None = Field(default=None, min_length=1, max_length=80)
    correlation_id: str = Field(min_length=1, max_length=80)
    reconciliation_status: ReconciliationStatus = "NONE"
    reconciliation_reason: str | None = Field(default=None, max_length=1000)
    record_version: int = Field(default=1, ge=1)
    last_audit_event_id: EventId | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("agent_id")
    @classmethod
    def _forbid_master_worker_session(cls, value: str) -> str:
        """Enforce separation between Master orchestrator and isolated Worker Sessions."""
        cleaned = validate_agent_id(value)
        if cleaned == "core.master":
            raise ValueError("core.master cannot hold a WorkerSessionBinding.")
        return cleaned

    @field_validator(
        "model_id",
        "workspace_scope",
        "private_context_namespace",
        "idempotency_key",
        "execution_id",
        "correlation_id",
    )
    @classmethod
    def _validate_safe_strings(cls, value: str | None, info: object) -> str | None:
        """Ensure metadata fields are non-blank and free of credential leaks."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "binding_field"))

    @field_validator("reconciliation_reason")
    @classmethod
    def _validate_reconciliation_reason(cls, value: str | None) -> str | None:
        """Reject secret patterns inside reconciliation notes."""
        if value is None:
            return None
        cleaned = value.strip()
        reject_secret_text(cleaned, "reconciliation_reason")
        return cleaned or None

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime, info: object) -> datetime:
        """Require explicit timezone-aware timestamps."""
        return validate_aware_datetime(value, getattr(info, "field_name", "binding_ts"))


class WorkerSessionTransitionRecord(BaseModel):
    """Immutable audit record capturing a verified Session state transition."""

    model_config = ConfigDict(extra="forbid")

    transition_id: str = Field(
        default_factory=lambda: f"ws_trn_{uuid.uuid4().hex[:16]}",
        min_length=6,
        max_length=64,
    )
    session_id: SessionId
    task_id: TaskId
    run_id: RunId
    from_status: SessionStatus | None = None
    to_status: SessionStatus
    actor_id: AgentId
    reason: str = Field(min_length=1, max_length=500)
    evidence_summary: str = Field(min_length=1, max_length=500)
    event_id: EventId
    timestamp: datetime

    @field_validator("reason", "evidence_summary")
    @classmethod
    def _validate_safe_text(cls, value: str, info: object) -> str:
        """Ensure transition rationale and evidence note contain no secrets."""
        return ensure_not_secret(value, getattr(info, "field_name", "transition_field"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")


class StoredWorkerSession(BaseModel):
    """Combined persisted logical Session and pinned Worker binding metadata."""

    model_config = ConfigDict(extra="forbid")

    session: Session
    binding: WorkerSessionBinding
    record_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_alignment(self) -> StoredWorkerSession:
        """Verify logical Session and WorkerSessionBinding share identical ownership anchors."""
        if self.session.session_id != self.binding.session_id:
            raise ValueError("StoredWorkerSession session_id mismatch.")
        if self.session.task_id != self.binding.task_id:
            raise ValueError("StoredWorkerSession task_id mismatch.")
        if self.session.run_id != self.binding.run_id:
            raise ValueError("StoredWorkerSession run_id mismatch.")
        if self.session.agent_id != self.binding.agent_id:
            raise ValueError("StoredWorkerSession agent_id mismatch.")
        return self


class WorkerExecutionRequest(BaseModel):
    """Explicit governed request to launch or initialize one assigned Worker step."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    plan_id: PlanId
    team_proposal_id: TeamProposalId
    step_id: StepId
    agent_id: AgentId
    actor_id: AgentId
    actor_kind: ActorTrustKind = "orchestrator"
    workspace_root: str = Field(min_length=1, max_length=512)
    workspace_scope: str = Field(min_length=1, max_length=512)
    timestamp: datetime
    run_id: RunId | None = None
    provider_id: SnakeId | None = None
    connection_id: SnakeId | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    execution_mode: Literal["text", "structured", "accept-edits", "plan"] = "accept-edits"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    handoff_ids: list[str] = Field(default_factory=list, max_length=32)
    require_acknowledged_handoffs: bool = False
    expected_artifacts: list[str] = Field(default_factory=list, max_length=32)
    additional_context: str | None = Field(default=None, max_length=8000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    verification_tier: VerificationTier = "OFFLINE_INTEGRATION_VERIFIED"

    @field_validator("agent_id")
    @classmethod
    def _forbid_master_as_worker(cls, value: str) -> str:
        """Prevent launching core.master through Worker session execution."""
        cleaned = validate_agent_id(value)
        if cleaned == "core.master":
            raise ValueError("core.master cannot be executed as a Worker session.")
        return cleaned

    @field_validator("workspace_scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        """Ensure requested workspace sub-scope is strictly workspace-relative."""
        return validate_safe_relative_path(value, "workspace_scope")

    @field_validator("expected_artifacts")
    @classmethod
    def _validate_expected_artifacts(cls, values: list[str]) -> list[str]:
        """Ensure expected artifact output paths stay relative and secret-free."""
        return [validate_safe_relative_path(item, "expected_artifacts") for item in values]

    @field_validator("model_id", "idempotency_key", "correlation_id", "approval_id", "workspace_root")
    @classmethod
    def _validate_safe_labels(cls, value: str | None, info: object) -> str | None:
        """Reject blank values or secret patterns in request strings."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "execution_request"))

    @field_validator("additional_context")
    @classmethod
    def _validate_context(cls, value: str | None) -> str | None:
        """Reject credential strings inside caller-supplied execution context."""
        if value is None:
            return None
        reject_secret_text(value, "additional_context")
        return value

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject secret keys or credential values inside request metadata."""
        validate_safe_payload(value, "worker_execution_request.metadata")
        return value

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "timestamp")

    def compute_request_fingerprint(self) -> str:
        """Compute deterministic SHA-256 fingerprint for idempotency conflict detection."""
        canonical = {
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "team_proposal_id": self.team_proposal_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "workspace_scope": self.workspace_scope,
            "provider_id": self.provider_id,
            "connection_id": self.connection_id,
            "model_id": self.model_id,
            "execution_mode": self.execution_mode,
            "handoff_ids": list(self.handoff_ids),
            "expected_artifacts": list(self.expected_artifacts),
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class WorkerExecutionOutcome(BaseModel):
    """Normalized outcome of executing or replaying one isolated Worker step."""

    model_config = ConfigDict(extra="forbid")

    status: WorkerExecutionOutcomeStatus
    verification_tier: VerificationTier
    task_id: TaskId
    run_id: RunId | None = None
    session_id: SessionId | None = None
    invocation_id: str | None = None
    provider_conversation_id: str | None = None
    session: Session | None = None
    binding: WorkerSessionBinding | None = None
    preflight: WorkerExecutionPreflightReport
    engine_result: EngineResult | None = None
    recorded_event_ids: list[EventId] = Field(default_factory=list)
    output_artifact_ids: list[ArtifactId] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    @field_validator("error_message")
    @classmethod
    def _validate_error_message(cls, value: str | None) -> str | None:
        """Redact any secret fragments inside surfaced failure details."""
        if value is None:
            return None
        return redact_secrets(value)


class WorkerRuntimeStateSnapshot(BaseModel):
    """Read-only observation separating Registry, Assignment, Session, Output, and QA truth."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    task_id: TaskId | None = None
    in_registry: bool
    registry_status: str | None = None
    is_assigned_to_task: bool
    assigned_step_ids: list[StepId] = Field(default_factory=list)
    session_id: SessionId | None = None
    session_status: SessionStatus | None = None
    session_initialized: bool = False
    session_active: bool = False
    observable_running: bool = False
    run_id: RunId | None = None
    run_status: RunStatus | None = None
    output_artifact_ids: list[ArtifactId] = Field(default_factory=list)
    qa_verified_artifact_ids: list[ArtifactId] = Field(default_factory=list)
    reconciliation_required: bool = False
    reconciliation_reason: str | None = None


class WorkerSessionRecoveryItem(BaseModel):
    """Per-session restart recovery diagnosis comparing SQLite state with live handles."""

    model_config = ConfigDict(extra="forbid")

    session_id: SessionId
    task_id: TaskId
    run_id: RunId
    agent_id: AgentId
    step_id: StepId
    last_persisted_status: SessionStatus
    observable_process_status: Literal[
        "LIVE_OBSERVABLE",
        "UNOBSERVABLE_AFTER_RESTART",
        "INITIALIZED_NOT_STARTED",
        "TERMINAL",
    ]
    reconciliation_required: bool
    manifest_drift_detected: bool
    worker_disabled: bool
    reason: str = Field(min_length=1, max_length=1000)


class WorkerSessionRecoveryReport(BaseModel):
    """Aggregated Worker Session recovery report for a Task or Run after restart."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    recovery_status: Literal["VERIFIED_CLEAN", "NEEDS_RECONCILIATION", "INVALID_HISTORY"]
    sessions: list[StoredWorkerSession] = Field(default_factory=list)
    items: list[WorkerSessionRecoveryItem] = Field(default_factory=list)
    inconsistencies: list[str] = Field(default_factory=list)
    stale_manifest_warnings: list[str] = Field(default_factory=list)


@runtime_checkable
class WorkerSessionRepository(Protocol):
    """Persistence port isolating ``WorkerSessionLifecycleService`` from SQLite internals."""

    def create_session_atomic(
        self,
        *,
        session: Session,
        binding: WorkerSessionBinding,
        transition: WorkerSessionTransitionRecord,
        events: Sequence[Event],
        new_run: Run | None = None,
        updated_run: Run | None = None,
        expected_run_version: int | None = None,
        updated_task: Task | None = None,
        expected_task_version: int | None = None,
    ) -> StoredWorkerSession:
        """Atomically persist Run link, Task link, logical Session, binding, transition, and Events."""

    def transition_session_atomic(
        self,
        *,
        session: Session,
        binding: WorkerSessionBinding,
        expected_record_version: int,
        transition: WorkerSessionTransitionRecord,
        events: Sequence[Event],
        updated_run: Run | None = None,
        expected_run_version: int | None = None,
        updated_task: Task | None = None,
        expected_task_version: int | None = None,
    ) -> StoredWorkerSession:
        """Atomically transition Session state, verify no conversation collision, and append Events."""

    def append_events_atomic(self, events: Sequence[Event]) -> list[EventId]:
        """Atomically append zero or more Events in one transaction."""

    def get_stored_session(self, session_id: str) -> StoredWorkerSession | None:
        """Fetch persisted logical Session + binding by UUID v4 identifier."""

    def list_stored_sessions(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        step_id: str | None = None,
    ) -> list[StoredWorkerSession]:
        """List persisted Worker Sessions filtered by Task, Run, Agent, or Step."""

    def find_by_idempotency(
        self,
        task_id: str,
        idempotency_key: str,
    ) -> StoredWorkerSession | None:
        """Look up existing Worker Session created under ``(task_id, idempotency_key)``."""

    def find_provider_conversation_conflict(
        self,
        provider_id: str,
        provider_conversation_id: str,
        *,
        session_id: str,
        task_id: str,
        agent_id: str,
    ) -> Session | None:
        """Return conflicting persisted Session sharing ``(provider_id, provider_conversation_id)``."""

    def list_transitions(self, session_id: str) -> list[WorkerSessionTransitionRecord]:
        """Return ordered immutable state transitions recorded for ``session_id``."""

    def record_reconciliation_atomic(
        self,
        session_id: str,
        *,
        expected_record_version: int,
        reconciliation_status: ReconciliationStatus,
        reconciliation_reason: str,
        timestamp: datetime,
        event: Event,
    ) -> StoredWorkerSession:
        """Persist reconciliation metadata and audit Event atomically."""
