"""Phase 09 + Phase 11 / P11.01 Reception Department: concurrent intake, conversation isolation, and Action Inbox."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
import uuid

from core.contracts.event import Event, EventType, sanitize_event_payload
from core.contracts.identifiers import (
    new_conversation_id,
    new_event_id,
    new_notification_id,
    new_refinement_id,
    new_task_id,
    new_turn_id,
    scrub_secret_text,
)
from core.contracts.references import NotificationRef, NotificationSeverity, NotificationType
from core.contracts.task import Task
from core.departments._common import (
    DepartmentGovernanceError,
    append_department_event,
    authorize_and_audit,
    event_actor_id,
)
from core.departments.reception_models import (
    ActionInboxItem,
    ActionInboxKind,
    ClarificationResolutionRequest,
    ConcurrentIntakeClassification,
    ConversationCreateRequest,
    ConversationIsolationError,
    ConversationLinkRole,
    ConversationRecord,
    ConversationTurnRecord,
    DedupCounterRecord,
    IncomingSignalRequest,
    IntakeClassification,
    IntakeDecision,
    IntakeIdempotencyConflictError,
    IntakeIntentMode,
    IntakeProcessingStatus,
    IntakeRecord,
    IntakeRecoverySnapshot,
    MasterHandoffStatus,
    MasterIntakeAckRequest,
    NotificationStorePort,
    ReceptionIntakeRepository,
    ReceptionPersistenceError,
    ReceptionReferenceError,
    ReceptionTransactionContext,
    ReceptionTriageOutcome,
    ReceptionValidationError,
    RecoveredIntakeEntry,
    RoutingRecommendation,
    StaleIntakeVersionError,
    TaskRefinementRecord,
    UserRequestIntake,
    UserRequestIntakeOutcome,
)
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source
from core.tasks.transitions import TERMINAL_TASK_STATUSES

_VAGUE_REQUEST_PHRASES: frozenset[str] = frozenset(
    {
        "tbd",
        "todo",
        "help",
        "fix",
        "work",
        "do it",
        "update",
        "continue",
        "more",
        "retry",
        "chua ro",
        "chưa rõ",
        "tiếp đi",
        "làm đi",
        "sửa lại",
        "cập nhật",
    }
)
_DANGLING_REFINEMENT_CUES: tuple[str, ...] = (
    "also update",
    "add to it",
    "update that",
    "same task",
    "previous task",
    "running task",
    "that task",
    "bổ sung thêm",
    "sửa lại phần đó",
    "làm thêm vào đó",
    "task vừa rồi",
    "nhiệm vụ vừa rồi",
    "cái đó",
    "phần đó",
)
_SIGNAL_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"QUEUED", "APPENDED", "ANOMALOUS", "EMERGENCY", "APPROVAL_REQUIRED"}
)


def _enforce_worker_task_ownership(auth_context: TrustedAuthContext, task_id: str | None) -> None:
    """Prevent Worker agents from reading or mutating Tasks outside assigned_task_ids."""
    if auth_context.actor_kind != "worker_agent" or task_id is None:
        return
    if task_id not in auth_context.assigned_task_ids:
        raise DepartmentGovernanceError(
            "TASK_OWNERSHIP_DENIED",
            f"Worker '{auth_context.actor_id}' is not assigned to Task '{task_id}'.",
        )


def _enforce_conversation_access(conv: ConversationRecord, auth_context: TrustedAuthContext) -> None:
    """Ensure only the owning Human Operator or Master Agent accesses/mutates a Conversation."""
    if auth_context.actor_kind == "master_agent":
        return
    if auth_context.actor_kind == "human_operator" and conv.owner_actor_id == auth_context.actor_id:
        return
    raise DepartmentGovernanceError(
        "CONVERSATION_OWNERSHIP_DENIED",
        (
            f"Actor '{auth_context.actor_id}' ({auth_context.actor_kind}) cannot access "
            f"Conversation '{conv.conversation_id}' owned by '{conv.owner_actor_id}'."
        ),
    )


def _compute_intake_fingerprint(request: UserRequestIntake) -> str:
    """Compute canonical SHA-256 over immutable request semantics for safe idempotency checks."""
    canonical = {
        "source_channel": request.source_channel,
        "conversation_id": request.conversation_id,
        "project_id": request.project_id,
        "title": request.title,
        "summary": request.summary,
        "intent_mode": request.intent_mode,
        "target_task_id": request.target_task_id,
        "target_run_id": request.target_run_id,
        "active_task_squad": list(request.active_task_squad),
        "target_department": request.target_department,
        "priority": request.priority,
        "acceptance_criteria": list(request.acceptance_criteria),
        "metadata": request.metadata,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_ambiguous_or_vague_summary(summary: str) -> bool:
    """Detect underspecified or dangling-reference text without speculating on missing intent."""
    lowered = " ".join(summary.strip().lower().split())
    if lowered in _VAGUE_REQUEST_PHRASES:
        return True
    words = [token for token in lowered.split(" ") if token]
    if len(words) < 3:
        return True
    return any(cue in lowered for cue in _DANGLING_REFINEMENT_CUES)


def _make_domain_event(
    *,
    event_type: EventType,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
    correlation_id: str,
    payload: dict[str, object],
    task_id: str | None = None,
    run_id: str | None = None,
) -> Event:
    """Construct a secret-scrubbed, attribution-anchored Event ready for atomic append."""
    return Event(
        event_id=new_event_id(),
        timestamp=timestamp,
        event_type=event_type,
        source=actor_kind_to_event_source(auth_context.actor_kind),
        task_id=task_id,
        run_id=run_id,
        actor_id=event_actor_id(auth_context),
        correlation_id=correlation_id,
        redacted_payload=sanitize_event_payload(payload),
    )


def _derive_task_title(explicit_title: str | None, summary: str) -> str:
    """Derive a secret-safe bounded Task title from explicit title or scrubbed summary."""
    raw = explicit_title if explicit_title is not None else summary.splitlines()[0][:120]
    cleaned = scrub_secret_text(raw).strip()
    return cleaned[:200] if cleaned else "Untitled user task"


class ReceptionDepartmentService:
    """Routes incoming requests/events concurrently with durable isolation and Action Inbox."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        notification_store: NotificationStorePort | None = None,
        intake_repository: ReceptionIntakeRepository | None = None,
    ) -> None:
        """Bind Reception coordinator to Governance, EventJournal, Notification, and durable Intake store."""
        self._pm = permission_manager
        self._journal = journal
        self._notifications = notification_store
        self._intake_repo = intake_repository
        self._lock = threading.RLock()
        self._dedup_state: dict[tuple[str | None, str, str, str], tuple[int, str | None]] = {}
        self._inbox: list[ActionInboxItem] = []

    def _require_intake_repo(self) -> ReceptionIntakeRepository:
        if self._intake_repo is None:
            raise ReceptionPersistenceError(
                "INTAKE_REPOSITORY_UNAVAILABLE",
                "Durable ReceptionIntakeRepository is required for P11.01 intake operations.",
            )
        return self._intake_repo

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

    def _dedup_token_for_signal(
        self,
        req: IncomingSignalRequest,
        classification: IntakeClassification,
    ) -> str:
        """Suppress alert storms without ever collapsing distinct same-text user requests."""
        if req.dedup_key is not None:
            return req.dedup_key
        if classification in {"ANOMALOUS", "EMERGENCY", "APPROVAL_REQUIRED"}:
            return f"{classification}:{req.summary[:80].lower()}"
        return f"{classification}:{req.signal_id}"

    def _build_signal_triage_tx(
        self,
        *,
        tx: ReceptionTransactionContext,
        request: IncomingSignalRequest,
        auth_context: TrustedAuthContext,
        classification: IntakeClassification,
        severity: NotificationSeverity,
        notif_type: NotificationType,
        rec_action: RoutingRecommendation,
        targets: list[str],
        inbox_kind: str,
        inbox_priority: str,
        dedup_token: str,
        scope_key: str,
    ) -> ReceptionTriageOutcome:
        if request.is_refinement_for_running_task and request.task_id is not None:
            target_task = tx.get_task(request.task_id)
            if target_task is None:
                raise ReceptionReferenceError(
                    "TARGET_TASK_NOT_FOUND",
                    f"Referenced refinement Task '{request.task_id}' does not exist.",
                )
            if target_task.status in TERMINAL_TASK_STATUSES:
                raise ReceptionValidationError(
                    "TARGET_TASK_ALREADY_TERMINAL",
                    (
                        f"Cannot append refinement signal to Task '{request.task_id}' because it is "
                        f"already in terminal status '{target_task.status}'."
                    ),
                )

        existing = tx.get_dedup_state(scope_key)
        prev_count = existing.occurrence_count if existing is not None else 0
        existing_notif_id = existing.first_notification_id if existing is not None else None
        is_duplicate = prev_count > 0
        next_count = prev_count + 1

        ev = _make_domain_event(
            event_type="notification.created",
            auth_context=auth_context,
            timestamp=request.timestamp,
            correlation_id=f"rcp-{request.signal_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            payload={
                "department": "reception",
                "signal_id": request.signal_id,
                "conversation_id": request.conversation_id,
                "classification": classification,
                "severity": severity,
                "notification_type": notif_type,
                "route_targets": targets,
                "deduplicated": is_duplicate,
                "occurrence_count": next_count,
                "broadcast_all_departments": False,
            },
        )
        tx.append_event(ev)

        notification_id = existing_notif_id
        if not is_duplicate:
            saved_notif = tx.save_notification(
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

        tx.upsert_dedup_state(
            DedupCounterRecord(
                dedup_scope_key=scope_key,
                task_id=request.task_id,
                notification_type=notif_type,
                severity=severity,
                dedup_token=dedup_token,
                occurrence_count=next_count,
                first_notification_id=notification_id,
                last_signal_id=request.signal_id,
                updated_at=request.timestamp,
            )
        )

        inbox_item = ActionInboxItem(
            inbox_item_id=f"ibx_{uuid.uuid4().hex[:14]}",
            item_kind=inbox_kind,  # type: ignore[arg-type]
            priority=inbox_priority,  # type: ignore[arg-type]
            conversation_id=request.conversation_id,
            summary=scrub_secret_text(request.summary),
            task_id=request.task_id,
            run_id=request.run_id,
            approval_id=request.approval_id,
            assigned_route=targets,
            created_at=request.timestamp,
        )
        if not is_duplicate:
            tx.insert_action_inbox_item(inbox_item, audit_event_id=ev.event_id)

        return ReceptionTriageOutcome(
            signal_id=request.signal_id,
            classification=classification,
            severity=severity,
            notification_type=notif_type,
            recommended_action=rec_action,
            route_targets=targets,
            escalated_to_master=(
                "core.master" in targets
                and classification in {"EMERGENCY", "ANOMALOUS", "APPROVAL_REQUIRED", "QUEUED"}
            ),
            deduplicated=is_duplicate,
            occurrence_count=next_count,
            notification_id=notification_id,
            inbox_item=inbox_item,
            audit_event_id=ev.event_id,
        )

    def triage_incoming_signal(
        self,
        request: IncomingSignalRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> ReceptionTriageOutcome:
        """Classify signal across 5 levels, route narrowly, deduplicate alert storms, and emit inbox item."""
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
        _enforce_worker_task_ownership(auth_context, request.task_id)

        (
            classification,
            severity,
            notif_type,
            rec_action,
            targets,
            inbox_kind,
            inbox_priority,
        ) = self._classify_signal(request)
        dedup_token = self._dedup_token_for_signal(request, classification)

        if self._intake_repo is not None:
            scope_raw = f"{request.task_id or ''}|{notif_type}|{severity}|{dedup_token}"
            scope_key = hashlib.sha256(scope_raw.encode("utf-8")).hexdigest()
            return self._intake_repo.execute_transaction(
                lambda tx: self._build_signal_triage_tx(
                    tx=tx,
                    request=request,
                    auth_context=auth_context,
                    classification=classification,
                    severity=severity,
                    notif_type=notif_type,
                    rec_action=rec_action,
                    targets=targets,
                    inbox_kind=inbox_kind,
                    inbox_priority=inbox_priority,
                    dedup_token=dedup_token,
                    scope_key=scope_key,
                )
            )

        dedup_map_key = (request.task_id, notif_type, severity, dedup_token)
        with self._lock:
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
                conversation_id=request.conversation_id,
                summary=scrub_secret_text(request.summary),
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
                escalated_to_master=(
                    "core.master" in targets
                    and classification in {"EMERGENCY", "ANOMALOUS", "APPROVAL_REQUIRED", "QUEUED"}
                ),
                deduplicated=is_duplicate,
                occurrence_count=next_count,
                notification_id=notification_id,
                inbox_item=inbox_item,
                audit_event_id=ev.event_id,
            )

    def open_conversation(
        self,
        request: ConversationCreateRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> ConversationRecord:
        """Open a persisted isolated conversation context owned by the authenticated actor."""
        repo = self._require_intake_repo()
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.create",
            resource=f"reception:conversation:{request.conversation_id}",
            timestamp=request.timestamp,
            correlation_id=f"conv-open-{request.conversation_id}",
        )
        record = ConversationRecord(
            conversation_id=request.conversation_id,
            project_id=request.project_id,
            owner_actor_id=auth_context.actor_id,
            title=request.title,
            status="ACTIVE",
            record_version=1,
            created_at=request.timestamp,
            updated_at=request.timestamp,
        )
        ev = _make_domain_event(
            event_type="reception.request_received",
            auth_context=auth_context,
            timestamp=request.timestamp,
            correlation_id=f"conv-open-{request.conversation_id}",
            payload={
                "department": "reception",
                "action": "conversation_opened",
                "conversation_id": request.conversation_id,
                "project_id": request.project_id,
            },
        )
        return repo.create_conversation(record, ev)

    def _resolve_intake_conversation(
        self,
        tx: ReceptionTransactionContext,
        request: UserRequestIntake,
        auth_context: TrustedAuthContext,
    ) -> ConversationRecord:
        if request.conversation_id is None:
            new_conv = ConversationRecord(
                conversation_id=new_conversation_id(),
                project_id=request.project_id,
                owner_actor_id=auth_context.actor_id,
                title=request.title,
                status="ACTIVE",
                record_version=1,
                created_at=request.timestamp,
                updated_at=request.timestamp,
            )
            return tx.create_conversation(new_conv)

        conv = tx.get_conversation(request.conversation_id)
        if conv is None:
            raise ReceptionReferenceError(
                "CONVERSATION_NOT_FOUND",
                f"Conversation '{request.conversation_id}' does not exist.",
            )
        _enforce_conversation_access(conv, auth_context)
        if conv.status != "ACTIVE":
            raise ReceptionValidationError(
                "CONVERSATION_NOT_ACTIVE",
                f"Conversation '{conv.conversation_id}' has status '{conv.status}' and cannot accept new requests.",
            )
        if (
            request.project_id is not None
            and conv.project_id is not None
            and request.project_id != conv.project_id
        ):
            raise ReceptionValidationError(
                "CONVERSATION_PROJECT_MISMATCH",
                f"Request project '{request.project_id}' conflicts with Conversation project '{conv.project_id}'.",
            )
        return conv

    def _validate_target_refs(
        self,
        tx: ReceptionTransactionContext,
        request: UserRequestIntake,
        conv: ConversationRecord,
        auth_context: TrustedAuthContext,
    ) -> Task | None:
        if request.target_run_id is not None and request.target_task_id is None:
            raise ReceptionValidationError(
                "TARGET_TASK_REQUIRED_FOR_RUN",
                "target_run_id requires target_task_id so Run ownership can be verified.",
            )
        if request.target_task_id is None:
            return None

        target_task = tx.get_task(request.target_task_id)
        if target_task is None:
            raise ReceptionReferenceError(
                "TARGET_TASK_NOT_FOUND",
                f"Referenced target_task_id '{request.target_task_id}' does not exist.",
            )
        bound_conv = tx.get_task_conversation_id(target_task.task_id)
        if bound_conv is not None and bound_conv != conv.conversation_id:
            raise ConversationIsolationError(
                "CROSS_CONVERSATION_TASK_CONFLICT",
                (
                    f"Task '{target_task.task_id}' belongs to Conversation '{bound_conv}', "
                    f"not '{conv.conversation_id}'."
                ),
            )
        if (
            bound_conv is None
            and auth_context.actor_kind == "human_operator"
            and conv.owner_actor_id != target_task.requested_by
        ):
            raise ConversationIsolationError(
                "CROSS_OPERATOR_TASK_CONFLICT",
                (
                    f"Operator '{auth_context.actor_id}' cannot bind or modify Task "
                    f"'{target_task.task_id}' owned by '{target_task.requested_by}'."
                ),
            )
        if request.target_run_id is not None:
            run_task_id = tx.get_run_task_id(request.target_run_id)
            if run_task_id != target_task.task_id:
                raise ReceptionReferenceError(
                    "TARGET_RUN_MISMATCH_OR_NOT_FOUND",
                    (
                        f"Referenced target_run_id '{request.target_run_id}' does not exist or does not "
                        f"belong to Task '{target_task.task_id}'."
                    ),
                )
        return target_task

    def _persist_intake_bundle(
        self,
        *,
        tx: ReceptionTransactionContext,
        request: UserRequestIntake,
        conv: ConversationRecord,
        auth_context: TrustedAuthContext,
        fingerprint: str,
        corr_id: str,
        primary_event: Event,
        classification: ConcurrentIntakeClassification,
        decision: IntakeDecision,
        processing_status: IntakeProcessingStatus,
        master_handoff_status: MasterHandoffStatus,
        severity: NotificationSeverity,
        inbox_kind: ActionInboxKind,
        inbox_priority: str,
        inbox_summary: str,
        route_targets: list[str],
        escalated_to_master: bool,
        related_task_id: str | None,
        created_task: Task | None,
        observed_running_id: str | None,
        run_id: str | None,
        link_role: ConversationLinkRole | None = None,
        refinement: TaskRefinementRecord | None = None,
        reason_code: str | None = None,
        questions: list[str] = (),
        observed_target_status: str | None = None,
    ) -> UserRequestIntakeOutcome:
        """Persist event, notification, intake record, link, refinement/turn, and inbox atomically."""
        created_task_id = created_task.task_id if created_task is not None else None
        notif_ev = _make_domain_event(
            event_type="notification.created",
            auth_context=auth_context,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            task_id=related_task_id,
            run_id=run_id,
            payload={
                "department": "reception",
                "intake_id": request.intake_id,
                "conversation_id": conv.conversation_id,
                "classification": classification,
                "notification_type": "TASK_UPDATE",
                "severity": severity,
                "route_targets": route_targets,
                **({"reason_code": reason_code} if reason_code is not None else {}),
            },
        )
        tx.append_event(notif_ev)
        saved_notif = tx.save_notification(
            NotificationRef(
                notification_id=new_notification_id(),
                task_id=related_task_id,
                run_id=run_id,
                event_id=notif_ev.event_id,
                severity=severity,
                notification_type="TASK_UPDATE",
                created_at=request.timestamp,
            )
        )
        inbox_item = ActionInboxItem(
            inbox_item_id=f"ibx_{uuid.uuid4().hex[:14]}",
            item_kind=inbox_kind,
            priority=inbox_priority,  # type: ignore[arg-type]
            status="OPEN",
            intake_id=request.intake_id,
            conversation_id=conv.conversation_id,
            summary=inbox_summary,
            task_id=related_task_id,
            run_id=run_id,
            assigned_route=route_targets,
            created_at=request.timestamp,
        )
        turn = ConversationTurnRecord(
            turn_id=new_turn_id(),
            conversation_id=conv.conversation_id,
            turn_index=1,
            intake_id=request.intake_id,
            task_id=related_task_id,
            run_id=run_id,
            sender_actor_id=auth_context.actor_id,
            sender_kind=auth_context.actor_kind,
            role="user",
            message_text=request.summary,
            audit_event_id=primary_event.event_id,
            created_at=request.timestamp,
        )
        q_list = list(questions)
        intake_record = IntakeRecord(
            intake_id=request.intake_id,
            conversation_id=conv.conversation_id,
            idempotency_scope=request.idempotency_scope,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            source_channel=request.source_channel,
            submitted_by_actor_id=auth_context.actor_id,
            submitted_by_actor_kind=auth_context.actor_kind,
            intent_mode=request.intent_mode,
            classification=classification,
            decision=decision,
            processing_status=processing_status,
            summary=request.summary,
            reason_code=reason_code,
            clarification_questions=q_list,
            related_task_id=related_task_id,
            created_task_id=created_task_id,
            observed_running_task_id=observed_running_id,
            run_id=run_id,
            route_targets=route_targets,
            escalated_to_master=escalated_to_master,
            master_handoff_status=master_handoff_status,
            correlation_id=corr_id,
            audit_event_id=primary_event.event_id,
            record_version=1,
            created_at=request.timestamp,
            updated_at=request.timestamp,
        )
        preliminary = UserRequestIntakeOutcome(
            intake_id=request.intake_id,
            conversation_id=conv.conversation_id,
            classification=classification,
            decision=decision,
            processing_status=processing_status,
            master_handoff_status=master_handoff_status,
            idempotent_replay=False,
            reason_code=reason_code,
            created_task=created_task,
            related_task_id=related_task_id,
            observed_running_task_id=observed_running_id,
            observed_target_task_status=observed_target_status,  # type: ignore[arg-type]
            task_refinement=refinement,
            conversation_turn=turn,
            clarification_questions=q_list,
            route_targets=route_targets,
            escalated_to_master=escalated_to_master,
            inbox_item=inbox_item,
            notification_id=saved_notif.notification_id,
            audit_event_id=primary_event.event_id,
            correlation_id=corr_id,
            record_version=1,
        )
        tx.insert_intake(intake_record, preliminary)
        if link_role is not None and related_task_id is not None:
            tx.link_conversation_task(
                conversation_id=conv.conversation_id,
                task_id=related_task_id,
                link_role=link_role,
                intake_id=request.intake_id,
                audit_event_id=primary_event.event_id,
                linked_at=request.timestamp,
            )
        persisted_ref = tx.append_task_refinement(refinement) if refinement is not None else None
        persisted_turn = tx.append_conversation_turn(turn)
        persisted_inbox = tx.insert_action_inbox_item(inbox_item, audit_event_id=primary_event.event_id)
        final_outcome = preliminary.model_copy(
            update={
                "task_refinement": persisted_ref,
                "conversation_turn": persisted_turn,
                "inbox_item": persisted_inbox,
            }
        )
        tx.update_intake_outcome(request.intake_id, final_outcome)
        return final_outcome

    def _commit_independent_new_task(
        self,
        *,
        tx: ReceptionTransactionContext,
        request: UserRequestIntake,
        conv: ConversationRecord,
        auth_context: TrustedAuthContext,
        fingerprint: str,
        corr_id: str,
        observed_running_id: str | None,
        active_conv_tasks: list[Task],
    ) -> UserRequestIntakeOutcome:
        if request.target_task_id is not None:
            raise ReceptionValidationError(
                "TARGET_TASK_FORBIDDEN_FOR_NEW_TASK",
                "NEW_TASK intake must remain independent and cannot target an existing task_id.",
            )

        task_b = Task(
            task_id=new_task_id(),
            project_id=request.project_id or conv.project_id,
            title=_derive_task_title(request.title, request.summary),
            description=request.summary,
            requested_by=auth_context.actor_id,
            status="QUEUED",
            priority=request.priority,
            created_at=request.timestamp,
            acceptance_criteria=list(request.acceptance_criteria),
            metadata={
                **request.metadata,
                "conversation_id": conv.conversation_id,
                "intake_id": request.intake_id,
                "source_channel": request.source_channel,
            },
        )
        task_created_ev = _make_domain_event(
            event_type="task.created",
            auth_context=auth_context,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            task_id=task_b.task_id,
            payload={
                "initial_status": "QUEUED",
                "priority": task_b.priority,
                "requested_by": task_b.requested_by,
                "conversation_id": conv.conversation_id,
                "intake_id": request.intake_id,
            },
        )
        persisted_task = tx.create_task(task_b, task_created_ev)

        targets = ["core.master"]
        if request.target_department and request.target_department not in targets:
            targets.append(request.target_department)

        intake_ev = _make_domain_event(
            event_type="reception.request_received",
            auth_context=auth_context,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            task_id=persisted_task.task_id,
            payload={
                "department": "reception",
                "intake_id": request.intake_id,
                "conversation_id": conv.conversation_id,
                "task_id": persisted_task.task_id,
                "observed_running_task_id": observed_running_id,
                "source_channel": request.source_channel,
                "classification": "QUEUED",
                "decision": "CREATED_INDEPENDENT_TASK",
                "processing_status": "QUEUED_FOR_MASTER",
                "route_targets": targets,
            },
        )
        tx.append_event(intake_ev)
        link_role: ConversationLinkRole = (
            "CONCURRENT_INDEPENDENT"
            if (observed_running_id is not None or active_conv_tasks)
            else "PRIMARY"
        )
        return self._persist_intake_bundle(
            tx=tx,
            request=request,
            conv=conv,
            auth_context=auth_context,
            fingerprint=fingerprint,
            corr_id=corr_id,
            primary_event=intake_ev,
            classification="QUEUED",
            decision="CREATED_INDEPENDENT_TASK",
            processing_status="QUEUED_FOR_MASTER",
            master_handoff_status="PENDING_MASTER_REVIEW",
            severity="INFO",
            inbox_kind="NEW_TASK_INTAKE",
            inbox_priority="HIGH" if request.priority in {"HIGH", "URGENT"} else "MEDIUM",
            inbox_summary=persisted_task.title,
            route_targets=targets,
            escalated_to_master=True,
            related_task_id=persisted_task.task_id,
            created_task=persisted_task,
            observed_running_id=observed_running_id,
            run_id=None,
            link_role=link_role,
        )

    def _commit_task_refinement(
        self,
        *,
        tx: ReceptionTransactionContext,
        request: UserRequestIntake,
        conv: ConversationRecord,
        target_task: Task,
        auth_context: TrustedAuthContext,
        fingerprint: str,
        corr_id: str,
        observed_running_id: str | None,
    ) -> UserRequestIntakeOutcome:
        effective_run_id = request.target_run_id or target_task.active_run_id
        squad = (
            list(dict.fromkeys(request.active_task_squad))
            or list(target_task.assigned_agent_ids)
            or ["core.master"]
        )
        refine_ev = _make_domain_event(
            event_type="reception.task_refined",
            auth_context=auth_context,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            task_id=target_task.task_id,
            run_id=effective_run_id,
            payload={
                "department": "reception",
                "intake_id": request.intake_id,
                "conversation_id": conv.conversation_id,
                "task_id": target_task.task_id,
                "run_id": effective_run_id,
                "source_channel": request.source_channel,
                "submitted_by": auth_context.actor_id,
                "classification": "APPENDED",
                "decision": "APPENDED_REFINEMENT",
                "processing_status": "APPLIED_TO_TASK",
                "route_targets": squad,
            },
        )
        tx.append_event(refine_ev)
        refinement = TaskRefinementRecord(
            refinement_id=new_refinement_id(),
            task_id=target_task.task_id,
            run_id=effective_run_id,
            conversation_id=conv.conversation_id,
            intake_id=request.intake_id,
            sequence_in_task=1,
            source_channel=request.source_channel,
            submitted_by=auth_context.actor_id,
            refinement_text=request.summary,
            metadata=dict(request.metadata),
            audit_event_id=refine_ev.event_id,
            created_at=request.timestamp,
        )
        return self._persist_intake_bundle(
            tx=tx,
            request=request,
            conv=conv,
            auth_context=auth_context,
            fingerprint=fingerprint,
            corr_id=corr_id,
            primary_event=refine_ev,
            classification="APPENDED",
            decision="APPENDED_REFINEMENT",
            processing_status="APPLIED_TO_TASK",
            master_handoff_status="NOT_APPLICABLE",
            severity="INFO",
            inbox_kind="TASK_REFINEMENT",
            inbox_priority="MEDIUM",
            inbox_summary=request.summary,
            route_targets=squad,
            escalated_to_master=False,
            related_task_id=target_task.task_id,
            created_task=None,
            observed_running_id=observed_running_id,
            run_id=effective_run_id,
            link_role="PRIMARY",
            refinement=refinement,
            observed_target_status=target_task.status,
        )

    def _commit_clarification_hold(
        self,
        *,
        tx: ReceptionTransactionContext,
        request: UserRequestIntake,
        conv: ConversationRecord,
        auth_context: TrustedAuthContext,
        fingerprint: str,
        corr_id: str,
        observed_running_id: str | None,
        reason_code: str,
        questions: list[str],
        observed_target_status: str | None = None,
    ) -> UserRequestIntakeOutcome:
        targets = ["core.master", "human_operator"]
        clarify_ev = _make_domain_event(
            event_type="reception.clarification_requested",
            auth_context=auth_context,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            task_id=None,
            run_id=None,
            payload={
                "department": "reception",
                "intake_id": request.intake_id,
                "conversation_id": conv.conversation_id,
                "observed_running_task_id": observed_running_id,
                "observed_target_task_status": observed_target_status,
                "reason_code": reason_code,
                "clarification_questions": questions,
                "classification": "CLARIFICATION_REQUIRED",
                "decision": "HELD_FOR_CLARIFICATION",
                "processing_status": "NEEDS_CLARIFICATION",
                "route_targets": targets,
            },
        )
        tx.append_event(clarify_ev)
        return self._persist_intake_bundle(
            tx=tx,
            request=request,
            conv=conv,
            auth_context=auth_context,
            fingerprint=fingerprint,
            corr_id=corr_id,
            primary_event=clarify_ev,
            classification="CLARIFICATION_REQUIRED",
            decision="HELD_FOR_CLARIFICATION",
            processing_status="NEEDS_CLARIFICATION",
            master_handoff_status="PENDING_MASTER_REVIEW",
            severity="WARNING",
            inbox_kind="CLARIFICATION_NEEDED",
            inbox_priority="HIGH",
            inbox_summary=request.summary,
            route_targets=targets,
            escalated_to_master=True,
            related_task_id=None,
            created_task=None,
            observed_running_id=observed_running_id,
            run_id=None,
            reason_code=reason_code,
            questions=questions,
            observed_target_status=observed_target_status,
        )

    def _process_user_intake_tx(
        self,
        *,
        tx: ReceptionTransactionContext,
        request: UserRequestIntake,
        auth_context: TrustedAuthContext,
        fingerprint: str,
        corr_id: str,
    ) -> UserRequestIntakeOutcome:
        if request.idempotency_key is not None:
            existing = tx.find_idempotent_intake(request.idempotency_scope, request.idempotency_key)
            if existing is not None:
                stored_fingerprint, cached_outcome = existing
                if stored_fingerprint != fingerprint:
                    raise IntakeIdempotencyConflictError(
                        "IDEMPOTENCY_CONFLICT",
                        (
                            f"Idempotency key '{request.idempotency_key}' in scope "
                            f"'{request.idempotency_scope}' already committed with different content."
                        ),
                    )
                return cached_outcome.model_copy(update={"idempotent_replay": True})

        conv = self._resolve_intake_conversation(tx, request, auth_context)
        target_task = self._validate_target_refs(tx, request, conv, auth_context)
        active_conv_tasks = tx.list_active_tasks_for_conversation(conv.conversation_id)
        running_tasks = tx.list_running_tasks()
        observed_running_id = running_tasks[0].task_id if running_tasks else None

        if request.intent_mode == "REFINEMENT":
            if target_task is None:
                raise ReceptionValidationError(
                    "TASK_ID_REQUIRED_FOR_REFINEMENT",
                    "REFINEMENT intake requires target_task_id so running tasks are never cross-contaminated.",
                )
            if target_task.status in TERMINAL_TASK_STATUSES:
                return self._commit_clarification_hold(
                    tx=tx,
                    request=request,
                    conv=conv,
                    auth_context=auth_context,
                    fingerprint=fingerprint,
                    corr_id=corr_id,
                    observed_running_id=observed_running_id,
                    reason_code="TARGET_TASK_ALREADY_TERMINAL",
                    observed_target_status=target_task.status,
                    questions=[
                        (
                            f"Task '{target_task.task_id}' already reached terminal status "
                            f"'{target_task.status}'. Confirm whether to start a new independent Task instead."
                        )
                    ],
                )
            return self._commit_task_refinement(
                tx=tx,
                request=request,
                conv=conv,
                target_task=target_task,
                auth_context=auth_context,
                fingerprint=fingerprint,
                corr_id=corr_id,
                observed_running_id=observed_running_id,
            )

        if request.intent_mode == "AMBIGUOUS":
            return self._commit_clarification_hold(
                tx=tx,
                request=request,
                conv=conv,
                auth_context=auth_context,
                fingerprint=fingerprint,
                corr_id=corr_id,
                observed_running_id=observed_running_id,
                reason_code="EXPLICIT_AMBIGUOUS_INTENT",
                questions=[
                    "Clarify whether this message should start an independent Task or refine an existing Task."
                ],
            )

        if request.intent_mode == "AUTO":
            if target_task is not None:
                return self._commit_clarification_hold(
                    tx=tx,
                    request=request,
                    conv=conv,
                    auth_context=auth_context,
                    fingerprint=fingerprint,
                    corr_id=corr_id,
                    observed_running_id=observed_running_id,
                    reason_code="AMBIGUOUS_TARGET_TASK_INTENT",
                    observed_target_status=target_task.status,
                    questions=[
                        (
                            f"Clarify whether to refine Task '{target_task.task_id}' or create a separate "
                            "independent Task."
                        )
                    ],
                )
            if active_conv_tasks:
                return self._commit_clarification_hold(
                    tx=tx,
                    request=request,
                    conv=conv,
                    auth_context=auth_context,
                    fingerprint=fingerprint,
                    corr_id=corr_id,
                    observed_running_id=observed_running_id,
                    reason_code="ACTIVE_TASK_IN_SAME_CONVERSATION",
                    questions=[
                        (
                            f"Conversation '{conv.conversation_id}' has active Task(s) "
                            f"({', '.join(t.task_id for t in active_conv_tasks)}). Confirm whether this is a "
                            "new independent Task or a refinement to a specific Task ID."
                        )
                    ],
                )
            if _is_ambiguous_or_vague_summary(request.summary):
                return self._commit_clarification_hold(
                    tx=tx,
                    request=request,
                    conv=conv,
                    auth_context=auth_context,
                    fingerprint=fingerprint,
                    corr_id=corr_id,
                    observed_running_id=observed_running_id,
                    reason_code="UNDERSPECIFIED_OR_DANGLING_REQUEST",
                    questions=[
                        "Provide clear standalone task details or specify the target_task_id to update."
                    ],
                )

        return self._commit_independent_new_task(
            tx=tx,
            request=request,
            conv=conv,
            auth_context=auth_context,
            fingerprint=fingerprint,
            corr_id=corr_id,
            observed_running_id=observed_running_id,
            active_conv_tasks=active_conv_tasks,
        )

    def receive_user_request(
        self,
        request: UserRequestIntake,
        *,
        auth_context: TrustedAuthContext,
    ) -> UserRequestIntakeOutcome:
        """Accept concurrent user/system requests atomically with conversation/task isolation."""
        if request.claimed_actor_id is not None and request.claimed_actor_id != auth_context.actor_id:
            raise DepartmentGovernanceError(
                "ACTOR_IDENTITY_MISMATCH",
                (
                    f"Request claimed_actor_id '{request.claimed_actor_id}' does not match "
                    f"authenticated actor '{auth_context.actor_id}'."
                ),
            )

        gov_action = "task.create"
        if request.intent_mode == "REFINEMENT":
            gov_action = (
                "task.update_plan"
                if auth_context.actor_kind == "master_agent"
                else "task.provide_input"
            )
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action=gov_action,
            resource=f"reception:intake:{request.intake_id}",
            task_id=request.target_task_id,
            run_id=request.target_run_id,
            timestamp=request.timestamp,
            correlation_id=request.correlation_id or f"rcp-intake-{request.intake_id}",
        )
        _enforce_worker_task_ownership(auth_context, request.target_task_id)
        repo = self._require_intake_repo()

        fingerprint = _compute_intake_fingerprint(request)
        corr_id = request.correlation_id or f"rcp-intake-{request.intake_id}"
        return repo.execute_transaction(
            lambda tx: self._process_user_intake_tx(
                tx=tx,
                request=request,
                auth_context=auth_context,
                fingerprint=fingerprint,
                corr_id=corr_id,
            )
        )

    def acknowledge_intake_by_master(
        self,
        request: MasterIntakeAckRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> IntakeRecord:
        """Record Master's triage acknowledgment for a queued concurrent intake."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.update_plan",
            resource=f"reception:intake:{request.intake_id}",
            timestamp=request.timestamp,
            correlation_id=f"rcp-ack-{request.intake_id}",
        )
        repo = self._require_intake_repo()

        def _tx_ack(tx: ReceptionTransactionContext) -> IntakeRecord:
            current = tx.get_intake_for_update(request.intake_id)
            if current is None:
                raise ReceptionReferenceError(
                    "INTAKE_NOT_FOUND",
                    f"Intake '{request.intake_id}' does not exist.",
                )
            if current.master_handoff_status != "PENDING_MASTER_REVIEW":
                raise ReceptionValidationError(
                    "INVALID_MASTER_ACK_STATE",
                    f"Intake '{request.intake_id}' has handoff status '{current.master_handoff_status}'.",
                )
            if current.record_version != request.expected_record_version:
                raise StaleIntakeVersionError(
                    "STALE_INTAKE_VERSION",
                    (
                        f"Stale version for intake '{request.intake_id}': expected "
                        f"{request.expected_record_version}, found {current.record_version}."
                    ),
                )
            ev = _make_domain_event(
                event_type="reception.master_decision_recorded",
                auth_context=auth_context,
                timestamp=request.timestamp,
                correlation_id=current.correlation_id,
                task_id=current.created_task_id or current.related_task_id,
                run_id=current.run_id,
                payload={
                    "department": "reception",
                    "intake_id": current.intake_id,
                    "conversation_id": current.conversation_id,
                    "decision": "ACKNOWLEDGED_BY_MASTER",
                    "decision_note": request.decision_note,
                },
            )
            return tx.update_intake_master_ack(
                intake_id=request.intake_id,
                expected_record_version=request.expected_record_version,
                decision_note=request.decision_note,
                decided_by=auth_context.actor_id,
                decided_at=request.timestamp,
                audit_event=ev,
            )

        return repo.execute_transaction(_tx_ack)

    def resolve_intake_clarification(
        self,
        request: ClarificationResolutionRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> IntakeRecord:
        """Resolve an intake held in NEEDS_CLARIFICATION into a NEW_TASK, REFINEMENT, or DISMISS."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.update_plan",
            resource=f"reception:intake:{request.intake_id}",
            task_id=request.target_task_id,
            timestamp=request.timestamp,
            correlation_id=f"rcp-resolve-{request.intake_id}",
        )
        repo = self._require_intake_repo()

        def _tx_resolve(tx: ReceptionTransactionContext) -> IntakeRecord:
            current = tx.get_intake_for_update(request.intake_id)
            if current is None:
                raise ReceptionReferenceError(
                    "INTAKE_NOT_FOUND",
                    f"Intake '{request.intake_id}' does not exist.",
                )
            if current.processing_status != "NEEDS_CLARIFICATION":
                raise ReceptionValidationError(
                    "INTAKE_NOT_NEEDS_CLARIFICATION",
                    f"Intake '{request.intake_id}' is in status '{current.processing_status}', not NEEDS_CLARIFICATION.",
                )
            if current.record_version != request.expected_record_version:
                raise StaleIntakeVersionError(
                    "STALE_INTAKE_VERSION",
                    (
                        f"Stale version for intake '{request.intake_id}': expected "
                        f"{request.expected_record_version}, found {current.record_version}."
                    ),
                )
            conv_id = current.conversation_id
            if request.resolution_mode == "NEW_TASK":
                task_b = Task(
                    task_id=new_task_id(),
                    project_id=None,
                    title=_derive_task_title(request.title, current.summary),
                    description=current.summary,
                    requested_by=current.submitted_by_actor_id,
                    status="QUEUED",
                    priority=request.priority,
                    created_at=request.timestamp,
                    acceptance_criteria=list(request.acceptance_criteria),
                    metadata={
                        "conversation_id": conv_id,
                        "intake_id": current.intake_id,
                        "resolved_by": auth_context.actor_id,
                    },
                )
                task_ev = _make_domain_event(
                    event_type="task.created",
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                    correlation_id=current.correlation_id,
                    task_id=task_b.task_id,
                    payload={
                        "initial_status": "QUEUED",
                        "priority": task_b.priority,
                        "requested_by": task_b.requested_by,
                        "intake_id": current.intake_id,
                    },
                )
                created = tx.create_task(task_b, task_ev)
                decision_ev = _make_domain_event(
                    event_type="reception.master_decision_recorded",
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                    correlation_id=current.correlation_id,
                    task_id=created.task_id,
                    payload={
                        "intake_id": current.intake_id,
                        "conversation_id": conv_id,
                        "resolution_mode": "NEW_TASK",
                        "created_task_id": created.task_id,
                        "decision_note": request.decision_note,
                    },
                )
                if conv_id is not None:
                    tx.link_conversation_task(
                        conversation_id=conv_id,
                        task_id=created.task_id,
                        link_role="CONCURRENT_INDEPENDENT",
                        intake_id=current.intake_id,
                        audit_event_id=task_ev.event_id,
                        linked_at=request.timestamp,
                    )
                return tx.update_intake_resolution(
                    intake_id=current.intake_id,
                    expected_record_version=request.expected_record_version,
                    classification="QUEUED",
                    decision="CREATED_INDEPENDENT_TASK",
                    processing_status="RESOLVED",
                    master_handoff_status="ACKNOWLEDGED_BY_MASTER",
                    related_task_id=created.task_id,
                    created_task_id=created.task_id,
                    reason_code="CLARIFIED_AS_NEW_TASK",
                    decision_note=request.decision_note,
                    decided_by=auth_context.actor_id,
                    decided_at=request.timestamp,
                    audit_event=decision_ev,
                    inbox_status="RESOLVED",
                )

            if request.resolution_mode == "REFINEMENT":
                if request.target_task_id is None:
                    raise ReceptionValidationError(
                        "TARGET_TASK_REQUIRED_FOR_REFINEMENT",
                        "Clarification resolution_mode='REFINEMENT' requires target_task_id.",
                    )
                target_task = tx.get_task(request.target_task_id)
                if target_task is None:
                    raise ReceptionReferenceError(
                        "TARGET_TASK_NOT_FOUND",
                        f"Referenced target_task_id '{request.target_task_id}' does not exist.",
                    )
                bound_conv = tx.get_task_conversation_id(target_task.task_id)
                if conv_id is not None and bound_conv is not None and bound_conv != conv_id:
                    raise ConversationIsolationError(
                        "CROSS_CONVERSATION_TASK_CONFLICT",
                        (
                            f"Task '{target_task.task_id}' belongs to Conversation '{bound_conv}', "
                            f"not '{conv_id}'."
                        ),
                    )
                if bound_conv is None and auth_context.actor_kind == "human_operator":
                    if current.submitted_by_actor_id != target_task.requested_by:
                        raise ConversationIsolationError(
                            "CROSS_OPERATOR_TASK_CONFLICT",
                            (
                                f"Task '{target_task.task_id}' belongs to '{target_task.requested_by}', "
                                f"not '{current.submitted_by_actor_id}'."
                            ),
                        )
                if target_task.status in TERMINAL_TASK_STATUSES:
                    raise ReceptionValidationError(
                        "TARGET_TASK_ALREADY_TERMINAL",
                        f"Task '{target_task.task_id}' is already terminal ('{target_task.status}').",
                    )
                refine_ev = _make_domain_event(
                    event_type="reception.task_refined",
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                    correlation_id=current.correlation_id,
                    task_id=target_task.task_id,
                    run_id=target_task.active_run_id,
                    payload={
                        "intake_id": current.intake_id,
                        "conversation_id": conv_id,
                        "task_id": target_task.task_id,
                        "resolved_by": auth_context.actor_id,
                    },
                )
                tx.append_event(refine_ev)
                tx.append_task_refinement(
                    TaskRefinementRecord(
                        refinement_id=new_refinement_id(),
                        task_id=target_task.task_id,
                        run_id=target_task.active_run_id,
                        conversation_id=conv_id,
                        intake_id=current.intake_id,
                        sequence_in_task=1,
                        source_channel=current.source_channel,
                        submitted_by=current.submitted_by_actor_id,
                        refinement_text=current.summary,
                        metadata={"resolved_by": auth_context.actor_id},
                        audit_event_id=refine_ev.event_id,
                        created_at=request.timestamp,
                    )
                )
                if conv_id is not None:
                    tx.link_conversation_task(
                        conversation_id=conv_id,
                        task_id=target_task.task_id,
                        link_role="PRIMARY",
                        intake_id=current.intake_id,
                        audit_event_id=refine_ev.event_id,
                        linked_at=request.timestamp,
                    )
                decision_ev = _make_domain_event(
                    event_type="reception.master_decision_recorded",
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                    correlation_id=current.correlation_id,
                    task_id=target_task.task_id,
                    payload={
                        "intake_id": current.intake_id,
                        "conversation_id": conv_id,
                        "resolution_mode": "REFINEMENT",
                        "target_task_id": target_task.task_id,
                        "decision_note": request.decision_note,
                    },
                )
                return tx.update_intake_resolution(
                    intake_id=current.intake_id,
                    expected_record_version=request.expected_record_version,
                    classification="APPENDED",
                    decision="APPENDED_REFINEMENT",
                    processing_status="RESOLVED",
                    master_handoff_status="ACKNOWLEDGED_BY_MASTER",
                    related_task_id=target_task.task_id,
                    created_task_id=None,
                    reason_code="CLARIFIED_AS_REFINEMENT",
                    decision_note=request.decision_note,
                    decided_by=auth_context.actor_id,
                    decided_at=request.timestamp,
                    audit_event=decision_ev,
                    inbox_status="RESOLVED",
                )

            dismiss_ev = _make_domain_event(
                event_type="reception.master_decision_recorded",
                auth_context=auth_context,
                timestamp=request.timestamp,
                correlation_id=current.correlation_id,
                task_id=None,
                payload={
                    "intake_id": current.intake_id,
                    "conversation_id": conv_id,
                    "resolution_mode": "DISMISS",
                    "decision_note": request.decision_note,
                },
            )
            return tx.update_intake_resolution(
                intake_id=current.intake_id,
                expected_record_version=request.expected_record_version,
                classification="CLARIFICATION_REQUIRED",
                decision="HELD_FOR_CLARIFICATION",
                processing_status="REJECTED",
                master_handoff_status="DISMISSED_BY_MASTER",
                related_task_id=None,
                created_task_id=None,
                reason_code="DISMISSED_AFTER_CLARIFICATION",
                decision_note=request.decision_note,
                decided_by=auth_context.actor_id,
                decided_at=request.timestamp,
                audit_event=dismiss_ev,
                inbox_status="DISMISSED",
            )

        return repo.execute_transaction(_tx_resolve)

    def get_conversation(
        self,
        conversation_id: str,
        *,
        auth_context: TrustedAuthContext | None = None,
    ) -> ConversationRecord | None:
        """Fetch conversation metadata with optional Governance and owner isolation check."""
        repo = self._require_intake_repo()
        if auth_context is not None:
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="task.read",
                resource=f"reception:conversation:{conversation_id}",
                timestamp=datetime.now(timezone.utc),
                correlation_id=f"conv-read-{conversation_id}",
            )
            if auth_context.actor_kind == "worker_agent":
                raise DepartmentGovernanceError(
                    "CONVERSATION_OWNERSHIP_DENIED",
                    f"Worker '{auth_context.actor_id}' cannot read user Conversations.",
                )
        conv = repo.get_conversation(conversation_id)
        if auth_context is not None and conv is not None:
            _enforce_conversation_access(conv, auth_context)
        return conv

    def list_conversation_turns(
        self,
        conversation_id: str,
        *,
        auth_context: TrustedAuthContext | None = None,
    ) -> list[ConversationTurnRecord]:
        """List immutable chronological turns for a single Conversation without cross-talk."""
        repo = self._require_intake_repo()
        if auth_context is not None:
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="task.read",
                resource=f"reception:conversation_turns:{conversation_id}",
                timestamp=datetime.now(timezone.utc),
                correlation_id=f"conv-turns-{conversation_id}",
            )
            if auth_context.actor_kind == "worker_agent":
                raise DepartmentGovernanceError(
                    "CONVERSATION_OWNERSHIP_DENIED",
                    f"Worker '{auth_context.actor_id}' cannot read Conversation turns.",
                )
        conv = repo.get_conversation(conversation_id)
        if auth_context is not None and conv is not None:
            _enforce_conversation_access(conv, auth_context)
        if conv is None:
            raise ReceptionReferenceError(
                "CONVERSATION_NOT_FOUND",
                f"Conversation '{conversation_id}' does not exist.",
            )
        return repo.list_conversation_turns(conversation_id)

    def get_intake_request(
        self,
        intake_id: str,
        *,
        auth_context: TrustedAuthContext | None = None,
    ) -> IntakeRecord | None:
        """Fetch a persisted IntakeRecord by ID with Governance access check."""
        repo = self._require_intake_repo()
        if auth_context is not None:
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="task.read",
                resource=f"reception:intake:{intake_id}",
                timestamp=datetime.now(timezone.utc),
                correlation_id=f"rcp-read-{intake_id}",
            )
        record = repo.get_intake_request(intake_id)
        if auth_context is not None and record is not None:
            _enforce_worker_task_ownership(auth_context, record.related_task_id)
            if record.conversation_id is not None:
                conv = repo.get_conversation(record.conversation_id)
                if conv is not None:
                    _enforce_conversation_access(conv, auth_context)
        return record

    def list_intake_requests(
        self,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
        master_handoff_status: MasterHandoffStatus | None = None,
        processing_status: IntakeProcessingStatus | None = None,
    ) -> list[IntakeRecord]:
        """List persisted IntakeRecords matching optional conversation, task, or handoff filters."""
        return self._require_intake_repo().list_intake_requests(
            conversation_id=conversation_id,
            task_id=task_id,
            master_handoff_status=master_handoff_status,
            processing_status=processing_status,
        )

    def list_master_pending_intakes(self) -> list[IntakeRecord]:
        """Return all intakes queued or held for Master triage and decision."""
        return self._require_intake_repo().list_intake_requests(
            master_handoff_status="PENDING_MASTER_REVIEW"
        )

    def list_task_refinements(
        self,
        task_id: str,
        *,
        auth_context: TrustedAuthContext | None = None,
    ) -> list[TaskRefinementRecord]:
        """List ordered immutable addendum refinements attached to ``task_id``."""
        repo = self._require_intake_repo()
        if auth_context is not None:
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="task.read",
                resource=f"reception:refinements:{task_id}",
                task_id=task_id,
                timestamp=datetime.now(timezone.utc),
                correlation_id=f"rcp-ref-{task_id}",
            )
            _enforce_worker_task_ownership(auth_context, task_id)
        return repo.list_task_refinements(task_id)

    def recover_intake_state(self) -> IntakeRecoverySnapshot:
        """Rehydrate persisted intake, conversation, and Task statuses cleanly across restart."""
        return self._require_intake_repo().recover_intake_state()

    def list_action_inbox(
        self,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
        include_closed: bool = False,
    ) -> list[ActionInboxItem]:
        """List Action Inbox entries durably from SQLite when wired, or in memory for legacy P09.06."""
        if self._intake_repo is not None:
            return self._intake_repo.list_action_inbox(
                task_id=task_id,
                conversation_id=conversation_id,
                include_closed=include_closed,
            )
        rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        with self._lock:
            items = [
                item
                for item in self._inbox
                if (task_id is None or item.task_id == task_id)
                and (conversation_id is None or item.conversation_id == conversation_id)
                and (include_closed or item.status in {"OPEN", "ACKNOWLEDGED"})
            ]
        return sorted(items, key=lambda item: (rank[item.priority], item.created_at))


__all__ = [
    "ActionInboxItem",
    "ClarificationResolutionRequest",
    "ConcurrentIntakeClassification",
    "ConversationCreateRequest",
    "ConversationIsolationError",
    "ConversationRecord",
    "ConversationTurnRecord",
    "DedupCounterRecord",
    "IncomingSignalRequest",
    "IntakeClassification",
    "IntakeDecision",
    "IntakeIdempotencyConflictError",
    "IntakeIntentMode",
    "IntakeProcessingStatus",
    "IntakeRecord",
    "IntakeRecoverySnapshot",
    "MasterHandoffStatus",
    "MasterIntakeAckRequest",
    "NotificationStorePort",
    "ReceptionDepartmentService",
    "ReceptionIntakeRepository",
    "ReceptionPersistenceError",
    "ReceptionReferenceError",
    "ReceptionTransactionContext",
    "ReceptionTriageOutcome",
    "ReceptionValidationError",
    "RecoveredIntakeEntry",
    "RoutingRecommendation",
    "StaleIntakeVersionError",
    "TaskRefinementRecord",
    "UserRequestIntake",
    "UserRequestIntakeOutcome",
]
