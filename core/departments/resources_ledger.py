"""Pure AST domain logic for P11.04 persistent resource ledger, budget warnings, and quota alerts."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any

from core.contracts.event import Event, EventType, sanitize_event_payload
from core.contracts.identifiers import (
    new_event_id,
    new_notification_id,
    new_resource_alert_id,
    scrub_secret_text,
)
from core.contracts.references import NotificationRef
from core.departments._common import authorize_and_audit, event_actor_id
from core.departments.reception_models import ActionInboxItem
from core.departments.resources_models import (
    BucketQuotaSummary,
    EvaluatedAlertOutcome,
    RecordResourceUsageOutcome,
    RecordResourceUsageRequest,
    RegisterBudgetPolicyRequest,
    ResourceAlertCategory,
    ResourceAlertSeverity,
    ResourceBudgetPolicy,
    ResourceLedgerConflictError,
    ResourceLedgerEntry,
    ResourceLedgerGovernanceError,
    ResourceLedgerPersistenceError,
    ResourceLedgerQuery,
    ResourceLedgerReferenceError,
    ResourceLedgerRepository,
    ResourceLedgerTransactionContext,
    ResourceLedgerValidationError,
    ResourceThresholdAlert,
    TaskResourceUsageSummary,
    derive_quota_bucket_key,
)
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source


def _require_repo(ledger_repository: ResourceLedgerRepository | None) -> ResourceLedgerRepository:
    """Ensure a durable ledger repository was injected into ``ResourcesDepartmentService``."""
    if ledger_repository is None:
        raise ResourceLedgerPersistenceError(
            "RESOURCE_LEDGER_REPOSITORY_UNAVAILABLE",
            "Durable ResourceLedgerRepository is required for P11.04 resource ledger operations.",
        )
    return ledger_repository


def _make_resources_event(
    *,
    event_type: EventType,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
    correlation_id: str,
    payload: dict[str, Any],
    task_id: str | None = None,
    run_id: str | None = None,
) -> Event:
    """Construct a secret-scrubbed Event attached to Task/Run/Actor without side effects."""
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


def _compute_usage_fingerprint(
    request: RecordResourceUsageRequest,
    quota_bucket_key: str,
) -> str:
    """Compute canonical SHA-256 fingerprint for idempotency conflict detection."""
    canonical = {
        "task_id": request.task_id,
        "run_id": request.run_id,
        "project_id": request.project_id,
        "agent_id": request.agent_id,
        "session_id": request.session_id,
        "provider_id": request.provider_id,
        "connection_id": request.connection_id,
        "model_id": request.model_id,
        "project_ref": request.project_ref,
        "account_ref": request.account_ref,
        "quota_bucket_key": quota_bucket_key,
        "duration_ms": request.duration_ms,
        "provider_duration_ms": request.provider_duration_ms,
        "duration_source": request.duration_source,
        "memory_mb": request.memory_mb,
        "peak_memory_mb": request.peak_memory_mb,
        "memory_source": request.memory_source,
        "input_tokens": request.input_tokens,
        "output_tokens": request.output_tokens,
        "thinking_tokens": request.thinking_tokens,
        "cached_tokens": request.cached_tokens,
        "total_tokens": request.total_tokens,
        "token_usage_status": request.token_usage_status,
        "actual_cost_usd": request.actual_cost_usd,
        "estimated_cost_usd": request.estimated_cost_usd,
        "cost_source": request.cost_source,
        "pricing_table_ref": request.pricing_table_ref,
        "is_estimated": request.is_estimated,
        "estimate_label": request.estimate_label,
        "quota_units_used": request.quota_units_used,
        "quota_limit_units": request.quota_limit_units,
        "quota_remaining_units": request.quota_remaining_units,
        "note": request.note,
        "metadata": request.metadata,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _resolve_telemetry_status(request: RecordResourceUsageRequest) -> str:
    """Derive ``RunTelemetryStatus`` honestly without mislabeling estimates or unavailable data."""
    if (
        request.is_estimated
        or request.cost_source == "ESTIMATED_LOCAL_PRICING_TABLE"
        or request.token_usage_status == "ESTIMATED"
    ):
        return "ESTIMATED_PENDING_AUDIT"
    if (
        request.cost_source == "UNAVAILABLE_FROM_PROVIDER"
        and request.token_usage_status == "UNAVAILABLE_FROM_PROVIDER"
    ):
        return "UNAVAILABLE_FROM_PROVIDER"
    return "VERIFIED_MEASURED"


def _evaluate_policy_thresholds(
    tx: ResourceLedgerTransactionContext,
    policy: ResourceBudgetPolicy,
    *,
    entry: ResourceLedgerEntry | None,
    trigger_event_id: str,
    task_id_hint: str | None,
    run_id_hint: str | None,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
) -> tuple[list[EvaluatedAlertOutcome], list[str]]:
    """Evaluate all threshold categories for one policy, performing dedup, escalation, and recovery."""
    totals = tx.compute_scope_totals(policy)
    est_label_text = (
        "; ".join(totals.estimate_labels)
        if totals.has_estimated_cost and totals.estimate_labels
        else (entry.estimate_label if entry is not None else None)
    )
    is_cost_estimated = totals.has_estimated_cost

    checks: list[tuple[ResourceAlertCategory, float | None, float | None, bool, str | None]] = [
        ("BUDGET_COST", policy.budget_cost_usd_limit, totals.total_cost_usd, is_cost_estimated, est_label_text),
        (
            "DURATION",
            float(policy.duration_ms_limit) if policy.duration_ms_limit is not None else None,
            float(totals.total_duration_ms) if totals.total_duration_ms is not None else None,
            False,
            None,
        ),
        ("MEMORY", policy.memory_mb_limit, totals.peak_memory_mb, False, None),
        (
            "TOKENS",
            float(policy.total_tokens_limit) if policy.total_tokens_limit is not None else None,
            float(totals.total_tokens) if totals.total_tokens is not None else None,
            bool(entry is not None and entry.token_usage_status == "ESTIMATED"),
            entry.estimate_label if entry is not None and entry.token_usage_status == "ESTIMATED" else None,
        ),
        (
            "QUOTA",
            float(policy.quota_units_limit) if policy.quota_units_limit is not None else None,
            float(totals.total_quota_units_used) if totals.total_quota_units_used is not None else None,
            False,
            None,
        ),
    ]

    outcomes: list[EvaluatedAlertOutcome] = []
    recovered_ids: list[str] = []

    for category, limit_val, observed_val, is_est_basis, est_label in checks:
        dedup_key = f"{policy.policy_id}:{category}:{policy.scope_kind}:{policy.scope_ref}"
        active_alert = tx.find_active_alert(dedup_key)
        if limit_val is None or limit_val <= 0 or observed_val is None:
            continue

        ratio = round(float(observed_val) / float(limit_val), 6)
        target_severity: ResourceAlertSeverity | None = None
        if ratio >= policy.critical_ratio:
            target_severity = "CRITICAL"
        elif ratio >= policy.warning_ratio:
            target_severity = "WARNING"

        if target_severity is None:
            if active_alert is not None:
                rec_ev = _make_resources_event(
                    event_type="resources.alert_resolved",
                    auth_context=auth_context,
                    timestamp=timestamp,
                    correlation_id=f"res-alert-{active_alert.alert_id}",
                    task_id=active_alert.task_id or task_id_hint,
                    run_id=active_alert.run_id or run_id_hint,
                    payload={
                        "department": "resources",
                        "alert_id": active_alert.alert_id,
                        "policy_id": policy.policy_id,
                        "alert_category": category,
                        "recovery": True,
                        "observed_value": observed_val,
                        "threshold_limit": limit_val,
                        "utilization_ratio": ratio,
                    },
                )
                saved_rec_ev = tx.append_event(rec_ev)
                tx.mark_alert_resolved(
                    alert_id=active_alert.alert_id,
                    expected_record_version=active_alert.record_version,
                    resolved_by=auth_context.actor_id,
                    resolved_at=timestamp,
                    note="Recovered below warning threshold after policy limit update.",
                    audit_event_id=saved_rec_ev.event_id,
                )
                if active_alert.action_inbox_item_id is not None:
                    tx.update_action_inbox_status(
                        active_alert.action_inbox_item_id,
                        status="RESOLVED",
                        updated_at=timestamp,
                    )
                recovered_ids.append(active_alert.alert_id)
            continue

        est_suffix = f" [ESTIMATED BASIS: {est_label}]" if is_est_basis and est_label else ""
        quota_note = (
            " [SHARED OR UNPROVEN QUOTA BUCKET]"
            if category == "QUOTA" and not policy.independent_quota_proof
            else ""
        )
        summary = scrub_secret_text(
            f"{category} {target_severity} on {policy.scope_kind}={policy.scope_ref}: "
            f"observed={observed_val:.4f}, limit={limit_val:.4f} ({ratio * 100:.1f}%)"
            f"{est_suffix}{quota_note}."
        )
        recommended_action = (
            "Require Master review and explicit operator approval before continuing, increasing budget, "
            "or switching models; do not terminate Tasks directly and do not rotate keys."
            if target_severity == "CRITICAL"
            else "Monitor consumption and prepare operator budget/quota review before reaching critical limit."
        )

        is_quota = category == "QUOTA"
        ev_type: EventType
        if target_severity == "CRITICAL":
            ev_type = "resources.quota_critical_triggered" if is_quota else "resources.budget_critical_triggered"
        else:
            ev_type = "resources.quota_warning_triggered" if is_quota else "resources.budget_warning_triggered"

        effective_task_id = entry.task_id if entry is not None else task_id_hint
        effective_run_id = entry.run_id if entry is not None else run_id_hint

        if active_alert is None:
            alert_id = new_resource_alert_id()
            alert_ev = _make_resources_event(
                event_type=ev_type,
                auth_context=auth_context,
                timestamp=timestamp,
                correlation_id=f"res-alert-{alert_id}",
                task_id=effective_task_id,
                run_id=effective_run_id,
                payload={
                    "department": "resources",
                    "alert_id": alert_id,
                    "policy_id": policy.policy_id,
                    "alert_category": category,
                    "scope_kind": policy.scope_kind,
                    "scope_ref": policy.scope_ref,
                    "provider_id": policy.provider_id or (entry.provider_id if entry else None),
                    "connection_id": policy.connection_id or (entry.connection_id if entry else None),
                    "model_id": policy.model_id or (entry.model_id if entry else None),
                    "quota_bucket_key": policy.quota_bucket_key or (entry.quota_bucket_key if entry else None),
                    "severity": target_severity,
                    "observed_value": observed_val,
                    "threshold_limit": limit_val,
                    "utilization_ratio": ratio,
                    "is_estimated_basis": is_est_basis,
                },
            )
            saved_ev = tx.append_event(alert_ev)
            saved_notif = tx.save_notification(
                NotificationRef(
                    notification_id=new_notification_id(),
                    task_id=effective_task_id,
                    run_id=effective_run_id,
                    event_id=saved_ev.event_id,
                    severity=target_severity,
                    notification_type="RESOURCE_WARNING",
                    created_at=timestamp,
                )
            )
            inbox_id: str | None = None
            master_notified = False
            req_approval = False
            if target_severity == "CRITICAL":
                inbox_id = f"inbox_{alert_id}"
                tx.insert_action_inbox_item(
                    ActionInboxItem(
                        inbox_item_id=inbox_id,
                        item_kind="APPROVAL_NEEDED"
                        if policy.requires_operator_approval_above_critical
                        else "ANOMALY_TRIAGE",
                        priority="CRITICAL",
                        status="OPEN",
                        summary=summary,
                        task_id=effective_task_id,
                        run_id=effective_run_id,
                        approval_id=policy.approval_id,
                        assigned_route=["core.master", "human_operator"],
                        created_at=timestamp,
                    ),
                    audit_event_id=saved_ev.event_id,
                )
                master_notified = True
                req_approval = policy.requires_operator_approval_above_critical

            new_alert = ResourceThresholdAlert(
                alert_id=alert_id,
                dedup_key=dedup_key,
                policy_id=policy.policy_id,
                alert_category=category,
                scope_kind=policy.scope_kind,
                scope_ref=policy.scope_ref,
                task_id=effective_task_id,
                run_id=effective_run_id,
                provider_id=policy.provider_id or (entry.provider_id if entry else None),
                connection_id=policy.connection_id or (entry.connection_id if entry else None),
                model_id=policy.model_id or (entry.model_id if entry else None),
                quota_bucket_key=policy.quota_bucket_key or (entry.quota_bucket_key if entry else None),
                severity=target_severity,
                status="OPEN",
                observed_value=observed_val,
                threshold_limit=limit_val,
                utilization_ratio=ratio,
                is_estimated_basis=is_est_basis,
                estimate_label=est_label,
                summary=summary,
                recommended_action=recommended_action,
                occurrence_count=1,
                notification_id=saved_notif.notification_id,
                action_inbox_item_id=inbox_id,
                master_notified=master_notified,
                requires_operator_approval=req_approval,
                audit_event_id=saved_ev.event_id,
                record_version=1,
                created_at=timestamp,
                updated_at=timestamp,
            )
            persisted_alert = tx.insert_alert(new_alert)
            outcomes.append(
                EvaluatedAlertOutcome(
                    alert=persisted_alert,
                    newly_created=True,
                    escalated_to_critical=(target_severity == "CRITICAL"),
                    notification_suppressed=False,
                )
            )
        else:
            escalating = active_alert.severity == "WARNING" and target_severity == "CRITICAL"
            new_notif_id: str | None = None
            new_inbox_id: str | None = None
            new_audit_id = trigger_event_id
            master_notified = active_alert.master_notified
            req_approval = active_alert.requires_operator_approval

            if escalating:
                esc_ev = _make_resources_event(
                    event_type=ev_type,
                    auth_context=auth_context,
                    timestamp=timestamp,
                    correlation_id=f"res-alert-{active_alert.alert_id}",
                    task_id=effective_task_id,
                    run_id=effective_run_id,
                    payload={
                        "department": "resources",
                        "alert_id": active_alert.alert_id,
                        "policy_id": policy.policy_id,
                        "alert_category": category,
                        "escalated_from": active_alert.severity,
                        "severity": "CRITICAL",
                        "observed_value": observed_val,
                        "threshold_limit": limit_val,
                        "utilization_ratio": ratio,
                    },
                )
                saved_esc_ev = tx.append_event(esc_ev)
                new_audit_id = saved_esc_ev.event_id
                saved_notif = tx.save_notification(
                    NotificationRef(
                        notification_id=new_notification_id(),
                        task_id=effective_task_id,
                        run_id=effective_run_id,
                        event_id=saved_esc_ev.event_id,
                        severity="CRITICAL",
                        notification_type="RESOURCE_WARNING",
                        created_at=timestamp,
                    )
                )
                new_notif_id = saved_notif.notification_id
                if active_alert.action_inbox_item_id is None:
                    new_inbox_id = f"inbox_{active_alert.alert_id}"
                    tx.insert_action_inbox_item(
                        ActionInboxItem(
                            inbox_item_id=new_inbox_id,
                            item_kind="APPROVAL_NEEDED"
                            if policy.requires_operator_approval_above_critical
                            else "ANOMALY_TRIAGE",
                            priority="CRITICAL",
                            status="OPEN",
                            summary=summary,
                            task_id=effective_task_id,
                            run_id=effective_run_id,
                            approval_id=policy.approval_id,
                            assigned_route=["core.master", "human_operator"],
                            created_at=timestamp,
                        ),
                        audit_event_id=saved_esc_ev.event_id,
                    )
                master_notified = True
                req_approval = policy.requires_operator_approval_above_critical

            updated_alert = tx.update_alert_observation(
                alert_id=active_alert.alert_id,
                expected_record_version=active_alert.record_version,
                severity="CRITICAL" if (escalating or active_alert.severity == "CRITICAL") else "WARNING",
                observed_value=observed_val,
                threshold_limit=limit_val,
                utilization_ratio=ratio,
                is_estimated_basis=is_est_basis,
                estimate_label=est_label,
                summary=summary,
                recommended_action=recommended_action,
                occurrence_count=active_alert.occurrence_count + 1,
                notification_id=new_notif_id,
                action_inbox_item_id=new_inbox_id,
                master_notified=master_notified,
                requires_operator_approval=req_approval,
                audit_event_id=new_audit_id,
                updated_at=timestamp,
            )
            outcomes.append(
                EvaluatedAlertOutcome(
                    alert=updated_alert,
                    newly_created=False,
                    escalated_to_critical=escalating,
                    notification_suppressed=not escalating,
                )
            )

    return outcomes, recovered_ids


def register_budget_policy(
    pm: PermissionManager,
    ledger_repository: ResourceLedgerRepository | None,
    request: RegisterBudgetPolicyRequest,
    *,
    auth_context: TrustedAuthContext,
) -> ResourceBudgetPolicy:
    """Register or update a durable budget/quota threshold policy (Operator or Master only)."""
    repo = _require_repo(ledger_repository)
    if auth_context.actor_kind not in ("human_operator", "master_agent"):
        raise ResourceLedgerGovernanceError(
            "POLICY_MANAGEMENT_DENIED",
            f"Actor '{auth_context.actor_id}' ({auth_context.actor_kind}) cannot manage resource budget policies.",
        )

    authorize_and_audit(
        pm,
        auth_context=auth_context,
        action="connection.read",
        resource=f"resources:policy:{request.scope_kind}:{request.scope_ref}",
        approval_id=request.approval_id,
        timestamp=request.timestamp,
        correlation_id=f"res-pol-{request.policy_id}",
    )

    bucket_key: str | None = None
    effective_scope = request.quota_scope_kind
    proven = False
    if request.provider_id is not None:
        bucket_key, effective_scope, proven = derive_quota_bucket_key(
            provider_id=request.provider_id,
            connection_id=request.connection_id,
            project_ref=request.project_ref,
            account_ref=request.account_ref,
            quota_scope_kind=request.quota_scope_kind,
            independent_quota_proof=request.independent_quota_proof,
        )

    def _tx_op(tx: ResourceLedgerTransactionContext) -> ResourceBudgetPolicy:
        existing = tx.get_policy(request.scope_kind, request.scope_ref)
        if request.approval_id is not None and not tx.is_approval_approved(request.approval_id):
            raise ResourceLedgerReferenceError(
                "APPROVAL_NOT_APPROVED",
                f"Approval '{request.approval_id}' does not exist or is not APPROVED.",
            )
        policy = ResourceBudgetPolicy(
            policy_id=existing.policy_id if existing is not None else request.policy_id,
            scope_kind=request.scope_kind,
            scope_ref=request.scope_ref,
            provider_id=request.provider_id,
            connection_id=request.connection_id,
            model_id=request.model_id,
            project_ref=request.project_ref,
            account_ref=request.account_ref,
            quota_scope_kind=effective_scope,
            quota_bucket_key=bucket_key,
            independent_quota_proof=proven,
            budget_cost_usd_limit=request.budget_cost_usd_limit,
            duration_ms_limit=request.duration_ms_limit,
            memory_mb_limit=request.memory_mb_limit,
            total_tokens_limit=request.total_tokens_limit,
            quota_units_limit=request.quota_units_limit,
            warning_ratio=request.warning_ratio,
            critical_ratio=request.critical_ratio,
            requires_operator_approval_above_critical=request.requires_operator_approval_above_critical,
            approval_id=request.approval_id,
            policy_note=request.policy_note,
            enabled=request.enabled,
            record_version=(existing.record_version + 1) if existing is not None else 1,
            created_by=existing.created_by if existing is not None else auth_context.actor_id,
            created_at=existing.created_at if existing is not None else request.timestamp,
            updated_at=request.timestamp,
        )
        saved = tx.upsert_policy(policy)
        ev = _make_resources_event(
            event_type="resources.policy_registered",
            auth_context=auth_context,
            timestamp=request.timestamp,
            correlation_id=f"res-pol-{saved.policy_id}",
            payload={
                "department": "resources",
                "policy_id": saved.policy_id,
                "scope_kind": saved.scope_kind,
                "scope_ref": saved.scope_ref,
                "provider_id": saved.provider_id,
                "connection_id": saved.connection_id,
                "model_id": saved.model_id,
                "quota_bucket_key": saved.quota_bucket_key,
                "quota_independence_proven": saved.independent_quota_proof,
                "warning_ratio": saved.warning_ratio,
                "critical_ratio": saved.critical_ratio,
                "enabled": saved.enabled,
            },
        )
        saved_ev = tx.append_event(ev)
        if saved.enabled:
            _evaluate_policy_thresholds(
                tx,
                saved,
                entry=None,
                trigger_event_id=saved_ev.event_id,
                task_id_hint=saved.scope_ref if saved.scope_kind == "TASK" else None,
                run_id_hint=None,
                auth_context=auth_context,
                timestamp=request.timestamp,
            )
        return saved

    return repo.execute_transaction(_tx_op)


def record_usage(
    pm: PermissionManager,
    ledger_repository: ResourceLedgerRepository | None,
    request: RecordResourceUsageRequest,
    *,
    auth_context: TrustedAuthContext,
) -> RecordResourceUsageOutcome:
    """Append an immutable resource usage entry and atomically evaluate budget & quota alerts."""
    repo = _require_repo(ledger_repository)
    if auth_context.actor_kind == "worker_agent":
        if request.agent_id is not None and request.agent_id != auth_context.actor_id:
            raise ResourceLedgerGovernanceError(
                "ACTOR_IDENTITY_MISMATCH",
                f"Worker '{auth_context.actor_id}' cannot record resource usage under agent '{request.agent_id}'.",
            )
        if request.task_id not in auth_context.assigned_task_ids:
            raise ResourceLedgerGovernanceError(
                "TASK_OWNERSHIP_DENIED",
                f"Worker '{auth_context.actor_id}' is not assigned to Task '{request.task_id}'.",
            )

    authorize_and_audit(
        pm,
        auth_context=auth_context,
        action="task.read",
        resource=f"resources:ledger:{request.task_id}",
        task_id=request.task_id,
        run_id=request.run_id,
        timestamp=request.timestamp,
        correlation_id=f"res-led-{request.ledger_entry_id}",
    )

    bucket_key, effective_scope, proven = derive_quota_bucket_key(
        provider_id=request.provider_id,
        connection_id=request.connection_id,
        project_ref=request.project_ref,
        account_ref=request.account_ref,
        quota_scope_kind=request.quota_scope_kind,
        independent_quota_proof=request.independent_quota_proof,
    )
    fingerprint = _compute_usage_fingerprint(request, bucket_key)
    telemetry_status = _resolve_telemetry_status(request)

    def _tx_op(tx: ResourceLedgerTransactionContext) -> RecordResourceUsageOutcome:
        task = tx.get_task(request.task_id)
        if task is None:
            raise ResourceLedgerReferenceError(
                "TASK_NOT_FOUND",
                f"Task '{request.task_id}' does not exist.",
            )
        if request.run_id is not None:
            run = tx.get_run(request.run_id)
            if run is None or run.task_id != request.task_id:
                raise ResourceLedgerReferenceError(
                    "RUN_NOT_FOUND",
                    f"Run '{request.run_id}' does not exist for Task '{request.task_id}'.",
                )

        if request.idempotency_key is not None:
            existing = tx.find_ledger_by_idempotency(
                request.task_id,
                request.run_id,
                request.idempotency_key,
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise ResourceLedgerConflictError(
                        "IDEMPOTENCY_PAYLOAD_MISMATCH",
                        (
                            f"Idempotency key '{request.idempotency_key}' already recorded "
                            "with a different resource usage payload."
                        ),
                    )
                return RecordResourceUsageOutcome(
                    entry=existing,
                    idempotent_replay=True,
                    triggered_alerts=[],
                    recovered_alert_ids=[],
                )

        ev = _make_resources_event(
            event_type="resources.ledger_recorded",
            auth_context=auth_context,
            timestamp=request.timestamp,
            correlation_id=f"res-led-{request.ledger_entry_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            payload={
                "department": "resources",
                "ledger_entry_id": request.ledger_entry_id,
                "provider_id": request.provider_id,
                "connection_id": request.connection_id,
                "model_id": request.model_id,
                "quota_bucket_key": bucket_key,
                "quota_independence_proven": proven,
                "duration_source": request.duration_source,
                "memory_source": request.memory_source,
                "token_usage_status": request.token_usage_status,
                "cost_source": request.cost_source,
                "telemetry_status": telemetry_status,
                "is_estimated": request.is_estimated,
                "actual_cost_usd": request.actual_cost_usd,
                "estimated_cost_usd": request.estimated_cost_usd,
            },
        )
        saved_ev = tx.append_event(ev)

        entry = ResourceLedgerEntry(
            ledger_entry_id=request.ledger_entry_id,
            task_id=request.task_id,
            run_id=request.run_id,
            project_id=request.project_id if request.project_id is not None else task.project_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            provider_id=request.provider_id,
            connection_id=request.connection_id,
            model_id=request.model_id,
            project_ref=request.project_ref,
            account_ref=request.account_ref,
            quota_scope_kind=effective_scope,
            quota_bucket_key=bucket_key,
            quota_independence_proven=proven,
            duration_ms=request.duration_ms,
            provider_duration_ms=request.provider_duration_ms,
            duration_source=request.duration_source,
            memory_mb=request.memory_mb,
            peak_memory_mb=request.peak_memory_mb,
            memory_source=request.memory_source,
            input_tokens=request.input_tokens,
            output_tokens=request.output_tokens,
            thinking_tokens=request.thinking_tokens,
            cached_tokens=request.cached_tokens,
            total_tokens=request.total_tokens,
            token_usage_status=request.token_usage_status,
            actual_cost_usd=request.actual_cost_usd,
            estimated_cost_usd=request.estimated_cost_usd,
            cost_source=request.cost_source,
            pricing_table_ref=request.pricing_table_ref,
            telemetry_status=telemetry_status,  # type: ignore[arg-type]
            is_estimated=request.is_estimated,
            estimate_label=request.estimate_label,
            quota_units_used=request.quota_units_used,
            quota_limit_units=request.quota_limit_units,
            quota_remaining_units=request.quota_remaining_units,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            note=request.note,
            metadata=request.metadata,
            recorded_by=auth_context.actor_id,
            audit_event_id=saved_ev.event_id,
            recorded_at=request.timestamp,
        )
        persisted = tx.insert_ledger_entry(entry)

        all_alerts: list[EvaluatedAlertOutcome] = []
        all_recovered: list[str] = []
        for policy in tx.list_matching_enabled_policies(persisted):
            alerts, recovered = _evaluate_policy_thresholds(
                tx,
                policy,
                entry=persisted,
                trigger_event_id=saved_ev.event_id,
                task_id_hint=persisted.task_id,
                run_id_hint=persisted.run_id,
                auth_context=auth_context,
                timestamp=request.timestamp,
            )
            all_alerts.extend(alerts)
            all_recovered.extend(recovered)

        return RecordResourceUsageOutcome(
            entry=persisted,
            idempotent_replay=False,
            triggered_alerts=all_alerts,
            recovered_alert_ids=all_recovered,
        )

    return repo.execute_transaction(_tx_op)


def get_task_resource_summary(
    pm: PermissionManager,
    ledger_repository: ResourceLedgerRepository | None,
    task_id: str,
    *,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
) -> TaskResourceUsageSummary:
    """Return honest aggregated resource usage for one Task preserving None for missing metrics."""
    repo = _require_repo(ledger_repository)
    if auth_context.actor_kind == "worker_agent" and task_id not in auth_context.assigned_task_ids:
        raise ResourceLedgerGovernanceError(
            "TASK_OWNERSHIP_DENIED",
            f"Worker '{auth_context.actor_id}' is not assigned to Task '{task_id}'.",
        )
    authorize_and_audit(
        pm,
        auth_context=auth_context,
        action="task.read",
        resource=f"resources:task:{task_id}",
        task_id=task_id,
        timestamp=timestamp,
        correlation_id=f"res-sum-{task_id}",
    )
    summary = repo.summarize_task(task_id)
    if summary is None:
        raise ResourceLedgerReferenceError("TASK_NOT_FOUND", f"Task '{task_id}' does not exist.")
    return summary


def get_bucket_quota_summary(
    pm: PermissionManager,
    ledger_repository: ResourceLedgerRepository | None,
    quota_bucket_key: str,
    *,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
) -> BucketQuotaSummary | None:
    """Return quota bucket consumption and independence proof status."""
    repo = _require_repo(ledger_repository)
    authorize_and_audit(
        pm,
        auth_context=auth_context,
        action="connection.read",
        resource=f"resources:bucket:{quota_bucket_key}",
        timestamp=timestamp,
        correlation_id=f"res-bucket-{quota_bucket_key}",
    )
    return repo.get_bucket_summary(quota_bucket_key)


def list_resource_ledger(
    pm: PermissionManager,
    ledger_repository: ResourceLedgerRepository | None,
    query: ResourceLedgerQuery | None = None,
    *,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
) -> list[ResourceLedgerEntry]:
    """List durable resource ledger entries filtered by task/provider/connection/model/bucket."""
    repo = _require_repo(ledger_repository)
    q = query or ResourceLedgerQuery()
    if auth_context.actor_kind == "worker_agent":
        if q.task_id is None or q.task_id not in auth_context.assigned_task_ids:
            raise ResourceLedgerGovernanceError(
                "TASK_OWNERSHIP_DENIED",
                "Workers may only list resource ledger entries for their assigned task_id.",
            )
    authorize_and_audit(
        pm,
        auth_context=auth_context,
        action="task.read",
        resource=f"resources:ledger:{q.task_id or 'query'}",
        task_id=q.task_id,
        run_id=q.run_id,
        timestamp=timestamp,
        correlation_id=f"res-list-{q.task_id or 'all'}",
    )
    return repo.list_ledger_entries(q)


def list_resource_alerts(
    pm: PermissionManager,
    ledger_repository: ResourceLedgerRepository | None,
    *,
    scope_ref: str | None = None,
    include_resolved: bool = False,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
) -> list[ResourceThresholdAlert]:
    """List persisted budget and quota threshold alerts."""
    repo = _require_repo(ledger_repository)
    authorize_and_audit(
        pm,
        auth_context=auth_context,
        action="task.read",
        resource=f"resources:alerts:{scope_ref or 'all'}",
        timestamp=timestamp,
        correlation_id=f"res-alerts-{scope_ref or 'all'}",
    )
    return repo.list_alerts(scope_ref=scope_ref, include_resolved=include_resolved)


def acknowledge_resource_alert(
    pm: PermissionManager,
    ledger_repository: ResourceLedgerRepository | None,
    alert_id: str,
    *,
    expected_record_version: int,
    note: str,
    timestamp: datetime,
    auth_context: TrustedAuthContext,
) -> ResourceThresholdAlert:
    """Acknowledge an open resource threshold alert (Master or Human Operator only)."""
    repo = _require_repo(ledger_repository)
    if auth_context.actor_kind not in ("human_operator", "master_agent"):
        raise ResourceLedgerGovernanceError(
            "ALERT_MANAGEMENT_DENIED",
            f"Actor '{auth_context.actor_id}' ({auth_context.actor_kind}) cannot acknowledge resource alerts.",
        )
    cleaned_note = scrub_secret_text(note.strip()).strip()
    if not cleaned_note:
        raise ResourceLedgerValidationError("EMPTY_NOTE", "Acknowledgement note must not be blank.")

    authorize_and_audit(
        pm,
        auth_context=auth_context,
        action="connection.read",
        resource=f"resources:alert:{alert_id}:ack",
        timestamp=timestamp,
        correlation_id=f"res-ack-{alert_id}",
    )

    def _tx_op(tx: ResourceLedgerTransactionContext) -> ResourceThresholdAlert:
        current = tx.get_alert_for_update(alert_id)
        if current is None:
            raise ResourceLedgerReferenceError("ALERT_NOT_FOUND", f"Alert '{alert_id}' does not exist.")
        if current.record_version != expected_record_version:
            raise ResourceLedgerConflictError(
                "STALE_ALERT_VERSION",
                f"Expected record_version={expected_record_version} for '{alert_id}', found {current.record_version}.",
            )
        if current.status in ("RESOLVED", "DISMISSED"):
            raise ResourceLedgerValidationError(
                "ALERT_ALREADY_CLOSED",
                f"Cannot acknowledge closed alert '{alert_id}' in status '{current.status}'.",
            )
        ev = _make_resources_event(
            event_type="resources.alert_acknowledged",
            auth_context=auth_context,
            timestamp=timestamp,
            correlation_id=f"res-ack-{alert_id}",
            task_id=current.task_id,
            run_id=current.run_id,
            payload={
                "department": "resources",
                "alert_id": alert_id,
                "previous_status": current.status,
                "note": cleaned_note,
            },
        )
        saved_ev = tx.append_event(ev)
        updated = tx.mark_alert_acknowledged(
            alert_id=alert_id,
            expected_record_version=expected_record_version,
            acknowledged_by=auth_context.actor_id,
            acknowledged_at=timestamp,
            note=cleaned_note,
            audit_event_id=saved_ev.event_id,
        )
        if updated.action_inbox_item_id is not None:
            tx.update_action_inbox_status(
                updated.action_inbox_item_id,
                status="ACKNOWLEDGED",
                updated_at=timestamp,
            )
        return updated

    return repo.execute_transaction(_tx_op)


def resolve_resource_alert(
    pm: PermissionManager,
    ledger_repository: ResourceLedgerRepository | None,
    alert_id: str,
    *,
    expected_record_version: int,
    note: str,
    timestamp: datetime,
    auth_context: TrustedAuthContext,
) -> ResourceThresholdAlert:
    """Resolve a resource threshold alert cleanly after operator/Master mitigation."""
    repo = _require_repo(ledger_repository)
    if auth_context.actor_kind not in ("human_operator", "master_agent"):
        raise ResourceLedgerGovernanceError(
            "ALERT_MANAGEMENT_DENIED",
            f"Actor '{auth_context.actor_id}' ({auth_context.actor_kind}) cannot resolve resource alerts.",
        )
    cleaned_note = scrub_secret_text(note.strip()).strip()
    if not cleaned_note:
        raise ResourceLedgerValidationError("EMPTY_NOTE", "Resolution note must not be blank.")

    authorize_and_audit(
        pm,
        auth_context=auth_context,
        action="connection.read",
        resource=f"resources:alert:{alert_id}:resolve",
        timestamp=timestamp,
        correlation_id=f"res-res-{alert_id}",
    )

    def _tx_op(tx: ResourceLedgerTransactionContext) -> ResourceThresholdAlert:
        current = tx.get_alert_for_update(alert_id)
        if current is None:
            raise ResourceLedgerReferenceError("ALERT_NOT_FOUND", f"Alert '{alert_id}' does not exist.")
        if current.record_version != expected_record_version:
            raise ResourceLedgerConflictError(
                "STALE_ALERT_VERSION",
                f"Expected record_version={expected_record_version} for '{alert_id}', found {current.record_version}.",
            )
        if current.status in ("RESOLVED", "DISMISSED"):
            raise ResourceLedgerValidationError(
                "ALERT_ALREADY_CLOSED",
                f"Alert '{alert_id}' is already in terminal status '{current.status}'.",
            )
        ev = _make_resources_event(
            event_type="resources.alert_resolved",
            auth_context=auth_context,
            timestamp=timestamp,
            correlation_id=f"res-res-{alert_id}",
            task_id=current.task_id,
            run_id=current.run_id,
            payload={
                "department": "resources",
                "alert_id": alert_id,
                "previous_status": current.status,
                "note": cleaned_note,
            },
        )
        saved_ev = tx.append_event(ev)
        updated = tx.mark_alert_resolved(
            alert_id=alert_id,
            expected_record_version=expected_record_version,
            resolved_by=auth_context.actor_id,
            resolved_at=timestamp,
            note=cleaned_note,
            audit_event_id=saved_ev.event_id,
        )
        if updated.action_inbox_item_id is not None:
            tx.update_action_inbox_status(
                updated.action_inbox_item_id,
                status="RESOLVED",
                updated_at=timestamp,
            )
        return updated

    return repo.execute_transaction(_tx_op)
