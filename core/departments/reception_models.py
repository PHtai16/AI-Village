"""Domain contracts, fail-closed exceptions, and persistence ports for Reception & Concurrent Intake (P09.06 + P11.01)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    ConversationId,
    DepartmentId,
    EventId,
    IntakeId,
    NotificationId,
    ProjectId,
    RefinementId,
    RunId,
    TaskId,
    TurnId,
    ensure_not_secret,
    new_conversation_id,
    new_intake_id,
    scrub_secret_text,
    validate_aware_datetime,
    validate_safe_metadata,
)
from core.contracts.references import NotificationRef, NotificationSeverity, NotificationType
from core.contracts.task import Task, TaskPriority, TaskStatus
from core.departments._common import DepartmentBehaviorError
from core.governance.models import ActorKind

IntakeClassification = Literal[
    "QUEUED",
    "APPENDED",
    "ANOMALOUS",
    "EMERGENCY",
    "APPROVAL_REQUIRED",
]
ConcurrentIntakeClassification = Literal[
    "QUEUED",
    "APPENDED",
    "CLARIFICATION_REQUIRED",
    "ANOMALOUS",
    "EMERGENCY",
    "APPROVAL_REQUIRED",
]
RoutingRecommendation = Literal[
    "QUEUE_FOR_MASTER",
    "APPEND_TO_ACTIVE_SQUAD",
    "HOLD_FOR_CLARIFICATION",
    "ROUTE_TO_QA_AND_RESOURCES",
    "ESCALATE_MASTER_EMERGENCY_STOP",
    "WAIT_OPERATOR_APPROVAL",
]
IntakeIntentMode = Literal["AUTO", "NEW_TASK", "REFINEMENT", "AMBIGUOUS"]
IntakeDecision = Literal[
    "CREATED_INDEPENDENT_TASK",
    "QUEUED_FOR_MASTER_TRIAGE",
    "APPENDED_REFINEMENT",
    "HELD_FOR_CLARIFICATION",
    "ESCALATED_EMERGENCY",
    "ROUTED_ANOMALY",
    "AWAITING_APPROVAL",
]
IntakeProcessingStatus = Literal[
    "RECEIVED",
    "QUEUED_FOR_MASTER",
    "APPLIED_TO_TASK",
    "NEEDS_CLARIFICATION",
    "MASTER_ACKNOWLEDGED",
    "RESOLVED",
    "REJECTED",
]
MasterHandoffStatus = Literal[
    "PENDING_MASTER_REVIEW",
    "NOT_APPLICABLE",
    "ACKNOWLEDGED_BY_MASTER",
    "PLANNED_BY_MASTER",
    "DISMISSED_BY_MASTER",
]
ActionInboxKind = Literal[
    "APPROVAL_NEEDED",
    "EMERGENCY_ESCALATION",
    "ANOMALY_TRIAGE",
    "NEW_TASK_INTAKE",
    "TASK_REFINEMENT",
    "CLARIFICATION_NEEDED",
]
ActionInboxStatus = Literal["OPEN", "ACKNOWLEDGED", "RESOLVED", "DISMISSED"]
ConversationStatus = Literal["ACTIVE", "ARCHIVED", "CLOSED"]
ConversationLinkRole = Literal["PRIMARY", "CONCURRENT_INDEPENDENT", "REFINEMENT"]
ClarificationResolutionMode = Literal["NEW_TASK", "REFINEMENT", "DISMISS"]
SourceChannel = Literal["user", "runtime", "tool_runtime", "telemetry", "governance"]


def _scrub_non_blank_text(value: str, field_name: str) -> str:
    """Reject blank input and redact credentials before storing conversation/task text."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank.")
    scrubbed = scrub_secret_text(cleaned).strip()
    if not scrubbed:
        raise ValueError(f"{field_name} must not be blank.")
    return scrubbed


class ReceptionValidationError(DepartmentBehaviorError):
    """Raised when incoming signal triage or request intake input violates domain rules."""


class ReceptionReferenceError(DepartmentBehaviorError):
    """Raised when a referenced Conversation, Task, Run, or Intake record does not exist or mismatches."""


class ConversationIsolationError(DepartmentBehaviorError):
    """Raised when an operation attempts cross-conversation Task mixing or ownership escape."""


class IntakeIdempotencyConflictError(DepartmentBehaviorError):
    """Raised when an idempotency key is reused with a mismatched request fingerprint."""


