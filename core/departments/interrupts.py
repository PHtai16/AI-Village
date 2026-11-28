"""Phase 11 / P11.02 Operational Interrupt Classification, Targeted Routing, Persistent Dedup, ACK & Escalation."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import uuid

from core.contracts.event import Event, EventType, sanitize_event_payload
from core.contracts.identifiers import (
    new_escalation_id,
    new_event_id,
    new_incident_id,
    new_interrupt_id,
    scrub_secret_text,
)
from core.contracts.references import NotificationRef, NotificationSeverity, NotificationType
from core.departments._common import (
    DepartmentGovernanceError,
    authorize_and_audit,
    event_actor_id,
)
from core.departments.interrupt_models import (
    InterruptAckRequest,
    InterruptClassification,
    InterruptClassificationDecision,
    InterruptDedupReason,
    InterruptEscalateRequest,
    InterruptEscalationRecord,
    InterruptEventRecord,
    InterruptImpact,
    InterruptIncidentRecord,
    InterruptPersistenceError,
    InterruptPriority,
    InterruptReferenceError,
    InterruptRepository,
    InterruptResolveRequest,
    InterruptSeverity,
    InterruptStateError,
    InterruptStatus,
    InterruptSubmitOutcome,
    InterruptSubmitRequest,
    InterruptTransactionContext,
    InterruptUrgency,
    InterruptValidationError,
    RoutingTarget,
)
from core.departments.reception_models import ActionInboxItem, ActionInboxKind
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source

_VALID_DEPARTMENTS: frozenset[str] = frozenset(
    {"hr", "resources", "service", "marketing", "it", "reception", "process", "qa", "vault"}
)
_SEVERITY_RANK: dict[InterruptSeverity, int] = {
    "INFO": 1,
    "WARNING": 2,
    "ERROR": 3,
    "CRITICAL": 4,
}
_CLASSIFICATION_RANK: dict[InterruptClassification, int] = {
    "INFORMATIONAL": 1,
    "ATTENTION_NEEDED": 2,
    "ACTION_REQUIRED": 3,
    "HUMAN_INTERVENTION_REQUIRED": 4,
    "CRITICAL_EMERGENCY": 5,
}
_PRIORITY_RANK: dict[InterruptPriority, int] = {
    "P4_LOW": 1,
    "P3_NORMAL": 2,
    "P2_HIGH": 3,
    "P1_URGENT": 4,
    "P0_IMMEDIATE": 5,
}
_URGENCY_RANK: dict[InterruptUrgency, int] = {
    "LOW": 1,
    "MODERATE": 2,
    "HIGH": 3,
    "IMMEDIATE": 4,
}
_IMPACT_RANK: dict[InterruptImpact, int] = {
    "SINGLE_STEP": 1,
    "SINGLE_TASK": 2,
    "MULTI_TASK": 3,
    "WORKSPACE_WIDE": 4,
    "SYSTEM_WIDE": 5,
}
_PRIORITY_BY_CLASSIFICATION: dict[InterruptClassification, InterruptPriority] = {
    "INFORMATIONAL": "P4_LOW",
    "ATTENTION_NEEDED": "P3_NORMAL",
    "ACTION_REQUIRED": "P2_HIGH",
    "HUMAN_INTERVENTION_REQUIRED": "P1_URGENT",
    "CRITICAL_EMERGENCY": "P0_IMMEDIATE",
}


def _actor_label(auth_context: TrustedAuthContext) -> str:
    """Return canonical actor identity string bounded cleanly for attribution."""
    return auth_context.actor_id


def _make_interrupt_event(
    *,
    event_type: EventType,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
    correlation_id: str,
    payload: dict[str, object],
    task_id: str | None = None,
    run_id: str | None = None,
) -> Event:
    """Build a secret-scrubbed Event anchored to Task/Run/Actor/Correlation cleanly."""
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


def _compute_interrupt_fingerprint(request: InterruptSubmitRequest) -> str:
    """Compute canonical SHA-256 fingerprint over semantic interrupt payload."""
    canonical = {
        "source": request.source,
        "event_category": request.event_category,
        "summary": request.summary,
        "details": request.details,
        "task_id": request.task_id,
        "run_id": request.run_id,
        "conversation_id": request.conversation_id,
        "approval_id": request.approval_id,
        "target_department": request.target_department,
        "target_agent_id": request.target_agent_id,
        "dedup_scope": request.dedup_scope,
        "incident_key": request.incident_key,
        "metadata": request.metadata,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _map_inbox_priority(priority: InterruptPriority) -> str:
    """Map 5-tier interrupt priority cleanly onto persisted Action Inbox priority."""
    match priority:
        case "P0_IMMEDIATE":
            return "CRITICAL"
        case "P1_URGENT" | "P2_HIGH":
            return "HIGH"
        case "P3_NORMAL":
            return "MEDIUM"
        case _:
            return "LOW"


def _map_inbox_kind(
    decision: InterruptClassificationDecision,
    request: InterruptSubmitRequest,
) -> ActionInboxKind:
    """Select canonical Action Inbox item_kind matching interrupt category."""
    if decision.classification == "CRITICAL_EMERGENCY":
        return "EMERGENCY_ESCALATION"
    if request.event_category == "clarification":
        return "CLARIFICATION_NEEDED"
    if (
        decision.classification == "HUMAN_INTERVENTION_REQUIRED"
        or request.approval_id is not None
        or request.event_category == "approval_required"
    ):
        return "APPROVAL_NEEDED"
    return "ANOMALY_TRIAGE"


class InterruptRoutingService:
    """Durable policy-governed interrupt classifier, router, deduplicator, and lifecycle service."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        interrupt_repository: InterruptRepository | None = None,
    ) -> None:
        """Bind service to Governance PermissionManager, EventJournal, and durable repository."""
        self._pm = permission_manager
        self._journal = journal
        self._repo = interrupt_repository

    def _require_repo(self) -> InterruptRepository:
        if self._repo is None:
            raise InterruptPersistenceError(
                "INTERRUPT_REPOSITORY_UNAVAILABLE",
                "Durable InterruptRepository is required for P11.02 interrupt operations.",
            )
        return self._repo

    @staticmethod
    def _classify_request(
        request: InterruptSubmitRequest,
        auth_context: TrustedAuthContext,
    ) -> InterruptClassificationDecision:
        """Deterministically classify an interrupt without blindly trusting Worker severity claims."""
        is_worker = auth_context.actor_kind == "worker_agent"
        if is_worker:
            if request.claimed_severity == "CRITICAL":
                raise DepartmentGovernanceError(
                    "WORKER_CRITICAL_ESCALATION_DENIED",
                    (
                        f"Worker '{auth_context.actor_id}' cannot directly claim CRITICAL severity; "
                        "escalation to CRITICAL requires Master, Governance, Runtime, or Operator verification."
                    ),
                )
            if request.event_category in {"emergency_stop", "security_breach"}:
                raise DepartmentGovernanceError(
                    "WORKER_CRITICAL_ESCALATION_DENIED",
                    (
                        f"Worker '{auth_context.actor_id}' cannot self-trigger '{request.event_category}' "
                        "emergency interrupts without trusted verifier or orchestrator corroboration."
                    ),
                )
            if request.claimed_impact in {"WORKSPACE_WIDE", "SYSTEM_WIDE"}:
                raise DepartmentGovernanceError(
                    "WORKER_CRITICAL_ESCALATION_DENIED",
                    f"Worker '{auth_context.actor_id}' cannot self-assert '{request.claimed_impact}' blast radius.",
                )

        override_applied = False
        override_reason: str | None = None

        if request.event_category in {"emergency_stop", "security_breach"}:
            impact: InterruptImpact = request.claimed_impact or (
                "SYSTEM_WIDE" if request.event_category == "security_breach" else "WORKSPACE_WIDE"
            )
            notif_type: NotificationType = (
                "SECURITY_ALERT" if request.event_category == "security_breach" else "RUN_FAILED"
            )
            return InterruptClassificationDecision(
                classification="CRITICAL_EMERGENCY",
                severity="CRITICAL",
                priority="P0_IMMEDIATE",
                urgency="IMMEDIATE",
                impact=impact,
                notification_type=notif_type,
                policy_rationale=(
                    f"Category '{request.event_category}' from '{auth_context.actor_kind}' triggers "
                    "mandatory CRITICAL_EMERGENCY escalation to Master."
                ),
            )

        if (
            request.event_category in {"approval_required", "clarification"}
            or request.approval_id is not None
        ):
            if is_worker and request.claimed_severity in {"ERROR", "CRITICAL"}:
                override_applied = True
                override_reason = (
                    "Worker severity claim capped to WARNING pending human clarification or approval."
                )
            return InterruptClassificationDecision(
                classification="HUMAN_INTERVENTION_REQUIRED",
                severity="WARNING",
                priority="P1_URGENT",
                urgency="HIGH",
                impact=request.claimed_impact or "SINGLE_TASK",
                notification_type=(
                    "APPROVAL_REQUIRED"
                    if request.event_category == "approval_required" or request.approval_id is not None
                    else "TASK_UPDATE"
                ),
                severity_override_applied=override_applied,
                override_reason=override_reason,
                policy_rationale=(
                    f"Category '{request.event_category}' requires human decision: severity stays WARNING "
                    "while priority is P1_URGENT to surface in Action Inbox."
                ),
            )

        if request.event_category == "specialist_question":
            if is_worker and request.claimed_severity in {"WARNING", "ERROR", "CRITICAL"}:
                override_applied = True
                override_reason = (
                    "Worker severity claim overridden to INFO for cross-department specialist question."
                )
            return InterruptClassificationDecision(
                classification="ACTION_REQUIRED",
                severity="INFO",
                priority="P2_HIGH",
                urgency="HIGH",
                impact=request.claimed_impact or "SINGLE_STEP",
                notification_type="TASK_UPDATE",
                severity_override_applied=override_applied,
                override_reason=override_reason,
                policy_rationale=(
                    "Specialist question carries INFO error severity with P2_HIGH operational priority "
                    "to unblock execution quickly."
                ),
            )

        if request.event_category in {"quota_exceeded", "run_failure"}:
            impact = request.claimed_impact or "SINGLE_TASK"
            if is_worker and request.claimed_severity == "ERROR" and request.event_category == "quota_exceeded":
                override_applied = True
                override_reason = (
                    "Worker-reported quota issue mapped by policy awaiting verified Resources telemetry."
                )
            severity: InterruptSeverity = (
                "WARNING" if is_worker and request.event_category == "quota_exceeded" else "ERROR"
            )
            priority: InterruptPriority = (
                "P1_URGENT" if impact in {"MULTI_TASK", "WORKSPACE_WIDE", "SYSTEM_WIDE"} else "P2_HIGH"
            )
            return InterruptClassificationDecision(
                classification="ACTION_REQUIRED",
                severity=severity,
                priority=priority,
                urgency="HIGH",
                impact=impact,
                notification_type=(
                    "RESOURCE_WARNING" if request.event_category == "quota_exceeded" else "RUN_FAILED"
                ),
                severity_override_applied=override_applied,
                override_reason=override_reason,
                policy_rationale=(
                    f"Operational failure/limit '{request.event_category}' mapped to ACTION_REQUIRED "
                    f"with impact '{impact}'."
                ),
            )

        if request.event_category in {"anomaly", "resource_warning"}:
            trusted_error = (
                not is_worker
                and request.claimed_severity == "ERROR"
                and auth_context.actor_kind
                in {"master_agent", "human_operator", "runtime_service", "trusted_verifier"}
            )
            if is_worker and request.claimed_severity in {"ERROR", "CRITICAL"}:
                override_applied = True
                override_reason = (
                    "Worker severity claim capped to WARNING; ERROR requires verified QA/Runtime/Resources triage."
                )
            classification: InterruptClassification = (
                "ACTION_REQUIRED" if trusted_error else "ATTENTION_NEEDED"
            )
            severity = "ERROR" if trusted_error else "WARNING"
            priority = "P2_HIGH" if request.event_category == "anomaly" or trusted_error else "P3_NORMAL"
            urgency: InterruptUrgency = "HIGH" if trusted_error else "MODERATE"
            return InterruptClassificationDecision(
                classification=classification,
                severity=severity,
                priority=priority,
                urgency=urgency,
                impact=request.claimed_impact or "SINGLE_TASK",
                notification_type=(
                    "RESOURCE_WARNING" if request.event_category == "resource_warning" else "TASK_UPDATE"
                ),
                severity_override_applied=override_applied,
                override_reason=override_reason,
                policy_rationale=(
                    f"Signal '{request.event_category}' classified as '{classification}' "
                    f"(severity={severity}, priority={priority})."
                ),
            )

        # status_update -> INFORMATIONAL
        if is_worker and request.claimed_severity in {"WARNING", "ERROR", "CRITICAL"}:
            override_applied = True
            override_reason = (
                "Worker status_update severity normalized to INFO to prevent false alarm inflation."
            )
        return InterruptClassificationDecision(
            classification="INFORMATIONAL",
            severity="INFO",
            priority="P4_LOW",
            urgency="LOW",
            impact=request.claimed_impact or "SINGLE_STEP",
            notification_type="TASK_UPDATE",
            severity_override_applied=override_applied,
            override_reason=override_reason,
            policy_rationale="Routine status update routed as INFORMATIONAL without urgent escalation.",
        )

    @staticmethod
    def _resolve_routing_targets(
        tx: InterruptTransactionContext,
        request: InterruptSubmitRequest,
        decision: InterruptClassificationDecision,
        auth_context: TrustedAuthContext,
        assigned_agents: list[str],
    ) -> tuple[list[RoutingTarget], bool]:
        """Select minimal governed targets, checking real agent registration & online sessions."""
        targets: list[RoutingTarget] = []
        seen_ids: set[str] = set()
        master_notified = False

        def _add_target(target: RoutingTarget) -> None:
            nonlocal master_notified
            if target.target_id not in seen_ids:
                seen_ids.add(target.target_id)
                targets.append(target)
                if target.target_kind == "master" or target.target_id == "core.master":
                    master_notified = True

        if request.target_department is not None and request.target_department not in _VALID_DEPARTMENTS:
            raise DepartmentGovernanceError(
                "UNAUTHORIZED_ROUTING_TARGET",
                f"Unknown or forbidden target department '{request.target_department}'.",
            )

        # 1. Explicit agent target validation + real online check.
        if request.target_agent_id is not None:
            snap = tx.check_agent_availability(request.target_agent_id, task_id=request.task_id)
            if not snap.registered or snap.registration_status != "ACTIVE":
                raise DepartmentGovernanceError(
                    "UNAUTHORIZED_ROUTING_TARGET",
                    f"Agent '{request.target_agent_id}' is not an ACTIVE registered agent.",
                )
            if request.target_department is not None and snap.department != request.target_department:
                raise DepartmentGovernanceError(
                    "UNAUTHORIZED_ROUTING_TARGET",
                    (
                        f"Agent '{request.target_agent_id}' belongs to department '{snap.department}', "
                        f"not '{request.target_department}'."
                    ),
                )
            if (
                auth_context.actor_kind == "worker_agent"
                and request.target_agent_id != "core.master"
                and not snap.assigned_to_task
                and request.event_category != "specialist_question"
            ):
                raise DepartmentGovernanceError(
                    "UNAUTHORIZED_ROUTING_TARGET",
                    (
                        f"Worker '{auth_context.actor_id}' cannot arbitrarily notify unassigned "
                        f"agent '{request.target_agent_id}'."
                    ),
                )
            _add_target(
                RoutingTarget(
                    target_id=request.target_agent_id,
                    target_kind="master" if request.target_agent_id == "core.master" else "agent",
                    department=snap.department,
                    delivery_status="ROUTED" if snap.online_confirmed else "QUEUED_FOR_ONLINE",
                    verified_online=snap.online_confirmed,
                    reason=(
                        "Confirmed active runtime session on target agent."
                        if snap.online_confirmed
                        else "Agent has no confirmed online runtime session; queued without assuming online."
                    ),
                )
            )
            if not snap.online_confirmed and snap.department and snap.department in _VALID_DEPARTMENTS:
                _add_target(
                    RoutingTarget(
                        target_id=snap.department,
                        target_kind="department",
                        department=snap.department,
                        delivery_status="FALLBACK_DEPARTMENT",
                        verified_online=None,
                        reason=f"Target agent '{request.target_agent_id}' offline; queued on department '{snap.department}'.",
                    )
                )

        # 2. Explicit department target routing (never assumes agents online).
        if request.target_department is not None:
            dept_agents = tx.list_department_agents(request.target_department, task_id=request.task_id)
            online_agents = [a for a in dept_agents if a.online_confirmed]
            _add_target(
                RoutingTarget(
                    target_id=request.target_department,
                    target_kind="department",
                    department=request.target_department,
                    delivery_status="ROUTED",
                    verified_online=len(online_agents) > 0,
                    reason=(
                        f"Department '{request.target_department}' has {len(online_agents)} verified online agent(s)."
                        if online_agents
                        else f"No verified online session in '{request.target_department}'; routed to department queue."
                    ),
                )
            )

        # 3. Category & severity routing policy (bounded & non-broadcasting).
        if decision.classification == "CRITICAL_EMERGENCY":
            _add_target(
                RoutingTarget(
                    target_id="core.master",
                    target_kind="master",
                    delivery_status="ESCALATED_TO_MASTER",
                    verified_online=tx.check_agent_availability("core.master", task_id=request.task_id).online_confirmed,
                    reason="CRITICAL_EMERGENCY must notify Master immediately.",
                )
            )
            if request.event_category == "security_breach":
                _add_target(
                    RoutingTarget(
                        target_id="human_operator",
                        target_kind="human_operator",
                        delivery_status="ROUTED",
                        verified_online=None,
                        reason="Security breach requires operator visibility.",
                    )
                )
        elif decision.classification == "HUMAN_INTERVENTION_REQUIRED":
            _add_target(
                RoutingTarget(
                    target_id="human_operator",
                    target_kind="human_operator",
                    delivery_status="ROUTED",
                    verified_online=None,
                    reason="Human intervention required via Action Inbox.",
                )
            )
            _add_target(
                RoutingTarget(
                    target_id="core.master",
                    target_kind="master",
                    delivery_status="ESCALATED_TO_MASTER",
                    verified_online=None,
                    reason="Master notified that execution awaits human input/approval.",
                )
            )
        elif decision.classification == "ACTION_REQUIRED":
            if request.event_category == "quota_exceeded":
                _add_target(
                    RoutingTarget(
                        target_id="resources",
                        target_kind="department",
                        department="resources",
                        delivery_status="ROUTED",
                        verified_online=None,
                        reason="Quota exhaustion requires Resources triage.",
                    )
                )
                _add_target(
                    RoutingTarget(
                        target_id="core.master",
                        target_kind="master",
                        delivery_status="ESCALATED_TO_MASTER",
                        verified_online=None,
                        reason="Master notified of quota block requiring scheduling decision.",
                    )
                )
            elif request.event_category == "run_failure":
                _add_target(
                    RoutingTarget(
                        target_id="qa",
                        target_kind="department",
                        department="qa",
                        delivery_status="ROUTED",
                        verified_online=None,
                        reason="Run failure routed to QA for failure verification.",
                    )
                )
                _add_target(
                    RoutingTarget(
                        target_id="core.master",
                        target_kind="master",
                        delivery_status="ESCALATED_TO_MASTER",
                        verified_online=None,
                        reason="Run failure escalated to Master for replanning.",
                    )
                )
            elif not targets:
                _add_target(
                    RoutingTarget(
                        target_id="core.master",
                        target_kind="master",
                        delivery_status="ESCALATED_TO_MASTER",
                        verified_online=None,
                        reason="Actionable event routed to Master when no specialist target specified.",
                    )
                )
        elif decision.classification == "ATTENTION_NEEDED":
            if request.event_category in {"anomaly", "resource_warning"}:
                _add_target(
                    RoutingTarget(
                        target_id="resources",
                        target_kind="department",
                        department="resources",
                        delivery_status="ROUTED",
                        verified_online=None,
                        reason="Resource/anomaly signal routed to Resources.",
                    )
                )
            if request.event_category == "anomaly":
                _add_target(
                    RoutingTarget(
                        target_id="qa",
                        target_kind="department",
                        department="qa",
                        delivery_status="ROUTED",
                        verified_online=None,
                        reason="Operational anomaly routed to QA without broadcasting all departments.",
                    )
                )
            if decision.impact in {"MULTI_TASK", "WORKSPACE_WIDE", "SYSTEM_WIDE"}:
                _add_target(
                    RoutingTarget(
                        target_id="core.master",
                        target_kind="master",
                        delivery_status="ESCALATED_TO_MASTER",
                        verified_online=None,
                        reason="Multi-task or wider impact warrants Master awareness.",
                    )
                )
        else:
            # INFORMATIONAL: notify only directly involved task actors, never broadcast or flood Master.
            for agent_id in assigned_agents:
                if agent_id not in seen_ids:
                    snap = tx.check_agent_availability(agent_id, task_id=request.task_id)
                    _add_target(
                        RoutingTarget(
                            target_id=agent_id,
                            target_kind="master" if agent_id == "core.master" else "agent",
                            department=snap.department,
                            delivery_status="ROUTED" if snap.online_confirmed else "QUEUED_FOR_ONLINE",
                            verified_online=snap.online_confirmed,
                            reason="Scoped informational update to Task stakeholder.",
                        )
                    )
            if not targets:
                dept = request.target_department or "reception"
                _add_target(
                    RoutingTarget(
                        target_id=dept,
                        target_kind="department",
                        department=dept,
                        delivery_status="ROUTED",
                        verified_online=None,
                        reason="Scoped informational log recorded without broad notification.",
                    )
                )

        return targets, master_notified

    def submit_interrupt(
        self,
        request: InterruptSubmitRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> InterruptSubmitOutcome:
        """Classify, route, deduplicate, and persist an interrupt incident without mutating Tasks."""
        repo = self._require_repo()
        if request.claimed_actor_id is not None and request.claimed_actor_id != auth_context.actor_id:
            raise DepartmentGovernanceError(
                "ACTOR_IDENTITY_MISMATCH",
                f"Claimed actor '{request.claimed_actor_id}' mismatches authenticated '{auth_context.actor_id}'.",
            )
        if auth_context.actor_kind == "worker_agent":
            if request.task_id is None:
                raise DepartmentGovernanceError(
                    "TASK_OWNERSHIP_DENIED",
                    f"Worker '{auth_context.actor_id}' must reference an assigned task_id when reporting interrupts.",
                )
            if request.task_id not in auth_context.assigned_task_ids:
                raise DepartmentGovernanceError(
                    "TASK_OWNERSHIP_DENIED",
                    f"Worker '{auth_context.actor_id}' is not assigned to Task '{request.task_id}'.",
                )

        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource=f"reception:interrupt:{request.interrupt_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            approval_id=request.approval_id,
            timestamp=request.timestamp,
            correlation_id=f"intr-{request.interrupt_id}",
        )

        decision = self._classify_request(request, auth_context)
        fingerprint = _compute_interrupt_fingerprint(request)

        def _tx_op(tx: InterruptTransactionContext) -> InterruptSubmitOutcome:
            # 1. Exact event dedup/idempotency check.
            if request.event_dedup_key is not None:
                hit = tx.find_event_by_dedup_key(request.dedup_scope, request.event_dedup_key)
                if hit is not None:
                    saved_fp, saved_outcome = hit
                    if saved_fp != fingerprint:
                        raise InterruptValidationError(
                            "EVENT_DEDUP_CONFLICT",
                            (
                                f"event_dedup_key '{request.event_dedup_key}' in scope '{request.dedup_scope}' "
                                "already exists with a different payload fingerprint."
                            ),
                        )
                    return saved_outcome.model_copy(
                        update={
                            "deduplicated": True,
                            "dedup_reason": "EXACT_EVENT_RETRY",
                            "notification_suppressed": True,
                        }
                    )

            # 2. Reference verification without touching Task/Run status.
            assigned_agents: list[str] = []
            if request.task_id is not None:
                task = tx.get_task(request.task_id)
                if task is None:
                    raise InterruptReferenceError(
                        "TARGET_TASK_NOT_FOUND",
                        f"Referenced Task '{request.task_id}' does not exist.",
                    )
                assigned_agents = list(task.assigned_agent_ids)
            if request.run_id is not None:
                parent_task = tx.get_run_task_id(request.run_id)
                if parent_task is None or (request.task_id is not None and parent_task != request.task_id):
                    raise InterruptReferenceError(
                        "TARGET_RUN_MISMATCH_OR_NOT_FOUND",
                        f"Run '{request.run_id}' does not exist or belong to Task '{request.task_id}'.",
                    )
            if request.conversation_id is not None:
                conv_owner = tx.get_conversation_owner(request.conversation_id)
                if conv_owner is None:
                    raise InterruptReferenceError(
                        "CONVERSATION_NOT_FOUND",
                        f"Referenced Conversation '{request.conversation_id}' does not exist.",
                    )
                if (
                    auth_context.actor_kind == "human_operator"
                    and conv_owner != auth_context.actor_id
                ):
                    raise DepartmentGovernanceError(
                        "CONVERSATION_OWNERSHIP_DENIED",
                        f"Operator '{auth_context.actor_id}' does not own Conversation '{request.conversation_id}'.",
                    )

            targets, master_notified = self._resolve_routing_targets(
                tx, request, decision, auth_context, assigned_agents
            )
            actor_id_str = _actor_label(auth_context)

            # 3. Correlate active incident by incident_key if present; never merge resolved incidents or plain summary.
            active_incident = (
                tx.find_active_incident_by_key(request.dedup_scope, request.incident_key)
                if request.incident_key is not None
                else None
            )
            if active_incident is not None:
                merged_classification = (
                    decision.classification
                    if _CLASSIFICATION_RANK[decision.classification]
                    > _CLASSIFICATION_RANK[active_incident.classification]
                    else active_incident.classification
                )
                merged_severity = (
                    decision.severity
                    if _SEVERITY_RANK[decision.severity] > _SEVERITY_RANK[active_incident.severity]
                    else active_incident.severity
                )
                merged_priority = (
                    decision.priority
                    if _PRIORITY_RANK[decision.priority] > _PRIORITY_RANK[active_incident.priority]
                    else active_incident.priority
                )
                merged_urgency = (
                    decision.urgency
                    if _URGENCY_RANK[decision.urgency] > _URGENCY_RANK[active_incident.urgency]
                    else active_incident.urgency
                )
                merged_impact = (
                    decision.impact
                    if _IMPACT_RANK[decision.impact] > _IMPACT_RANK[active_incident.impact]
                    else active_incident.impact
                )
                existing_targets = {t.target_id: t for t in active_incident.routing_targets}
                for tgt in targets:
                    if tgt.target_id not in existing_targets:
                        existing_targets[tgt.target_id] = tgt
                merged_targets = list(existing_targets.values())
                merged_master = active_incident.master_notified or master_notified

                suppress_ev = _make_interrupt_event(
                    event_type="reception.notification_suppressed",
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                    correlation_id=f"intr-{active_incident.incident_id}",
                    task_id=request.task_id,
                    run_id=request.run_id,
                    payload={
                        "department": "reception",
                        "incident_id": active_incident.incident_id,
                        "interrupt_id": request.interrupt_id,
                        "dedup_scope": request.dedup_scope,
                        "incident_key": request.incident_key,
                        "reason": "ACTIVE_INCIDENT_AGGREGATED",
                        "occurrence_count": active_incident.occurrence_count + 1,
                        "notification_id": active_incident.notification_id,
                    },
                )
                tx.append_event(suppress_ev)
                updated_inc = tx.update_incident_aggregation(
                    incident_id=active_incident.incident_id,
                    expected_record_version=active_incident.record_version,
                    occurrence_count=active_incident.occurrence_count + 1,
                    classification=merged_classification,
                    severity=merged_severity,
                    priority=merged_priority,
                    urgency=merged_urgency,
                    impact=merged_impact,
                    routing_targets=merged_targets,
                    master_notified=merged_master,
                    action_inbox_item_id=active_incident.action_inbox_item_id,
                    audit_event_id=suppress_ev.event_id,
                    updated_at=request.timestamp,
                )
                agg_outcome = InterruptSubmitOutcome(
                    incident=updated_inc,
                    decision=decision,
                    deduplicated=True,
                    dedup_reason="ACTIVE_INCIDENT_AGGREGATED",
                    notification_suppressed=True,
                    notification_id=updated_inc.notification_id,
                    action_inbox_item=None,
                    audit_event_id=suppress_ev.event_id,
                )
                tx.append_interrupt_event(
                    InterruptEventRecord(
                        interrupt_event_id=request.interrupt_id,
                        incident_id=updated_inc.incident_id,
                        dedup_scope=request.dedup_scope,
                        event_dedup_key=request.event_dedup_key,
                        request_fingerprint=fingerprint,
                        source=request.source,
                        event_category=request.event_category,
                        dedup_reason="ACTIVE_INCIDENT_AGGREGATED",
                        deduplicated=True,
                        summary=request.summary,
                        notification_suppressed=True,
                        notification_id=updated_inc.notification_id,
                        submitted_by_actor_id=actor_id_str,
                        audit_event_id=suppress_ev.event_id,
                        created_at=request.timestamp,
                    ),
                    outcome=agg_outcome,
                )
                return agg_outcome

            prior_closed = (
                tx.find_latest_incident_by_key(request.dedup_scope, request.incident_key)
                if request.incident_key is not None
                else None
            )
            dedup_reason: InterruptDedupReason = (
                "POST_RESOLUTION_RECURRENCE" if prior_closed is not None else "NEW_INCIDENT"
            )
            incident_id = new_incident_id()

            ev = _make_interrupt_event(
                event_type="reception.interrupt_classified",
                auth_context=auth_context,
                timestamp=request.timestamp,
                correlation_id=f"intr-{incident_id}",
                task_id=request.task_id,
                run_id=request.run_id,
                payload={
                    "department": "reception",
                    "incident_id": incident_id,
                    "interrupt_id": request.interrupt_id,
                    "classification": decision.classification,
                    "severity": decision.severity,
                    "priority": decision.priority,
                    "urgency": decision.urgency,
                    "impact": decision.impact,
                    "source": request.source,
                    "event_category": request.event_category,
                    "master_notified": master_notified,
                    "broadcast_all_departments": False,
                    "dedup_reason": dedup_reason,
                    "severity_override_applied": decision.severity_override_applied,
                    "route_targets": [t.target_id for t in targets],
                },
            )
            tx.append_event(ev)

            saved_notif = tx.save_notification(
                NotificationRef(
                    task_id=request.task_id,
                    run_id=request.run_id,
                    event_id=ev.event_id,
                    severity=decision.severity,
                    notification_type=decision.notification_type,
                    created_at=request.timestamp,
                )
            )

            inbox_item: ActionInboxItem | None = None
            if decision.classification in {
                "CRITICAL_EMERGENCY",
                "HUMAN_INTERVENTION_REQUIRED",
                "ACTION_REQUIRED",
            }:
                inbox_item = tx.insert_action_inbox_item(
                    ActionInboxItem(
                        inbox_item_id=f"ibx_{uuid.uuid4().hex[:14]}",
                        item_kind=_map_inbox_kind(decision, request),
                        priority=_map_inbox_priority(decision.priority),  # type: ignore[arg-type]
                        conversation_id=request.conversation_id,
                        summary=scrub_secret_text(request.summary),
                        task_id=request.task_id,
                        run_id=request.run_id,
                        approval_id=request.approval_id,
                        assigned_route=[t.target_id for t in targets],
                        created_at=request.timestamp,
                    ),
                    audit_event_id=ev.event_id,
                )

            initial_status: InterruptStatus = "DELIVERED" if any(
                t.delivery_status in {"ROUTED", "ESCALATED_TO_MASTER"} for t in targets
            ) else "PENDING_DELIVERY"

            incident = InterruptIncidentRecord(
                incident_id=incident_id,
                interrupt_id=request.interrupt_id,
                dedup_scope=request.dedup_scope,
                incident_key=request.incident_key,
                request_fingerprint=fingerprint,
                classification=decision.classification,
                severity=decision.severity,
                priority=decision.priority,
                urgency=decision.urgency,
                impact=decision.impact,
                status=initial_status,
                source=request.source,
                event_category=request.event_category,
                summary=request.summary,
                details=request.details,
                task_id=request.task_id,
                run_id=request.run_id,
                conversation_id=request.conversation_id,
                approval_id=request.approval_id,
                routing_targets=targets,
                master_notified=master_notified,
                action_inbox_item_id=inbox_item.inbox_item_id if inbox_item else None,
                notification_id=saved_notif.notification_id,
                occurrence_count=1,
                policy_rationale=decision.policy_rationale,
                submitted_by_actor_id=actor_id_str,
                submitted_by_actor_kind=auth_context.actor_kind,
                audit_event_id=ev.event_id,
                record_version=1,
                created_at=request.timestamp,
                updated_at=request.timestamp,
            )
            inserted_inc = tx.insert_incident(incident)
            outcome = InterruptSubmitOutcome(
                incident=inserted_inc,
                decision=decision,
                deduplicated=False,
                dedup_reason=dedup_reason,
                notification_suppressed=False,
                notification_id=saved_notif.notification_id,
                action_inbox_item=inbox_item,
                audit_event_id=ev.event_id,
            )
            tx.append_interrupt_event(
                InterruptEventRecord(
                    interrupt_event_id=request.interrupt_id,
                    incident_id=inserted_inc.incident_id,
                    dedup_scope=request.dedup_scope,
                    event_dedup_key=request.event_dedup_key,
                    request_fingerprint=fingerprint,
                    source=request.source,
                    event_category=request.event_category,
                    dedup_reason=dedup_reason,
                    deduplicated=False,
                    summary=request.summary,
                    notification_suppressed=False,
                    notification_id=saved_notif.notification_id,
                    submitted_by_actor_id=actor_id_str,
                    audit_event_id=ev.event_id,
                    created_at=request.timestamp,
                ),
                outcome=outcome,
            )
            return outcome

        return repo.execute_transaction(_tx_op)

    def acknowledge_interrupt(
        self,
        request: InterruptAckRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> InterruptIncidentRecord:
        """Record explicit acknowledgement without erroneously marking the incident RESOLVED."""
        repo = self._require_repo()
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource=f"reception:interrupt:{request.incident_id}:ack",
            timestamp=request.timestamp,
            correlation_id=f"intr-ack-{request.incident_id}",
        )

        def _tx_op(tx: InterruptTransactionContext) -> InterruptIncidentRecord:
            existing = tx.get_incident_for_update(request.incident_id)
            if existing is None:
                raise InterruptReferenceError(
                    "INCIDENT_NOT_FOUND",
                    f"Interrupt incident '{request.incident_id}' does not exist.",
                )
            if existing.status in {"RESOLVED", "DISMISSED"}:
                raise InterruptStateError(
                    "INVALID_INTERRUPT_STATE",
                    f"Cannot acknowledge incident '{request.incident_id}' already in status '{existing.status}'.",
                )
            if auth_context.actor_kind == "worker_agent":
                target_ids = {t.target_id for t in existing.routing_targets}
                task_allowed = (
                    existing.task_id is not None and existing.task_id in auth_context.assigned_task_ids
                )
                if auth_context.actor_id not in target_ids and not task_allowed:
                    raise DepartmentGovernanceError(
                        "UNAUTHORIZED_INTERRUPT_ACK",
                        f"Worker '{auth_context.actor_id}' cannot acknowledge incident '{request.incident_id}'.",
                    )

            actor = _actor_label(auth_context)
            ev = _make_interrupt_event(
                event_type="reception.interrupt_acknowledged",
                auth_context=auth_context,
                timestamp=request.timestamp,
                correlation_id=f"intr-ack-{existing.incident_id}",
                task_id=existing.task_id,
                run_id=existing.run_id,
                payload={
                    "department": "reception",
                    "incident_id": existing.incident_id,
                    "acknowledged_by": actor,
                    "acknowledgement_note": request.acknowledgement_note,
                    "remains_unresolved": True,
                },
            )
            tx.append_event(ev)
            updated = tx.update_incident_ack(
                incident_id=existing.incident_id,
                expected_record_version=request.expected_record_version,
                acknowledged_by=actor,
                acknowledged_at=request.timestamp,
                note=request.acknowledgement_note,
                audit_event_id=ev.event_id,
                updated_at=request.timestamp,
            )
            if existing.action_inbox_item_id is not None:
                tx.update_action_inbox_status(
                    existing.action_inbox_item_id,
                    status="ACKNOWLEDGED",
                    updated_at=request.timestamp,
                )
            return updated

        return repo.execute_transaction(_tx_op)

    def escalate_interrupt(
        self,
        request: InterruptEscalateRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> InterruptEscalationRecord:
        """Escalate incident severity/classification with immutable audit history and Master routing."""
        repo = self._require_repo()
        if auth_context.actor_kind == "worker_agent":
            raise DepartmentGovernanceError(
                "WORKER_ESCALATION_FORBIDDEN",
                f"Worker '{auth_context.actor_id}' is not authorized to escalate incidents directly.",
            )
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.update_plan",
            resource=f"reception:interrupt:{request.incident_id}:escalate",
            timestamp=request.timestamp,
            correlation_id=f"intr-esc-{request.incident_id}",
        )

        def _tx_op(tx: InterruptTransactionContext) -> InterruptEscalationRecord:
            existing = tx.get_incident_for_update(request.incident_id)
            if existing is None:
                raise InterruptReferenceError(
                    "INCIDENT_NOT_FOUND",
                    f"Interrupt incident '{request.incident_id}' does not exist.",
                )
            if existing.status in {"RESOLVED", "DISMISSED"}:
                raise InterruptStateError(
                    "INVALID_INTERRUPT_STATE",
                    f"Cannot escalate closed incident '{request.incident_id}' ({existing.status}).",
                )
            if (
                request.additional_target_department is not None
                and request.additional_target_department not in _VALID_DEPARTMENTS
            ):
                raise DepartmentGovernanceError(
                    "UNAUTHORIZED_ROUTING_TARGET",
                    f"Invalid target department '{request.additional_target_department}'.",
                )

            # Determine new severity & classification strictly monotonically upward or to requested tier.
            next_severity = request.target_severity
            if next_severity is None:
                promo_sev: dict[InterruptSeverity, InterruptSeverity] = {
                    "INFO": "WARNING",
                    "WARNING": "ERROR",
                    "ERROR": "CRITICAL",
                    "CRITICAL": "CRITICAL",
                }
                next_severity = promo_sev[existing.severity]
            elif _SEVERITY_RANK[next_severity] < _SEVERITY_RANK[existing.severity]:
                raise InterruptValidationError(
                    "DOWNGRADE_VIA_ESCALATE_FORBIDDEN",
                    "Escalation cannot lower incident severity.",
                )

            next_class = request.target_classification
            if next_class is None:
                next_class = (
                    "CRITICAL_EMERGENCY"
                    if next_severity == "CRITICAL"
                    else (
                        "ACTION_REQUIRED"
                        if _CLASSIFICATION_RANK[existing.classification] < 3
                        else existing.classification
                    )
                )
            elif _CLASSIFICATION_RANK[next_class] < _CLASSIFICATION_RANK[existing.classification]:
                raise InterruptValidationError(
                    "DOWNGRADE_VIA_ESCALATE_FORBIDDEN",
                    "Escalation cannot lower incident classification.",
                )

            next_priority = _PRIORITY_BY_CLASSIFICATION[next_class]
            next_urgency: InterruptUrgency = "IMMEDIATE" if next_severity == "CRITICAL" else "HIGH"

            merged = {t.target_id: t for t in existing.routing_targets}
            added_targets: list[RoutingTarget] = []
            if "core.master" not in merged:
                m_tgt = RoutingTarget(
                    target_id="core.master",
                    target_kind="master",
                    delivery_status="ESCALATED_TO_MASTER",
                    verified_online=None,
                    reason=f"Escalated to Master: {request.reason}",
                )
                merged["core.master"] = m_tgt
                added_targets.append(m_tgt)
            if (
                request.additional_target_department is not None
                and request.additional_target_department not in merged
            ):
                d_tgt = RoutingTarget(
                    target_id=request.additional_target_department,
                    target_kind="department",
                    department=request.additional_target_department,
                    delivery_status="ROUTED",
                    verified_online=None,
                    reason=f"Added specialist department on escalation: {request.reason}",
                )
                merged[d_tgt.target_id] = d_tgt
                added_targets.append(d_tgt)

            actor = _actor_label(auth_context)
            esc_id = new_escalation_id()
            ev = _make_interrupt_event(
                event_type="reception.interrupt_escalated",
                auth_context=auth_context,
                timestamp=request.timestamp,
                correlation_id=f"intr-esc-{existing.incident_id}",
                task_id=existing.task_id,
                run_id=existing.run_id,
                payload={
                    "department": "reception",
                    "incident_id": existing.incident_id,
                    "escalation_id": esc_id,
                    "from_classification": existing.classification,
                    "to_classification": next_class,
                    "from_severity": existing.severity,
                    "to_severity": next_severity,
                    "escalated_by": actor,
                    "reason": request.reason,
                },
            )
            tx.append_event(ev)

            esc_notif = tx.save_notification(
                NotificationRef(
                    task_id=existing.task_id,
                    run_id=existing.run_id,
                    event_id=ev.event_id,
                    severity=next_severity,
                    notification_type="SECURITY_ALERT" if next_severity == "CRITICAL" else "TASK_UPDATE",
                    created_at=request.timestamp,
                )
            )
            esc_record = InterruptEscalationRecord(
                escalation_id=esc_id,
                incident_id=existing.incident_id,
                from_classification=existing.classification,
                to_classification=next_class,
                from_severity=existing.severity,
                to_severity=next_severity,
                escalated_by=actor,
                reason=request.reason,
                added_targets=added_targets,
                notification_id=esc_notif.notification_id,
                audit_event_id=ev.event_id,
                created_at=request.timestamp,
            )
            tx.update_incident_escalation(
                incident_id=existing.incident_id,
                expected_record_version=request.expected_record_version,
                classification=next_class,
                severity=next_severity,
                priority=next_priority,
                urgency=next_urgency,
                routing_targets=list(merged.values()),
                master_notified=True,
                notification_id=esc_notif.notification_id,
                escalation=esc_record,
                audit_event_id=ev.event_id,
                updated_at=request.timestamp,
            )
            return esc_record

        return repo.execute_transaction(_tx_op)

    def resolve_interrupt(
        self,
        request: InterruptResolveRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> InterruptIncidentRecord:
        """Resolve or dismiss an Incident under Governance RBAC without changing Task status."""
        repo = self._require_repo()
        if auth_context.actor_kind == "worker_agent":
            raise DepartmentGovernanceError(
                "WORKER_RESOLUTION_FORBIDDEN",
                f"Worker '{auth_context.actor_id}' is not authorized to resolve or dismiss incidents.",
            )
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="task.update_plan",
            resource=f"reception:interrupt:{request.incident_id}:resolve",
            timestamp=request.timestamp,
            correlation_id=f"intr-res-{request.incident_id}",
        )

        def _tx_op(tx: InterruptTransactionContext) -> InterruptIncidentRecord:
            existing = tx.get_incident_for_update(request.incident_id)
            if existing is None:
                raise InterruptReferenceError(
                    "INCIDENT_NOT_FOUND",
                    f"Interrupt incident '{request.incident_id}' does not exist.",
                )
            if existing.status in {"RESOLVED", "DISMISSED"}:
                raise InterruptStateError(
                    "INVALID_INTERRUPT_STATE",
                    f"Incident '{request.incident_id}' is already closed ({existing.status}).",
                )

            actor = _actor_label(auth_context)
            target_status = "DISMISSED" if request.dismiss else "RESOLVED"
            ev = _make_interrupt_event(
                event_type="reception.interrupt_resolved",
                auth_context=auth_context,
                timestamp=request.timestamp,
                correlation_id=f"intr-res-{existing.incident_id}",
                task_id=existing.task_id,
                run_id=existing.run_id,
                payload={
                    "department": "reception",
                    "incident_id": existing.incident_id,
                    "status": target_status,
                    "resolved_by": actor,
                    "outcome": request.outcome,
                    "resolution_note": request.resolution_note,
                },
            )
            tx.append_event(ev)
            updated = tx.update_incident_resolution(
                incident_id=existing.incident_id,
                expected_record_version=request.expected_record_version,
                status=target_status,
                resolved_by=actor,
                resolved_at=request.timestamp,
                outcome=request.outcome,
                note=request.resolution_note,
                audit_event_id=ev.event_id,
                updated_at=request.timestamp,
            )
            if existing.action_inbox_item_id is not None:
                tx.update_action_inbox_status(
                    existing.action_inbox_item_id,
                    status="DISMISSED" if request.dismiss else "RESOLVED",
                    updated_at=request.timestamp,
                )
            return updated

        return repo.execute_transaction(_tx_op)

    def get_interrupt(
        self,
        incident_id: str,
        *,
        auth_context: TrustedAuthContext | None = None,
    ) -> InterruptIncidentRecord | None:
        """Fetch an InterruptIncidentRecord enforcing worker ownership when scoped."""
        repo = self._require_repo()
        inc = repo.get_incident(incident_id)
        if inc is not None and auth_context is not None and auth_context.actor_kind == "worker_agent":
            if inc.task_id is None or inc.task_id not in auth_context.assigned_task_ids:
                raise DepartmentGovernanceError(
                    "TASK_OWNERSHIP_DENIED",
                    f"Worker '{auth_context.actor_id}' cannot read incident '{incident_id}'.",
                )
        return inc

    def list_interrupts(
        self,
        *,
        task_id: str | None = None,
        status: InterruptStatus | None = None,
        severity: InterruptSeverity | None = None,
        classification: InterruptClassification | None = None,
        auth_context: TrustedAuthContext | None = None,
    ) -> list[InterruptIncidentRecord]:
        """List persisted InterruptIncidentRecords scoped to caller authority."""
        repo = self._require_repo()
        if auth_context is not None and auth_context.actor_kind == "worker_agent":
            if task_id is None or task_id not in auth_context.assigned_task_ids:
                raise DepartmentGovernanceError(
                    "TASK_OWNERSHIP_DENIED",
                    f"Worker '{auth_context.actor_id}' may list interrupts only for assigned task_ids.",
                )
        return repo.list_incidents(
            task_id=task_id,
            status=status,
            severity=severity,
            classification=classification,
        )

    def list_interrupt_events(self, incident_id: str) -> list[InterruptEventRecord]:
        """List immutable InterruptEventRecords attached to ``incident_id``."""
        return self._require_repo().list_interrupt_events(incident_id)

    def list_interrupt_escalations(self, incident_id: str) -> list[InterruptEscalationRecord]:
        """List immutable InterruptEscalationRecords attached to ``incident_id``."""
        return self._require_repo().list_escalations(incident_id)

    def recover_open_interrupts(self) -> list[InterruptIncidentRecord]:
        """Return all non-closed incidents after restart without re-emitting notifications."""
        repo = self._require_repo()
        return [
            inc
            for inc in repo.list_incidents()
            if inc.status not in {"RESOLVED", "DISMISSED"}
        ]
