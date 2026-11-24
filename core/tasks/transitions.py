"""Canonical transition matrices, guard contexts, and outcome models for Task and Run."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.agent import AgentId
from core.contracts.artifact import Artifact
from core.contracts.event import Event
from core.contracts.identifiers import (
    ArtifactId,
    EvidenceId,
    RunId,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    new_evidence_id,
    reject_secret_text,
    validate_aware_datetime,
    validate_unique_id_list,
    validate_artifact_id,
)
from core.contracts.references import ChecklistItem
from core.contracts.run import Run, RunErrorRef, RunStatus, RunTelemetryRef
from core.contracts.task import Task, TaskStatus
from engines.antigravity.diagnostics import redact_secrets

VALID_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        "QUEUED",
        "ASSIGNED",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "BLOCKED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    }
)
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED"}
)

TASK_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    "QUEUED": frozenset({"ASSIGNED", "BLOCKED", "CANCELLED"}),
    "ASSIGNED": frozenset(
        {"RUNNING", "WAITING_INPUT", "WAITING_APPROVAL", "BLOCKED", "FAILED", "CANCELLED"}
    ),
    "RUNNING": frozenset(
        {"WAITING_INPUT", "WAITING_APPROVAL", "BLOCKED", "COMPLETED", "FAILED", "CANCELLED"}
    ),
    "WAITING_INPUT": frozenset({"RUNNING", "BLOCKED", "FAILED", "CANCELLED"}),
    "WAITING_APPROVAL": frozenset({"RUNNING", "BLOCKED", "FAILED", "CANCELLED"}),
    "BLOCKED": frozenset({"QUEUED", "ASSIGNED", "RUNNING", "FAILED", "CANCELLED"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}

VALID_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {
        "PENDING",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "BLOCKED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
    }
)
TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
)

RUN_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    "PENDING": frozenset({"RUNNING", "BLOCKED", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset(
        {"WAITING_INPUT", "WAITING_APPROVAL", "BLOCKED", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}
    ),
    "WAITING_INPUT": frozenset({"RUNNING", "BLOCKED", "FAILED", "CANCELLED", "TIMED_OUT"}),
    "WAITING_APPROVAL": frozenset({"RUNNING", "BLOCKED", "FAILED", "CANCELLED", "TIMED_OUT"}),
    "BLOCKED": frozenset({"RUNNING", "FAILED", "CANCELLED", "TIMED_OUT"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
    "TIMED_OUT": frozenset(),
}

ActorTrustKind = Literal[
    "core_runtime",
    "orchestrator",
    "qa_verifier",
    "operator",
    "governance",
    "worker_agent",
    "llm_self_report",
]

TransitionErrorCode = Literal[
    "INVALID_TRANSITION",
    "UNKNOWN_STATUS",
    "TERMINAL_STATE_MUTATION",
    "MISSING_RUN",
    "WRONG_TASK_RUN_RELATIONSHIP",
    "MISSING_ASSIGNMENT",
    "MISSING_QA_EVIDENCE",
    "UNVERIFIED_COMPLETION",
    "UNTRUSTED_TRANSITION_SOURCE",
    "INVALID_TIMESTAMP",
    "INVALID_TIMESTAMP_ORDER",
    "MISSING_EXECUTION_EVIDENCE",
    "CANCELLATION_NOT_CONFIRMED",
    "DUPLICATE_TRANSITION",
    "STALE_EXPECTED_STATUS",
    "UNSAFE_EVENT_PAYLOAD",
    "CONTRACT_VALIDATION_ERROR",
]


class QaVerificationEvidence(BaseModel):
    """Independent QA verification record required to complete a Task."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: EvidenceId = Field(default_factory=new_evidence_id)
    verifier_source: Literal[
        "independent_qa",
        "engine_artifact_verifier",
        "test_runner",
        "unit_test_fixture",
    ]
    verifier_actor_id: AgentId
    verification_status: Literal["VERIFIED_PASS", "VERIFIED_FAIL", "UNVERIFIED"]
    verified_at: datetime
    artifact_ids: list[ArtifactId] = Field(default_factory=list, max_length=200)
    expected_sha256: Sha256Hex | None = None
    actual_sha256: Sha256Hex | None = None
    summary: str | None = Field(default=None, max_length=1000)

    @field_validator("verified_at")
    @classmethod
    def _validate_verified_at(cls, value: datetime) -> datetime:
        """Require timezone-aware verification timestamp."""
        return validate_aware_datetime(value, "verified_at")

    @field_validator("artifact_ids")
    @classmethod
    def _validate_artifacts(cls, values: list[str]) -> list[str]:
        """Validate deduplicated canonical artifact identifiers."""
        return validate_unique_id_list(values, validate_artifact_id, "artifact_ids")

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str | None) -> str | None:
        """Reject secret patterns in optional QA summary text."""
        if value is None:
            return None
        cleaned = value.strip()
        reject_secret_text(cleaned, "summary")
        return cleaned or None

    @model_validator(mode="after")
    def _validate_sha_consistency(self) -> QaVerificationEvidence:
        """Forbid PASS status if expected and actual SHA-256 digests mismatch."""
        if (
            self.verification_status == "VERIFIED_PASS"
            and self.expected_sha256 is not None
            and self.actual_sha256 is not None
            and self.expected_sha256 != self.actual_sha256
        ):
            raise ValueError(
                "QaVerificationEvidence cannot mark VERIFIED_PASS when expected_sha256 != actual_sha256."
            )
        return self