class StaleIntakeVersionError(DepartmentBehaviorError):
    """Raised on optimistic lock conflict during Master acknowledgment or clarification resolution."""


class ReceptionPersistenceError(DepartmentBehaviorError):
    """Raised fail-closed when durable intake persistence is unavailable or fails."""


@runtime_checkable
class NotificationStorePort(Protocol):
    """Structural write/read port matching ``SQLiteMetadataRepository`` notification methods."""

    def save_notification(self, notification: NotificationRef) -> NotificationRef:
        """Persist a deduplicated NotificationRef anchored to a real Event."""

    def list_notifications(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[NotificationRef]:
        """List stored NotificationRefs optionally filtered by Task or Run."""


class IncomingSignalRequest(BaseModel):
    """New user input, runtime anomaly, emergency failure, or approval trigger entering Reception."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str = Field(default_factory=lambda: f"sig_{uuid.uuid4().hex[:14]}")
    source_channel: SourceChannel
    summary: str = Field(min_length=3, max_length=1000)
    conversation_id: ConversationId | None = None
    task_id: TaskId | None = None
    run_id: RunId | None = None
    active_task_squad: list[AgentId] = Field(default_factory=list, max_length=16)
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    is_refinement_for_running_task: bool = False
    is_anomaly: bool = False
    is_emergency: bool = False
    requires_human_approval: bool = False
    target_department: DepartmentId | None = None
    dedup_key: str | None = Field(default=None, min_length=1, max_length=128)
    timestamp: datetime

    @field_validator("summary", "approval_id", "dedup_key")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "signal_field"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ActionInboxItem(BaseModel):
    """Prioritized actionable entry surfaced to Mission Control Action Inbox."""

    model_config = ConfigDict(extra="forbid")

    inbox_item_id: str
    item_kind: ActionInboxKind
    priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    status: ActionInboxStatus = "OPEN"
    intake_id: IntakeId | None = None
    conversation_id: ConversationId | None = None
    summary: str
    task_id: TaskId | None = None
    run_id: RunId | None = None
    approval_id: str | None = None
    assigned_route: list[str]
    created_at: datetime


class ReceptionTriageOutcome(BaseModel):
    """Deterministic routing, deduplicated notification, and Action Inbox decision."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    classification: IntakeClassification
    severity: NotificationSeverity
    notification_type: NotificationType
    recommended_action: RoutingRecommendation
    route_targets: list[str]
    escalated_to_master: bool
    broadcast_all_departments: Literal[False] = False
    deduplicated: bool = False
    occurrence_count: int = Field(ge=1)
    notification_id: NotificationId | None = None
    inbox_item: ActionInboxItem
    audit_event_id: EventId


class ConversationCreateRequest(BaseModel):
    """Validated request to open an isolated user conversation session."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: ConversationId = Field(default_factory=new_conversation_id)
    project_id: ProjectId | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    timestamp: datetime

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _scrub_non_blank_text(value, "title")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ConversationRecord(BaseModel):
    """Persisted isolated conversation container distinct from engine provider threads."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: ConversationId
    project_id: ProjectId | None = None
    owner_actor_id: str = Field(min_length=1, max_length=128)
    title: str | None = None
    status: ConversationStatus = "ACTIVE"
    record_version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime
    linked_task_ids: list[TaskId] = Field(default_factory=list)


class ConversationTurnRecord(BaseModel):
    """Immutable append-only conversation turn bound to exactly one Conversation."""

    model_config = ConfigDict(extra="forbid")

    turn_id: TurnId
    conversation_id: ConversationId
    turn_index: int = Field(ge=1)
    intake_id: IntakeId | None = None
    task_id: TaskId | None = None
    run_id: RunId | None = None
    sender_actor_id: str = Field(min_length=1, max_length=128)
    sender_kind: ActorKind
    role: Literal["user", "reception", "master", "system"]
    message_text: str = Field(min_length=1, max_length=8000)
    audit_event_id: EventId
    created_at: datetime


class TaskRefinementRecord(BaseModel):
    """Immutable addendum appended to an active Task without overwriting prior description or history."""

    model_config = ConfigDict(extra="forbid")

    refinement_id: RefinementId
    task_id: TaskId
    run_id: RunId | None = None
    conversation_id: ConversationId | None = None
    intake_id: IntakeId
    sequence_in_task: int = Field(ge=1)
    source_channel: SourceChannel
    submitted_by: str = Field(min_length=1, max_length=128)
    refinement_text: str = Field(min_length=1, max_length=8000)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    audit_event_id: EventId
    created_at: datetime


class UserRequestIntake(BaseModel):
    """Concurrent user/system intake command processed durably without dropping or mixing contexts."""

    model_config = ConfigDict(extra="forbid")

    intake_id: IntakeId = Field(default_factory=new_intake_id)
    conversation_id: ConversationId | None = None
    project_id: ProjectId | None = None
    source_channel: SourceChannel = "user"
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=8000)
    intent_mode: IntakeIntentMode = "AUTO"
    target_task_id: TaskId | None = None
    target_run_id: RunId | None = None
    active_task_squad: list[AgentId] = Field(default_factory=list, max_length=16)
    target_department: DepartmentId | None = None
    priority: TaskPriority = "NORMAL"
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_scope: str = Field(default="user_intake", min_length=1, max_length=64)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)
    claimed_actor_id: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    timestamp: datetime

    @field_validator("idempotency_key", "idempotency_scope", "correlation_id", "claimed_actor_id")
    @classmethod
    def _validate_non_secret_ids(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "intake_field"))

    @field_validator("title")
    @classmethod
    def _scrub_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _scrub_non_blank_text(value, "title")

    @field_validator("summary")
    @classmethod
    def _scrub_summary(cls, value: str) -> str:
        return _scrub_non_blank_text(value, "summary")

    @field_validator("acceptance_criteria")
    @classmethod
    def _scrub_criteria(cls, values: list[str]) -> list[str]:
        return [_scrub_non_blank_text(item, "acceptance_criteria") for item in values]

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(
        cls,
        value: dict[str, str | int | float | bool | None],
    ) -> dict[str, str | int | float | bool | None]:
        return validate_safe_metadata(value, "metadata")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class IntakeRecord(BaseModel):
    """Persisted structured intake record surviving restarts and tracking Master handoff."""

    model_config = ConfigDict(extra="forbid")

    intake_id: IntakeId
    conversation_id: ConversationId | None = None
    idempotency_scope: str
    idempotency_key: str | None = None
    request_fingerprint: str
    source_channel: SourceChannel
    submitted_by_actor_id: str
    submitted_by_actor_kind: ActorKind
    intent_mode: Literal["AUTO", "NEW_TASK", "REFINEMENT", "AMBIGUOUS", "SIGNAL"]
    classification: ConcurrentIntakeClassification
    decision: IntakeDecision
    processing_status: IntakeProcessingStatus
    summary: str
    reason_code: str | None = None
    clarification_questions: list[str] = Field(default_factory=list)
    related_task_id: TaskId | None = None
    created_task_id: TaskId | None = None
    observed_running_task_id: TaskId | None = None
    run_id: RunId | None = None
    approval_id: str | None = None
    route_targets: list[str] = Field(default_factory=list)
    escalated_to_master: bool = False
    master_handoff_status: MasterHandoffStatus
    master_decision_note: str | None = None
    master_decided_by: str | None = None
    master_decided_at: datetime | None = None
    correlation_id: str
    audit_event_id: EventId
    record_version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime


