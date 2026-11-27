"""Phase 09 / P09.06 Reception Department: 5-level request triage, targeted routing, alerts, and Action Inbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.identifiers import (
    AgentId,
    DepartmentId,
    EventId,
    NotificationId,
    RunId,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
)
from core.contracts.references import NotificationRef, NotificationSeverity, NotificationType
from core.departments._common import (
    DepartmentBehaviorError,
    append_department_event,
    authorize_and_audit,
    event_actor_id,
)
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source

IntakeClassification = Literal[
    "QUEUED",
    "APPENDED",
    "ANOMALOUS",
    "EMERGENCY",
    "APPROVAL_REQUIRED",
]
RoutingRecommendation = Literal[
    "QUEUE_FOR_MASTER",
    "APPEND_TO_ACTIVE_SQUAD",
    "ROUTE_TO_QA_AND_RESOURCES",
    "ESCALATE_MASTER_EMERGENCY_STOP",
    "WAIT_OPERATOR_APPROVAL",
]


class ReceptionValidationError(DepartmentBehaviorError):
    """Raised when incoming signal triage or notification routing input is invalid."""


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
    source_channel: Literal["user", "runtime", "tool_runtime", "telemetry", "governance"]
    summary: str = Field(min_length=3, max_length=1000)
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
    item_kind: Literal[
        "APPROVAL_NEEDED",
        "EMERGENCY_ESCALATION",
        "ANOMALY_TRIAGE",
        "NEW_TASK_INTAKE",
        "TASK_REFINEMENT",
    ]
    priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
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


class ReceptionDepartmentService:
    """Routes incoming requests/events to exact targets with alert deduplication and Action Inbox items."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        notification_store: NotificationStorePort | None = None,
    ) -> None:
        """Bind Reception coordinator to Governance, EventJournal, and optional NotificationStorePort."""
        self._pm = permission_manager
        self._journal = journal
        self._notifications = notification_store
        self._dedup_state: dict[tuple[str | None, str, str, str], tuple[int, str | None]] = {}
        self._inbox: list[ActionInboxItem] = []

    @staticmethod
    def _classify_signal(
        req: IncomingSignalRequest,
    ) -> tuple[
        IntakeClassification,
        NotificationSeverity,
        NotificationType,
        RoutingRecommendation,
        list[str],
        str,
        str,
    ]:
        if req.is_emergency:
            return (
                "EMERGENCY",
                "CRITICAL",
                "SECURITY_ALERT" if req.source_channel in {"governance", "tool_runtime"} else "RUN_FAILED",
                "ESCALATE_MASTER_EMERGENCY_STOP",
                ["core.master"],
                "EMERGENCY_ESCALATION",
                "CRITICAL",
            )
        if req.requires_human_approval or req.approval_id is not None:
            return (
                "APPROVAL_REQUIRED",
                "WARNING",
                "APPROVAL_REQUIRED",
                "WAIT_OPERATOR_APPROVAL",
                ["human_operator", "core.master"],
                "APPROVAL_NEEDED",
                "HIGH",
            )
        if req.is_anomaly:
            targets = ["qa", "resources", "core.master"]
            if req.target_department and req.target_department not in targets:
                targets.append(req.target_department)
            return (
                "ANOMALOUS",
                "ERROR",
                "RESOURCE_WARNING" if req.source_channel == "telemetry" else "TASK_UPDATE",
                "ROUTE_TO_QA_AND_RESOURCES",
                targets,
                "ANOMALY_TRIAGE",
                "HIGH",
            )
        if req.is_refinement_for_running_task:
            if req.task_id is None:
                raise ReceptionValidationError(
                    "TASK_ID_REQUIRED_FOR_REFINEMENT",
                    "APPENDED signal requires an active task_id so running tasks are never cross-contaminated.",
                )
            squad = list(dict.fromkeys(req.active_task_squad)) or ["core.master"]
            return (
                "APPENDED",
                "INFO",
                "TASK_UPDATE",
                "APPEND_TO_ACTIVE_SQUAD",
                squad,
                "TASK_REFINEMENT",
                "MEDIUM",
            )
        route = ["core.master"]
        if req.target_department:
            route.append(req.target_department)
        return (
            "QUEUED",
            "INFO",
            "TASK_UPDATE",
            "QUEUE_FOR_MASTER",
            route,
            "NEW_TASK_INTAKE",
            "MEDIUM",
        )

    def triage_incoming_signal(
        self,
        request: IncomingSignalRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> ReceptionTriageOutcome:
        """Classify signal across 5 levels, route narrowly, deduplicate alerts, and emit inbox item."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource=f"reception:signal:{request.signal_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"rcp-{request.signal_id}",
        )

        (
            classification,
            severity,
            notif_type,
            rec_action,
            targets,
            inbox_kind,
            inbox_priority,
        ) = self._classify_signal(request)

        dedup_token = request.dedup_key or f"{classification}:{request.summary[:80].lower()}"
        dedup_map_key = (request.task_id, notif_type, severity, dedup_token)
        prev_count, existing_notif_id = self._dedup_state.get(dedup_map_key, (0, None))
        is_duplicate = prev_count > 0
        next_count = prev_count + 1

        ev = append_department_event(
            self._journal,
            event_type="notification.created",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=request.run_id,
            actor_id=event_actor_id(auth_context),
            timestamp=request.timestamp,
            correlation_id=f"rcp-{request.signal_id}",
            payload={
                "department": "reception",
                "signal_id": request.signal_id,
                "classification": classification,
                "severity": severity,
                "notification_type": notif_type,
                "route_targets": targets,
                "deduplicated": is_duplicate,
                "occurrence_count": next_count,
                "broadcast_all_departments": False,
            },
        )

        notification_id = existing_notif_id
        if not is_duplicate and self._notifications is not None:
            saved_notif = self._notifications.save_notification(
                NotificationRef(
                    task_id=request.task_id,
                    run_id=request.run_id,
                    event_id=ev.event_id,
                    severity=severity,
                    notification_type=notif_type,
                    created_at=request.timestamp,
                )
            )
            notification_id = saved_notif.notification_id
        self._dedup_state[dedup_map_key] = (next_count, notification_id)

        inbox_item = ActionInboxItem(
            inbox_item_id=f"ibx_{uuid.uuid4().hex[:14]}",
            item_kind=inbox_kind,  # type: ignore[arg-type]
            priority=inbox_priority,  # type: ignore[arg-type]
            summary=request.summary,
            task_id=request.task_id,
            run_id=request.run_id,
            approval_id=request.approval_id,
            assigned_route=targets,
            created_at=request.timestamp,
        )
        if not is_duplicate:
            self._inbox.append(inbox_item)

        return ReceptionTriageOutcome(
            signal_id=request.signal_id,
            classification=classification,
            severity=severity,
            notification_type=notif_type,
            recommended_action=rec_action,
            route_targets=targets,
            escalated_to_master=("core.master" in targets and classification in {"EMERGENCY", "ANOMALOUS", "APPROVAL_REQUIRED", "QUEUED"}),
            deduplicated=is_duplicate,
            occurrence_count=next_count,
            notification_id=notification_id,
            inbox_item=inbox_item,
            audit_event_id=ev.event_id,
        )

    def list_action_inbox(self, *, task_id: str | None = None) -> list[ActionInboxItem]:
        """List deduplicated Action Inbox contributions sorted by priority and timestamp."""
        rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        items = [i for i in self._inbox if task_id is None or i.task_id == task_id]
        return sorted(items, key=lambda i: (rank[i.priority], i.created_at))