class ExecutionEvidence(BaseModel):
    """Runtime or process-level evidence supporting execution state transitions."""

    model_config = ConfigDict(extra="forbid")

    source: Literal[
        "core_runtime",
        "engine_adapter",
        "process_manager",
        "operator",
        "unit_test_fixture",
    ]
    evidence_id: EvidenceId | None = None
    run_id: RunId | None = None
    invocation_id: str | None = Field(default=None, min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)
    error_reference: RunErrorRef | None = None
    telemetry: RunTelemetryRef | None = None
    confirmed_cancelled: bool = False
    cancellation_source: Literal[
        "process_manager",
        "core_runtime",
        "operator_confirmed_idle",
        "unit_test_fixture",
    ] | None = None

    @field_validator("reason", "invocation_id")
    @classmethod
    def _validate_safe_text(cls, value: str | None, info: object) -> str | None:
        """Ensure reason and optional invocation ID are non-blank and secret-free."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "execution_evidence"))


class TransitionContext(BaseModel):
    """Caller-supplied transition context inspected fail-closed by State Machine guards."""

    model_config = ConfigDict(extra="forbid")

    actor_id: AgentId
    actor_kind: ActorTrustKind = "core_runtime"
    timestamp: datetime
    reason: str = Field(min_length=1, max_length=1000)
    expected_current_status: str | None = Field(default=None, min_length=1, max_length=64)
    assigned_agent_ids: list[AgentId] | None = Field(default=None, max_length=50)
    run: Run | None = None
    execution_evidence: ExecutionEvidence | None = None
    qa_evidence: QaVerificationEvidence | None = None
    checklist_items: list[ChecklistItem] | None = None
    artifacts: list[Artifact] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)


class TaskTransitionRequest(BaseModel):
    """Request to evaluate and perform one guarded Task state transition."""

    model_config = ConfigDict(extra="forbid")

    task: Task
    target_status: str = Field(min_length=1, max_length=64)
    context: TransitionContext


class RunTransitionRequest(BaseModel):
    """Request to evaluate and perform one guarded Run state transition."""

    model_config = ConfigDict(extra="forbid")

    run: Run
    target_status: str = Field(min_length=1, max_length=64)
    context: TransitionContext


class TransitionError(BaseModel):
    """Structured fail-closed rejection record scrubbed of potential secret patterns."""

    model_config = ConfigDict(extra="forbid")

    error_code: TransitionErrorCode
    guard_name: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _scrub_message(cls, value: str) -> str:
        """Redact known secret formats before returning transition errors."""
        return redact_secrets(value)


class GuardDecision(BaseModel):
    """Traceable evaluation outcome of one State Machine guard."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    guard_name: str = Field(min_length=1, max_length=80)
    error: TransitionError | None = None


class TaskTransitionResult(BaseModel):
    """Immutable outcome of a Task transition attempt with optional emitted Event."""

    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["task"] = "task"
    entity_id: TaskId
    previous_status: TaskStatus
    requested_status: str
    new_status: TaskStatus
    accepted: bool
    timestamp: datetime | None = None
    guard_decisions: list[GuardDecision] = Field(default_factory=list)
    task: Task | None = None
    event: Event | None = None
    error: TransitionError | None = None


class RunTransitionResult(BaseModel):
    """Immutable outcome of a Run transition attempt with optional canonical Event."""

    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["run"] = "run"
    entity_id: RunId
    task_id: TaskId
    previous_status: RunStatus
    requested_status: str
    new_status: RunStatus
    accepted: bool
    timestamp: datetime | None = None
    guard_decisions: list[GuardDecision] = Field(default_factory=list)
    run: Run | None = None
    event: Event | None = None
    event_omitted_reason: str | None = None
    error: TransitionError | None = None


class PairedTransitionResult(BaseModel):
    """Coordinated in-memory result updating Task and Run without invalid intermediate states."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    task_result: TaskTransitionResult | None = None
    run_result: RunTransitionResult | None = None
    task: Task | None = None
    run: Run | None = None
    events: list[Event] = Field(default_factory=list)
    error: TransitionError | None = None