class UserRequestIntakeOutcome(BaseModel):
    """Canonical traceable outcome returned by Reception after durable intake commit or replay."""

    model_config = ConfigDict(extra="forbid")

    intake_id: IntakeId
    conversation_id: ConversationId
    classification: ConcurrentIntakeClassification
    decision: IntakeDecision
    processing_status: IntakeProcessingStatus
    master_handoff_status: MasterHandoffStatus
    idempotent_replay: bool = False
    reason_code: str | None = None
    created_task: Task | None = None
    related_task_id: TaskId | None = None
    observed_running_task_id: TaskId | None = None
    observed_target_task_status: TaskStatus | None = None
    task_refinement: TaskRefinementRecord | None = None
    conversation_turn: ConversationTurnRecord
    clarification_questions: list[str] = Field(default_factory=list)
    route_targets: list[str] = Field(default_factory=list)
    escalated_to_master: bool = False
    inbox_item: ActionInboxItem
    notification_id: NotificationId | None = None
    audit_event_id: EventId
    correlation_id: str
    record_version: int = Field(default=1, ge=1)


class MasterIntakeAckRequest(BaseModel):
    """Master acknowledgment/triage note recorded against a queued independent intake."""

    model_config = ConfigDict(extra="forbid")

    intake_id: IntakeId
    expected_record_version: int = Field(ge=1)
    decision_note: str = Field(min_length=3, max_length=1000)
    timestamp: datetime

    @field_validator("decision_note")
    @classmethod
    def _validate_note(cls, value: str) -> str:
        return _scrub_non_blank_text(value, "decision_note")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ClarificationResolutionRequest(BaseModel):
    """Master or Human Operator decision resolving an intake held in NEEDS_CLARIFICATION."""

    model_config = ConfigDict(extra="forbid")

    intake_id: IntakeId
    expected_record_version: int = Field(ge=1)
    resolution_mode: ClarificationResolutionMode
    target_task_id: TaskId | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    decision_note: str = Field(min_length=3, max_length=1000)
    priority: TaskPriority = "NORMAL"
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    timestamp: datetime

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _scrub_non_blank_text(value, "title")

    @field_validator("decision_note")
    @classmethod
    def _validate_note(cls, value: str) -> str:
        return _scrub_non_blank_text(value, "decision_note")

    @field_validator("acceptance_criteria")
    @classmethod
    def _validate_criteria(cls, values: list[str]) -> list[str]:
        return [_scrub_non_blank_text(item, "acceptance_criteria") for item in values]

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class RecoveredIntakeEntry(BaseModel):
    """Intake recovery entry pairing intake state with live persisted Task state without auto-starting."""

    model_config = ConfigDict(extra="forbid")

    intake: IntakeRecord
    linked_task_status: TaskStatus | None = None
    active_run_id: RunId | None = None


