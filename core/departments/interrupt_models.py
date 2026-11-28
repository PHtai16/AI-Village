"""Pure AST domain contracts, policies, and ports for P11.02 Interrupt Classification & Routing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    ConversationId,
    DepartmentId,
    EscalationId,
    EventId,
    IncidentId,
    InterruptId,
    NotificationId,
    RunId,
    TaskId,
    ensure_not_secret,
    new_interrupt_id,
    scrub_secret_text,
    validate_aware_datetime,
    validate_safe_metadata,
)
from core.contracts.references import NotificationRef, NotificationSeverity, NotificationType
from core.contracts.task import Task
from core.departments._common import DepartmentBehaviorError
from core.departments.reception_models import ActionInboxItem, ActionInboxStatus
from core.governance.models import ActorKind

InterruptClassification = Literal[
    "INFORMATIONAL",
    "ATTENTION_NEEDED",
    "ACTION_REQUIRED",
    "HUMAN_INTERVENTION_REQUIRED",
    "CRITICAL_EMERGENCY",
]
InterruptSeverity = NotificationSeverity
InterruptPriority = Literal[
    "P4_LOW",
    "P3_NORMAL",
    "P2_HIGH",
    "P1_URGENT",
    "P0_IMMEDIATE",
]
InterruptUrgency = Literal["LOW", "MODERATE", "HIGH", "IMMEDIATE"]
InterruptImpact = Literal[
    "SINGLE_STEP",
    "SINGLE_TASK",
    "MULTI_TASK",
    "WORKSPACE_WIDE",
    "SYSTEM_WIDE",
]
InterruptStatus = Literal[
    "CREATED",
    "PENDING_DELIVERY",
    "DELIVERED",
    "ACKNOWLEDGED",
    "ESCALATED",
    "RESOLVED",
    "DISMISSED",
]
InterruptSource = Literal[
    "user",
    "runtime",
    "tool_runtime",
    "telemetry",
    "governance",
    "qa_verifier",
    "scheduler",
    "resources",
    "worker_report",
]
InterruptEventCategory = Literal[
    "status_update",
    "clarification",
    "anomaly",
    "resource_warning",
    "quota_exceeded",
    "security_breach",
    "approval_required",
    "run_failure",
    "emergency_stop",
    "specialist_question",
]
RoutingTargetKind = Literal["agent", "department", "master", "human_operator"]
InterruptDedupReason = Literal[
    "NEW_INCIDENT",
    "EXACT_EVENT_RETRY",
    "ACTIVE_INCIDENT_AGGREGATED",
    "POST_RESOLUTION_RECURRENCE",
]
InterruptResolutionOutcome = Literal[
    "FIXED",
    "MITIGATED",
    "APPROVED_PROCEED",
    "DENIED_STOPPED",
    "FALSE_POSITIVE",
    "DISMISSED",
]


def _scrub_non_blank_text(value: str, field_name: str) -> str:
    """Reject blank input and redact credentials before saving interrupt text."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank.")
    scrubbed = scrub_secret_text(cleaned).strip()
    if not scrubbed:
        raise ValueError(f"{field_name} must not be blank.")
    return scrubbed


class InterruptValidationError(DepartmentBehaviorError):
    """Raised when interrupt submission, ACK, escalation, or resolution input violates domain rules."""


class InterruptReferenceError(DepartmentBehaviorError):
    """Raised when a referenced Incident, Task, Run, Conversation, or Agent does not exist."""


class InterruptStateError(DepartmentBehaviorError):
    """Raised on invalid interrupt lifecycle transitions such as resolving a closed incident twice."""


class StaleInterruptVersionError(DepartmentBehaviorError):
    """Raised on optimistic lock version mismatch during concurrent incident updates."""


class InterruptPersistenceError(DepartmentBehaviorError):
    """Raised fail-closed when durable interrupt repository is absent or SQL transaction fails."""