class IntakeRecoverySnapshot(BaseModel):
    """Post-restart overview separating received, Master-pending, assigned, and running requests."""

    model_config = ConfigDict(extra="forbid")

    intakes: list[RecoveredIntakeEntry] = Field(default_factory=list)
    conversations: list[ConversationRecord] = Field(default_factory=list)
    open_inbox_items: list[ActionInboxItem] = Field(default_factory=list)
    awaiting_master_review: int = Field(default=0, ge=0)
    needs_clarification: int = Field(default=0, ge=0)
    queued_tasks: int = Field(default=0, ge=0)
    assigned_tasks: int = Field(default=0, ge=0)
    running_tasks: int = Field(default=0, ge=0)
    terminal_tasks: int = Field(default=0, ge=0)


class DedupCounterRecord(BaseModel):
    """Persisted deduplication counter row for Reception alert storm suppression."""

    model_config = ConfigDict(extra="forbid")

    dedup_scope_key: str
    task_id: TaskId | None = None
    notification_type: NotificationType
    severity: NotificationSeverity
    dedup_token: str
    occurrence_count: int = Field(ge=1)
    first_notification_id: NotificationId | None = None
    last_signal_id: str
    updated_at: datetime


@runtime_checkable
class ReceptionTransactionContext(Protocol):
    """Atomic transactional storage primitives executed inside a single SQLite BEGIN IMMEDIATE scope."""

    def find_idempotent_intake(
        self,
        scope: str,
        key: str,
    ) -> tuple[str, UserRequestIntakeOutcome] | None:
        """Return ``(request_fingerprint, outcome)`` for an existing idempotency key if present."""

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        """Fetch conversation record inside current transaction."""

    def create_conversation(self, record: ConversationRecord) -> ConversationRecord:
        """Insert a new conversation row."""

    def get_task(self, task_id: str) -> Task | None:
        """Read current Task snapshot under BEGIN IMMEDIATE serialization."""

    def get_run_task_id(self, run_id: str) -> str | None:
        """Return parent task_id of ``run_id`` or None if Run does not exist."""

    def get_task_conversation_id(self, task_id: str) -> str | None:
        """Return owning conversation_id bound to ``task_id``, or None if unbound."""

    def list_active_tasks_for_conversation(self, conversation_id: str) -> list[Task]:
        """Return non-terminal Tasks linked to ``conversation_id`` ordered chronologically."""

    def list_running_tasks(self) -> list[Task]:
        """Return all Tasks currently persisted with non-terminal RUNNING status."""

    def create_task(self, task: Task, task_created_event: Event) -> Task:
        """Insert independent QUEUED Task plus immutable baseline_json and ``task.created`` Event."""

    def append_event(self, event: Event) -> Event:
        """Append Event on active connection cleanly or raise fail-closed."""

    def insert_intake(
        self,
        record: IntakeRecord,
        outcome: UserRequestIntakeOutcome,
    ) -> IntakeRecord:
        """Persist new IntakeRecord alongside serialized canonical outcome."""

    def update_intake_outcome(
        self,
        intake_id: str,
        outcome: UserRequestIntakeOutcome,
    ) -> None:
        """Finalize serialized canonical outcome snapshot on an inserted IntakeRecord."""

    def link_conversation_task(
        self,
        *,
        conversation_id: str,
        task_id: str,
        link_role: ConversationLinkRole,
        intake_id: str | None,
        audit_event_id: str,
        linked_at: datetime,
    ) -> None:
        """Ensure Task is bound strictly to one Conversation, preventing cross-conversation leakage."""

    def append_conversation_turn(self, turn: ConversationTurnRecord) -> ConversationTurnRecord:
        """Assign monotonic turn_index and insert append-only turn row."""

    def append_task_refinement(self, refinement: TaskRefinementRecord) -> TaskRefinementRecord:
        """Assign monotonic sequence_in_task and insert append-only refinement row."""

    def insert_action_inbox_item(
        self,
        item: ActionInboxItem,
        *,
        audit_event_id: str,
    ) -> ActionInboxItem:
        """Persist ActionInboxItem row inside current transaction."""

    def save_notification(self, notification: NotificationRef) -> NotificationRef:
        """Persist NotificationRef row inside current transaction."""

    def get_intake_for_update(self, intake_id: str) -> IntakeRecord | None:
        """Load IntakeRecord by ID inside current transaction."""

    def update_intake_master_ack(
        self,
        *,
        intake_id: str,
        expected_record_version: int,
        decision_note: str,
        decided_by: str,
        decided_at: datetime,
        audit_event: Event,
    ) -> IntakeRecord:
        """Update intake Master acknowledgment atomically with optimistic lock and inbox status."""

    def update_intake_resolution(
        self,
        *,
        intake_id: str,
        expected_record_version: int,
        classification: ConcurrentIntakeClassification,
        decision: IntakeDecision,
        processing_status: IntakeProcessingStatus,
        master_handoff_status: MasterHandoffStatus,
        related_task_id: TaskId | None,
        created_task_id: TaskId | None,
        reason_code: str | None,
        decision_note: str,
        decided_by: str,
        decided_at: datetime,
        audit_event: Event,
        inbox_status: ActionInboxStatus,
    ) -> IntakeRecord:
        """Update clarified intake disposition atomically under optimistic lock."""

    def get_dedup_state(self, dedup_scope_key: str) -> DedupCounterRecord | None:
        """Fetch deduplication counter row under lock."""

    def upsert_dedup_state(self, record: DedupCounterRecord) -> DedupCounterRecord:
        """Insert or increment deduplication counter row atomically."""


@runtime_checkable
class ReceptionIntakeRepository(Protocol):
    """Repository port isolating ``ReceptionDepartmentService`` from SQLite internals."""

    def execute_transaction(
        self,
        processor: Callable[[ReceptionTransactionContext], Any],
    ) -> Any:
        """Run ``processor`` inside a single atomic BEGIN IMMEDIATE transaction."""

    def create_conversation(
        self,
        record: ConversationRecord,
        audit_event: Event,
    ) -> ConversationRecord:
        """Atomically persist Event and new ConversationRecord."""

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        """Fetch persisted ConversationRecord by ID."""

    def list_conversations(self, *, owner_actor_id: str | None = None) -> list[ConversationRecord]:
        """List persisted ConversationRecords optionally scoped to an owner actor."""

    def list_conversation_turns(self, conversation_id: str) -> list[ConversationTurnRecord]:
        """List immutable turns in ascending ``turn_index`` order for one Conversation."""

    def get_intake_request(self, intake_id: str) -> IntakeRecord | None:
        """Fetch persisted IntakeRecord by ID."""

    def list_intake_requests(
        self,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
        master_handoff_status: MasterHandoffStatus | None = None,
        processing_status: IntakeProcessingStatus | None = None,
    ) -> list[IntakeRecord]:
        """Query persisted IntakeRecords matching optional filters."""

    def list_task_refinements(self, task_id: str) -> list[TaskRefinementRecord]:
        """List immutable addendum refinements attached to ``task_id`` in sequence order."""

    def list_action_inbox(
        self,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
        include_closed: bool = False,
    ) -> list[ActionInboxItem]:
        """List persisted ActionInboxItems sorted by priority and timestamp."""

    def get_task_snapshot(self, task_id: str) -> Task | None:
        """Read current persisted Task snapshot if present."""

    def recover_intake_state(self) -> IntakeRecoverySnapshot:
        """Rehydrate persisted intake, conversation, inbox, and Task status snapshot after restart."""