class AgentAvailabilitySnapshot(BaseModel):
    """Real runtime + registry observation for an Agent without assuming online availability."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    registered: bool
    registration_status: str | None = None
    department: str | None = None
    active_session_count: int = Field(default=0, ge=0)
    online_confirmed: bool = False
    assigned_to_task: bool = False


class RoutingTarget(BaseModel):
    """Auditable notification/question routing target bound to real registry & runtime checks."""

    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(min_length=2, max_length=128)
    target_kind: RoutingTargetKind
    department: DepartmentId | None = None
    delivery_status: Literal[
        "ROUTED",
        "QUEUED_FOR_ONLINE",
        "FALLBACK_DEPARTMENT",
        "ESCALATED_TO_MASTER",
    ]
    verified_online: bool | None = None
    reason: str = Field(min_length=3, max_length=300)


class InterruptClassificationDecision(BaseModel):
    """Auditable multi-dimensional classification separating severity, priority, urgency, and impact."""

    model_config = ConfigDict(extra="forbid")

    classification: InterruptClassification
    severity: InterruptSeverity
    priority: InterruptPriority
    urgency: InterruptUrgency
    impact: InterruptImpact
    notification_type: NotificationType
    severity_override_applied: bool = False
    override_reason: str | None = None
    policy_rationale: str = Field(min_length=5, max_length=1000)


class InterruptSubmitRequest(BaseModel):
    """Validated interrupt signal submitted for classification, routing, and deduplicated notification."""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: InterruptId = Field(default_factory=new_interrupt_id)
    source: InterruptSource
    event_category: InterruptEventCategory
    summary: str = Field(min_length=3, max_length=1000)
    details: str | None = Field(default=None, max_length=4000)
    task_id: TaskId | None = None
    run_id: RunId | None = None
    conversation_id: ConversationId | None = None
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)
    target_department: DepartmentId | None = None
    target_agent_id: AgentId | None = None
    claimed_actor_id: str | None = Field(default=None, min_length=2, max_length=128)
    claimed_severity: InterruptSeverity | None = None
    claimed_impact: InterruptImpact | None = None
    dedup_scope: str = Field(default="interrupt", min_length=2, max_length=64)
    event_dedup_key: str | None = Field(default=None, min_length=1, max_length=128)
    incident_key: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    timestamp: datetime

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        return _scrub_non_blank_text(value, "summary")

    @field_validator("details")
    @classmethod
    def _validate_details(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _scrub_non_blank_text(value, "details")

    @field_validator(
        "approval_id",
        "claimed_actor_id",
        "dedup_scope",
        "event_dedup_key",
        "incident_key",
    )
    @classmethod
    def _validate_safe_tokens(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "interrupt_field"))

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


class InterruptIncidentRecord(BaseModel):
    """Persisted interrupt incident with separate severity/priority/urgency/impact and full lifecycle."""

    model_config = ConfigDict(extra="forbid")

    incident_id: IncidentId
    interrupt_id: InterruptId
    dedup_scope: str
    incident_key: str | None = None
    request_fingerprint: str
    classification: InterruptClassification
    severity: InterruptSeverity
    priority: InterruptPriority
    urgency: InterruptUrgency
    impact: InterruptImpact
    status: InterruptStatus
    source: InterruptSource
    event_category: InterruptEventCategory
    summary: str
    details: str | None = None
    task_id: TaskId | None = None
    run_id: RunId | None = None
    conversation_id: ConversationId | None = None
    approval_id: str | None = None
    routing_targets: list[RoutingTarget]
    broadcast_all_departments: Literal[False] = False
    master_notified: bool = False
    action_inbox_item_id: str | None = None
    notification_id: NotificationId | None = None
    occurrence_count: int = Field(ge=1)
    policy_rationale: str
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    acknowledgement_note: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_outcome: InterruptResolutionOutcome | None = None
    resolution_note: str | None = None
    escalation_count: int = Field(default=0, ge=0)
    submitted_by_actor_id: str
    submitted_by_actor_kind: ActorKind
    audit_event_id: EventId
    record_version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime


class InterruptEventRecord(BaseModel):
    """Append-only event log entry associating raw signals or retries with one Incident."""

    model_config = ConfigDict(extra="forbid")

    interrupt_event_id: InterruptId
    incident_id: IncidentId
    dedup_scope: str
    event_dedup_key: str | None = None
    request_fingerprint: str
    source: InterruptSource
    event_category: InterruptEventCategory
    dedup_reason: InterruptDedupReason
    deduplicated: bool
    summary: str
    notification_suppressed: bool
    notification_id: NotificationId | None = None
    submitted_by_actor_id: str
    audit_event_id: EventId
    created_at: datetime


class InterruptEscalationRecord(BaseModel):
    """Immutable audit record capturing manual or policy escalation of an Incident."""

    model_config = ConfigDict(extra="forbid")

    escalation_id: EscalationId
    incident_id: IncidentId
    from_classification: InterruptClassification
    to_classification: InterruptClassification
    from_severity: InterruptSeverity
    to_severity: InterruptSeverity
    escalated_by: str
    reason: str
    added_targets: list[RoutingTarget] = Field(default_factory=list)
    notification_id: NotificationId | None = None
    audit_event_id: EventId
    created_at: datetime


class InterruptSubmitOutcome(BaseModel):
    """Canonical outcome returned after interrupt classification, routing, and persistence."""

    model_config = ConfigDict(extra="forbid")

    incident: InterruptIncidentRecord
    decision: InterruptClassificationDecision
    deduplicated: bool
    dedup_reason: InterruptDedupReason
    notification_suppressed: bool
    notification_id: NotificationId | None = None
    action_inbox_item: ActionInboxItem | None = None
    audit_event_id: EventId


class InterruptAckRequest(BaseModel):
    """Explicit acknowledgement request without implying incident resolution."""

    model_config = ConfigDict(extra="forbid")

    incident_id: IncidentId
    expected_record_version: int = Field(ge=1)
    acknowledgement_note: str = Field(min_length=3, max_length=1000)
    timestamp: datetime

    @field_validator("acknowledgement_note")
    @classmethod
    def _validate_note(cls, value: str) -> str:
        return _scrub_non_blank_text(value, "acknowledgement_note")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class InterruptEscalateRequest(BaseModel):
    """Governed escalation request raising classification/severity and alerting Master."""

    model_config = ConfigDict(extra="forbid")

    incident_id: IncidentId
    expected_record_version: int = Field(ge=1)
    target_classification: InterruptClassification | None = None
    target_severity: InterruptSeverity | None = None
    reason: str = Field(min_length=3, max_length=1000)
    additional_target_department: DepartmentId | None = None
    timestamp: datetime

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        return _scrub_non_blank_text(value, "reason")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class InterruptResolveRequest(BaseModel):
    """Governed resolution or dismissal request concluding an Interrupt Incident."""

    model_config = ConfigDict(extra="forbid")

    incident_id: IncidentId
    expected_record_version: int = Field(ge=1)
    outcome: InterruptResolutionOutcome
    resolution_note: str = Field(min_length=3, max_length=1000)
    dismiss: bool = False
    timestamp: datetime

    @field_validator("resolution_note")
    @classmethod
    def _validate_note(cls, value: str) -> str:
        return _scrub_non_blank_text(value, "resolution_note")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


@runtime_checkable
class AgentAvailabilityPort(Protocol):
    """Read-only port verifying real Agent registration, department, and live runtime session state."""

    def check_agent(self, agent_id: str, *, task_id: str | None = None) -> AgentAvailabilitySnapshot:
        """Inspect registry and runtime session state for ``agent_id`` without assuming online."""

    def list_department_agents(
        self,
        department_id: str,
        *,
        task_id: str | None = None,
    ) -> list[AgentAvailabilitySnapshot]:
        """List registered active agents belonging to ``department_id``."""


@runtime_checkable
class InterruptTransactionContext(Protocol):
    """Atomic transactional primitives executed inside a single SQLite BEGIN IMMEDIATE scope."""

    def find_event_by_dedup_key(
        self,
        dedup_scope: str,
        event_dedup_key: str,
    ) -> tuple[str, InterruptSubmitOutcome] | None:
        """Return ``(request_fingerprint, outcome)`` if exact event was already processed."""

    def find_active_incident_by_key(
        self,
        dedup_scope: str,
        incident_key: str,
    ) -> InterruptIncidentRecord | None:
        """Return currently open incident for ``(dedup_scope, incident_key)`` if any."""

    def find_latest_incident_by_key(
        self,
        dedup_scope: str,
        incident_key: str,
    ) -> InterruptIncidentRecord | None:
        """Return most recent incident for ``(dedup_scope, incident_key)`` including resolved ones."""

    def get_task(self, task_id: str) -> Task | None:
        """Fetch current Task snapshot."""

    def get_run_task_id(self, run_id: str) -> str | None:
        """Return owning task_id of ``run_id`` or None."""

    def get_conversation_owner(self, conversation_id: str) -> str | None:
        """Return owner_actor_id of ``conversation_id`` or None."""

    def check_agent_availability(
        self,
        agent_id: str,
        *,
        task_id: str | None = None,
    ) -> AgentAvailabilitySnapshot:
        """Inspect registry and live session availability for ``agent_id``."""

    def list_department_agents(
        self,
        department_id: str,
        *,
        task_id: str | None = None,
    ) -> list[AgentAvailabilitySnapshot]:
        """List active agents in ``department_id`` with live online status."""

    def append_event(self, event: Event) -> Event:
        """Append audit Event inside current transaction."""

    def save_notification(self, notification: NotificationRef) -> NotificationRef:
        """Persist NotificationRef inside current transaction."""

    def insert_action_inbox_item(
        self,
        item: ActionInboxItem,
        *,
        audit_event_id: str,
    ) -> ActionInboxItem:
        """Persist ActionInboxItem row inside current transaction."""

    def update_action_inbox_status(
        self,
        inbox_item_id: str,
        *,
        status: ActionInboxStatus,
        updated_at: datetime,
    ) -> None:
        """Sync Action Inbox status when incident is acknowledged or resolved."""

    def insert_incident(self, incident: InterruptIncidentRecord) -> InterruptIncidentRecord:
        """Persist new InterruptIncidentRecord."""

    def append_interrupt_event(
        self,
        event_record: InterruptEventRecord,
        *,
        outcome: InterruptSubmitOutcome,
    ) -> InterruptEventRecord:
        """Insert immutable InterruptEventRecord alongside replayable outcome snapshot."""

    def update_incident_aggregation(
        self,
        *,
        incident_id: str,
        expected_record_version: int,
        occurrence_count: int,
        classification: InterruptClassification,
        severity: InterruptSeverity,
        priority: InterruptPriority,
        urgency: InterruptUrgency,
        impact: InterruptImpact,
        routing_targets: list[RoutingTarget],
        master_notified: bool,
        action_inbox_item_id: str | None,
        audit_event_id: str,
        updated_at: datetime,
    ) -> InterruptIncidentRecord:
        """Increment incident occurrence count and record updated severity/targets under lock."""

    def get_incident_for_update(self, incident_id: str) -> InterruptIncidentRecord | None:
        """Read InterruptIncidentRecord by ID inside transaction."""

    def update_incident_ack(
        self,
        *,
        incident_id: str,
        expected_record_version: int,
        acknowledged_by: str,
        acknowledged_at: datetime,
        note: str,
        audit_event_id: str,
        updated_at: datetime,
    ) -> InterruptIncidentRecord:
        """Mark incident ACKNOWLEDGED under optimistic lock without marking it RESOLVED."""

    def update_incident_escalation(
        self,
        *,
        incident_id: str,
        expected_record_version: int,
        classification: InterruptClassification,
        severity: InterruptSeverity,
        priority: InterruptPriority,
        urgency: InterruptUrgency,
        routing_targets: list[RoutingTarget],
        master_notified: bool,
        notification_id: str | None,
        escalation: InterruptEscalationRecord,
        audit_event_id: str,
        updated_at: datetime,
    ) -> InterruptIncidentRecord:
        """Escalate incident status/severity atomically and record immutable escalation row."""

    def update_incident_resolution(
        self,
        *,
        incident_id: str,
        expected_record_version: int,
        status: Literal["RESOLVED", "DISMISSED"],
        resolved_by: str,
        resolved_at: datetime,
        outcome: InterruptResolutionOutcome,
        note: str,
        audit_event_id: str,
        updated_at: datetime,
    ) -> InterruptIncidentRecord:
        """Conclude incident atomically under optimistic lock."""


@runtime_checkable
class InterruptRepository(Protocol):
    """Repository port isolating ``InterruptRoutingService`` from SQLite internals."""

    def execute_transaction(
        self,
        processor: Callable[[InterruptTransactionContext], Any],
    ) -> Any:
        """Run ``processor`` inside a single atomic BEGIN IMMEDIATE SQLite transaction."""

    def get_incident(self, incident_id: str) -> InterruptIncidentRecord | None:
        """Fetch InterruptIncidentRecord by ID."""

    def list_incidents(
        self,
        *,
        task_id: str | None = None,
        status: InterruptStatus | None = None,
        severity: InterruptSeverity | None = None,
        classification: InterruptClassification | None = None,
    ) -> list[InterruptIncidentRecord]:
        """List InterruptIncidentRecords matching optional filters."""

    def list_interrupt_events(self, incident_id: str) -> list[InterruptEventRecord]:
        """List immutable InterruptEventRecords linked to ``incident_id``."""

    def list_escalations(self, incident_id: str) -> list[InterruptEscalationRecord]:
        """List immutable InterruptEscalationRecords linked to ``incident_id``."""
