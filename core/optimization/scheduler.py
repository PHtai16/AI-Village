"""Phase 11 / P11.03 Execution Scheduler: concurrency, rate limit, quota buckets, leases & schedules."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import uuid

from core.contracts.event import Event, EventType, sanitize_event_payload
from core.contracts.identifiers import (
    new_event_id,
    new_lease_id,
    validate_aware_datetime,
)
from core.contracts.task import TaskPriority
from core.departments._common import authorize_and_audit, event_actor_id
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source
from core.optimization.scheduler_models import (
    ActiveExecutionItem,
    ActiveExecutionSnapshot,
    ConnectionQuotaPolicy,
    ConnectionSwitchEvaluationRequest,
    EnqueueTaskRequest,
    ExecutionLease,
    ExecutionReleaseRequest,
    ExecutionReservationResult,
    ExecutionStartConfirmRequest,
    KeyRotationForbiddenError,
    QueueEntry,
    QueueVisibilitySnapshot,
    QuotaBucketState,
    QuotaScopeKind,
    RegisterQuotaPolicyRequest,
    RegisterScheduledTaskRequest,
    ScheduledTaskMetadata,
    ScheduleEvaluationOutcome,
    ScheduleStatus,
    SchedulerConflictError,
    SchedulerDecision,
    SchedulerDecisionOutcome,
    SchedulerGovernanceError,
    SchedulerPersistenceError,
    SchedulerReferenceError,
    SchedulerRepository,
    SchedulerStateError,
    SchedulerTransactionContext,
    SchedulerValidationError,
)
from core.tasks.fallback import FallbackDecision, FallbackEvaluationRequest, evaluate_fallback
from core.tasks.transitions import TERMINAL_RUN_STATUSES, TERMINAL_TASK_STATUSES

_PRIORITY_BASE_RANK: dict[TaskPriority, int] = {
    "URGENT": 1,
    "HIGH": 2,
    "NORMAL": 3,
    "LOW": 4,
}


def compute_quota_bucket_key(
    *,
    provider_id: str,
    connection_id: str,
    project_ref: str | None,
    account_ref: str | None,
    quota_scope_kind: QuotaScopeKind,
    independent_quota_proof: bool,
) -> tuple[str, QuotaScopeKind]:
    """Resolve canonical shared quota bucket key without guessing key independence."""
    if quota_scope_kind == "CONNECTION_VERIFIED" and independent_quota_proof:
        return f"conn:{provider_id}:{connection_id}", "CONNECTION_VERIFIED"
    if project_ref is not None:
        return f"project:{provider_id}:{project_ref}", "PROJECT_SHARED"
    if account_ref is not None:
        return f"account:{provider_id}:{account_ref}", "ACCOUNT_SHARED"
    if quota_scope_kind == "ORG_SHARED":
        return f"org:{provider_id}", "ORG_SHARED"
    # Unproven scope across same-provider keys defaults strictly to shared bucket!
    return f"unproven_shared:{provider_id}", "UNKNOWN_SHARED"


def _make_scheduler_event(
    *,
    event_type: EventType,
    auth_context: TrustedAuthContext,
    timestamp: datetime,
    correlation_id: str,
    payload: dict[str, object],
    task_id: str | None = None,
    run_id: str | None = None,
    provider_id: str | None = None,
    connection_id: str | None = None,
    model_id: str | None = None,
) -> Event:
    """Build secret-safe domain Event for Scheduler state changes."""
    return Event(
        event_id=new_event_id(),
        timestamp=timestamp,
        event_type=event_type,
        source=actor_kind_to_event_source(auth_context.actor_kind),
        task_id=task_id,
        run_id=run_id,
        actor_id=event_actor_id(auth_context),
        provider_id=provider_id,
        connection_id=connection_id,
        model_id=model_id,
        correlation_id=correlation_id,
        redacted_payload=sanitize_event_payload(payload),
    )


def _enqueue_fingerprint(req: EnqueueTaskRequest) -> str:
    """Compute canonical SHA-256 fingerprint for idempotent queue insertion."""
    canonical = {
        "task_id": req.task_id,
        "run_id": req.run_id,
        "schedule_id": req.schedule_id,
        "provider_id": req.provider_id,
        "connection_id": req.connection_id,
        "model_id": req.model_id,
        "requested_quota_units": req.requested_quota_units,
        "assigned_worker_id": req.assigned_worker_id,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _map_governed_auth(
    pm: PermissionManager,
    *,
    auth_context: TrustedAuthContext,
    action: str,
    resource: str,
    timestamp: datetime,
    task_id: str | None = None,
    run_id: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Enforce Governance permission and translate denials cleanly to ``SchedulerGovernanceError``."""
    try:
        authorize_and_audit(
            pm,
            auth_context=auth_context,
            action=action,
            resource=resource,
            task_id=task_id,
            run_id=run_id,
            timestamp=timestamp,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        err_code = getattr(exc, "error_code", "GOVERNANCE_DENIED")
        err_msg = getattr(exc, "message", str(exc))
        raise SchedulerGovernanceError(err_code, err_msg) from exc


class ExecutionSchedulerService:
    """Durable priority/fairness Scheduler enforcing concurrency, sliding-window rate, and quota limits."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        scheduler_repository: SchedulerRepository | None = None,
    ) -> None:
        """Bind Scheduler coordinator to Governance and persistent SchedulerRepository."""
        self._pm = permission_manager
        self._repo = scheduler_repository

    def _require_repo(self) -> SchedulerRepository:
        if self._repo is None:
            raise SchedulerPersistenceError(
                "SCHEDULER_REPOSITORY_UNAVAILABLE",
                "Durable SchedulerRepository is required for P11.03 scheduler operations.",
            )
        return self._repo

    def register_quota_policy(
        self,
        request: RegisterQuotaPolicyRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> ConnectionQuotaPolicy:
        """Register or update concurrency, rate-limit, and quota limits mapped to a shared bucket."""
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.transition",
            resource=f"scheduler:policy:{request.provider_id}:{request.connection_id}",
            timestamp=request.timestamp,
            correlation_id=f"sched-pol-{request.connection_id}",
        )
        bucket_key, normalized_scope = compute_quota_bucket_key(
            provider_id=request.provider_id,
            connection_id=request.connection_id,
            project_ref=request.project_ref,
            account_ref=request.account_ref,
            quota_scope_kind=request.quota_scope_kind,
            independent_quota_proof=request.independent_quota_proof,
        )
        policy_id = f"{request.provider_id}:{request.connection_id}"

        def _tx_op(tx: SchedulerTransactionContext) -> ConnectionQuotaPolicy:
            existing_bucket = tx.get_bucket(bucket_key)
            existing_pol = tx.get_policy(request.provider_id, request.connection_id)
            used = existing_bucket.quota_used_units if existing_bucket is not None else 0
            # Keep strictest shared bucket limits when multiple connections join one shared bucket!
            if existing_bucket is not None and existing_pol is None:
                eff_conc = min(existing_bucket.max_concurrency, request.max_concurrency)
                if existing_bucket.quota_limit_units is not None and request.quota_limit_units is not None:
                    eff_quota = min(existing_bucket.quota_limit_units, request.quota_limit_units)
                else:
                    eff_quota = existing_bucket.quota_limit_units or request.quota_limit_units
            else:
                eff_conc = request.max_concurrency
                eff_quota = request.quota_limit_units

            bucket = QuotaBucketState(
                quota_bucket_key=bucket_key,
                provider_id=request.provider_id,
                quota_scope_kind=normalized_scope,
                max_concurrency=eff_conc,
                rate_limit_max_requests=request.rate_limit_max_requests,
                rate_limit_window_seconds=request.rate_limit_window_seconds,
                quota_limit_units=eff_quota,
                quota_used_units=used,
                base_backoff_seconds=request.base_backoff_seconds,
                max_backoff_seconds=request.max_backoff_seconds,
                max_retries=request.max_retries,
                record_version=(existing_bucket.record_version + 1) if existing_bucket else 1,
                updated_at=request.timestamp,
            )
            policy = ConnectionQuotaPolicy(
                policy_id=policy_id,
                provider_id=request.provider_id,
                connection_id=request.connection_id,
                project_ref=request.project_ref,
                account_ref=request.account_ref,
                model_id=request.model_id,
                quota_scope_kind=normalized_scope,
                quota_bucket_key=bucket_key,
                independent_quota_proof=normalized_scope == "CONNECTION_VERIFIED",
                max_concurrency=request.max_concurrency,
                rate_limit_max_requests=request.rate_limit_max_requests,
                rate_limit_window_seconds=request.rate_limit_window_seconds,
                quota_limit_units=request.quota_limit_units,
                base_backoff_seconds=request.base_backoff_seconds,
                max_backoff_seconds=request.max_backoff_seconds,
                max_retries=request.max_retries,
                enabled=request.enabled,
                record_version=(existing_pol.record_version + 1) if existing_pol else 1,
                created_at=existing_pol.created_at if existing_pol else request.timestamp,
                updated_at=request.timestamp,
            )
            return tx.upsert_bucket_and_policy(bucket, policy)

        return repo.execute_transaction(_tx_op)

    def enqueue_task(
        self,
        request: EnqueueTaskRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> QueueEntry:
        """Queue an eligible Task idempotently without ever running terminal or approval-held work."""
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.transition",
            resource=f"scheduler:queue:{request.task_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"sched-enq-{request.task_id}",
        )
        fingerprint = _enqueue_fingerprint(request)

        def _tx_op(tx: SchedulerTransactionContext) -> QueueEntry:
            if request.idempotency_key is not None:
                hit = tx.find_queue_by_idempotency(request.idempotency_key)
                if hit is not None:
                    if hit.request_fingerprint != fingerprint:
                        raise SchedulerConflictError(
                            "SCHEDULER_IDEMPOTENCY_CONFLICT",
                            f"idempotency_key '{request.idempotency_key}' reused with different payload.",
                        )
                    return hit

            policy = tx.get_policy(request.provider_id, request.connection_id)
            if policy is None:
                raise SchedulerReferenceError(
                    "QUOTA_POLICY_NOT_FOUND",
                    f"No quota policy registered for {request.provider_id}/{request.connection_id}.",
                )
            if not policy.enabled:
                raise SchedulerValidationError(
                    "CONNECTION_DISABLED",
                    f"Connection '{request.connection_id}' policy is disabled and cannot accept work.",
                )

            task = tx.get_task(request.task_id)
            if task is None:
                raise SchedulerReferenceError(
                    "TASK_NOT_FOUND",
                    f"Task '{request.task_id}' does not exist.",
                )
            if task.status in TERMINAL_TASK_STATUSES:
                raise SchedulerStateError(
                    "TERMINAL_TASK_CANNOT_SCHEDULE",
                    f"Terminal Task '{request.task_id}' ({task.status}) must never be scheduled again.",
                )

            if request.run_id is not None:
                run = tx.get_run(request.run_id)
                if run is None or run.task_id != request.task_id:
                    raise SchedulerReferenceError(
                        "RUN_MISMATCH_OR_NOT_FOUND",
                        f"Run '{request.run_id}' does not belong to Task '{request.task_id}'.",
                    )
                if run.status in TERMINAL_RUN_STATUSES:
                    raise SchedulerStateError(
                        "TERMINAL_RUN_CANNOT_SCHEDULE",
                        f"Terminal Run '{request.run_id}' ({run.status}) must never be re-run.",
                    )

            existing_active = tx.find_active_queue_for_task(request.task_id)
            if existing_active is not None:
                raise SchedulerConflictError(
                    "DUPLICATE_ACTIVE_QUEUE_ENTRY",
                    f"Task '{request.task_id}' already holds active queue entry '{existing_active.queue_entry_id}'.",
                )

            needs_clarification = tx.has_pending_clarification_intake(request.task_id)
            governed_hold = task.status in {"WAITING_INPUT", "WAITING_APPROVAL", "BLOCKED"} or needs_clarification
            initial_status = "BLOCKED_GOVERNANCE" if governed_hold else "READY"
            block_code = (
                "CLARIFICATION_REQUIRED"
                if needs_clarification
                else (f"TASK_{task.status}" if governed_hold else None)
            )
            block_msg = (
                "Held from dispatch until pending clarification or operator approval resolves."
                if governed_hold
                else None
            )

            eff_priority = request.priority or task.priority
            entry = QueueEntry(
                queue_entry_id=request.queue_entry_id,
                task_id=request.task_id,
                run_id=request.run_id,
                schedule_id=request.schedule_id,
                provider_id=request.provider_id,
                connection_id=request.connection_id,
                model_id=request.model_id or policy.model_id,
                quota_bucket_key=policy.quota_bucket_key,
                priority=eff_priority,
                priority_rank=_PRIORITY_BASE_RANK[eff_priority],
                fairness_aging_seconds=request.fairness_aging_seconds,
                requested_quota_units=request.requested_quota_units,
                status=initial_status,
                block_reason_code=block_code,
                block_reason=block_msg,
                idempotency_key=request.idempotency_key,
                request_fingerprint=fingerprint,
                requested_by=auth_context.actor_id,
                assigned_worker_id=request.assigned_worker_id,
                enqueued_at=request.timestamp,
                updated_at=request.timestamp,
            )
            ev = _make_scheduler_event(
                event_type="scheduler.task_enqueued",
                auth_context=auth_context,
                timestamp=request.timestamp,
                correlation_id=f"sched-enq-{entry.queue_entry_id}",
                task_id=entry.task_id,
                run_id=entry.run_id,
                provider_id=entry.provider_id,
                connection_id=entry.connection_id,
                model_id=entry.model_id,
                payload={
                    "queue_entry_id": entry.queue_entry_id,
                    "quota_bucket_key": entry.quota_bucket_key,
                    "priority": entry.priority,
                    "status": entry.status,
                    "block_reason_code": entry.block_reason_code,
                },
            )
            tx.append_event(ev)
            return tx.insert_queue_entry(entry)

        return repo.execute_transaction(_tx_op)

    def reserve_next_execution(
        self,
        *,
        worker_id: str,
        now: datetime,
        auth_context: TrustedAuthContext,
        lease_ttl_seconds: int = 120,
        connection_filter: str | None = None,
        expire_stale_first: bool = True,
    ) -> ExecutionReservationResult:
        """Atomically reserve at most one execution slot enforcing concurrency, rate, quota & fairness."""
        validate_aware_datetime(now, "now")
        if lease_ttl_seconds < 1 or lease_ttl_seconds > 86400:
            raise SchedulerValidationError(
                "INVALID_LEASE_TTL",
                f"lease_ttl_seconds must be within 1..86400, got {lease_ttl_seconds}.",
            )
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.transition",
            resource=f"scheduler:reserve:{worker_id}",
            timestamp=now,
            correlation_id=f"sched-res-{worker_id}",
        )

        def _tx_op(tx: SchedulerTransactionContext) -> ExecutionReservationResult:
            if expire_stale_first:
                expired_pairs = tx.expire_overdue_leases(now=now)
                for exp_lease, _ in expired_pairs:
                    tx.append_event(
                        _make_scheduler_event(
                            event_type="scheduler.lease_expired",
                            auth_context=auth_context,
                            timestamp=now,
                            correlation_id=f"sched-exp-{exp_lease.lease_id}",
                            task_id=exp_lease.task_id,
                            run_id=exp_lease.run_id,
                            provider_id=exp_lease.provider_id,
                            connection_id=exp_lease.connection_id,
                            payload={
                                "lease_id": exp_lease.lease_id,
                                "queue_entry_id": exp_lease.queue_entry_id,
                                "worker_id": exp_lease.worker_id,
                            },
                        )
                    )

            candidates = tx.list_candidate_queue_entries()
            eligible: list[tuple[int, datetime, str, QueueEntry]] = []
            blocked_entries: list[QueueEntry] = []
            primary_block_outcome: SchedulerDecisionOutcome | None = None
            primary_block_code = "NO_ELIGIBLE_WORK"
            primary_block_reason = "No eligible queued task is ready for reservation."
            primary_retry_after: int | None = None
            primary_next_retry_at: datetime | None = None
            primary_bucket_key: str | None = None

            for entry in candidates:
                if connection_filter is not None and entry.connection_id != connection_filter:
                    continue
                if entry.assigned_worker_id is not None and entry.assigned_worker_id != worker_id:
                    continue

                task = tx.get_task(entry.task_id)
                if task is None or task.status in TERMINAL_TASK_STATUSES:
                    tx.cancel_or_fail_queue_entry(
                        queue_entry_id=entry.queue_entry_id,
                        expected_record_version=entry.record_version,
                        status="CANCELLED",
                        reason_code="TASK_ALREADY_TERMINAL",
                        reason="Task reached terminal state before execution reservation.",
                        updated_at=now,
                    )
                    continue
                if entry.run_id is not None:
                    run = tx.get_run(entry.run_id)
                    if run is None or run.status in TERMINAL_RUN_STATUSES:
                        tx.cancel_or_fail_queue_entry(
                            queue_entry_id=entry.queue_entry_id,
                            expected_record_version=entry.record_version,
                            status="CANCELLED",
                            reason_code="RUN_ALREADY_TERMINAL",
                            reason="Linked Run reached terminal state before execution reservation.",
                            updated_at=now,
                        )
                        continue

                if task.status in {"WAITING_INPUT", "WAITING_APPROVAL", "BLOCKED"} or tx.has_pending_clarification_intake(task.task_id):
                    held = tx.update_queue_blocked(
                        queue_entry_id=entry.queue_entry_id,
                        expected_record_version=entry.record_version,
                        status="BLOCKED_GOVERNANCE",
                        block_reason_code="APPROVAL_OR_CLARIFICATION_REQUIRED",
                        block_reason="Execution withheld pending human approval or clarification.",
                        retry_count=entry.retry_count,
                        next_retry_at=None,
                        updated_at=now,
                    )
                    blocked_entries.append(held)
                    if primary_block_outcome is None:
                        primary_block_outcome = "BLOCKED_GOVERNANCE"
                        primary_block_code = "APPROVAL_OR_CLARIFICATION_REQUIRED"
                        primary_block_reason = held.block_reason or primary_block_reason
                        primary_bucket_key = held.quota_bucket_key
                    continue

                age_secs = max(0.0, (now - entry.enqueued_at).total_seconds())
                aging_bonus = int(age_secs // entry.fairness_aging_seconds)
                eff_rank = entry.priority_rank - aging_bonus
                eligible.append((eff_rank, entry.enqueued_at, entry.queue_entry_id, entry))

            eligible.sort(key=lambda item: (item[0], item[1], item[2]))

            for _, _, _, entry in eligible:
                policy = tx.get_policy(entry.provider_id, entry.connection_id)
                bucket = tx.get_bucket(entry.quota_bucket_key)
                if policy is None or bucket is None or not policy.enabled:
                    held = tx.update_queue_blocked(
                        queue_entry_id=entry.queue_entry_id,
                        expected_record_version=entry.record_version,
                        status="BLOCKED_GOVERNANCE",
                        block_reason_code="CONNECTION_DISABLED",
                        block_reason="Connection quota policy missing or disabled.",
                        retry_count=entry.retry_count,
                        next_retry_at=None,
                        updated_at=now,
                    )
                    blocked_entries.append(held)
                    continue

                # Respect active rate-limit backoff timestamp before re-evaluating sliding window.
                if entry.next_retry_at is not None and now < entry.next_retry_at:
                    wait_s = max(1, int((entry.next_retry_at - now).total_seconds()))
                    blocked_entries.append(entry)
                    if primary_block_outcome is None:
                        primary_block_outcome = "BLOCKED_BY_RATE_LIMIT"
                        primary_block_code = "RATE_LIMIT_BACKOFF_ACTIVE"
                        primary_block_reason = (
                            f"Waiting for rate-limit backoff until {entry.next_retry_at.isoformat()}."
                        )
                        primary_retry_after = wait_s
                        primary_next_retry_at = entry.next_retry_at
                        primary_bucket_key = bucket.quota_bucket_key
                    continue

                # 1. Quota check.
                if (
                    bucket.quota_limit_units is not None
                    and bucket.quota_used_units + entry.requested_quota_units > bucket.quota_limit_units
                ):
                    blocked = tx.update_queue_blocked(
                        queue_entry_id=entry.queue_entry_id,
                        expected_record_version=entry.record_version,
                        status="BLOCKED_BY_QUOTA",
                        block_reason_code="QUOTA_EXHAUSTED",
                        block_reason=(
                            f"Bucket '{bucket.quota_bucket_key}' reached quota limit "
                            f"({bucket.quota_used_units}/{bucket.quota_limit_units})."
                        ),
                        retry_count=entry.retry_count,
                        next_retry_at=None,
                        updated_at=now,
                    )
                    tx.append_event(
                        _make_scheduler_event(
                            event_type="scheduler.execution_blocked",
                            auth_context=auth_context,
                            timestamp=now,
                            correlation_id=f"sched-blk-{entry.queue_entry_id}",
                            task_id=entry.task_id,
                            run_id=entry.run_id,
                            provider_id=entry.provider_id,
                            connection_id=entry.connection_id,
                            payload={
                                "queue_entry_id": entry.queue_entry_id,
                                "quota_bucket_key": bucket.quota_bucket_key,
                                "reason_code": "QUOTA_EXHAUSTED",
                            },
                        )
                    )
                    blocked_entries.append(blocked)
                    primary_block_outcome = "BLOCKED_BY_QUOTA"
                    primary_block_code = "QUOTA_EXHAUSTED"
                    primary_block_reason = blocked.block_reason or "Quota exhausted."
                    primary_bucket_key = bucket.quota_bucket_key
                    continue

                # 2. Concurrency limit check.
                active_slots = tx.count_active_leases_for_bucket(bucket.quota_bucket_key)
                if active_slots >= bucket.max_concurrency:
                    blocked = tx.update_queue_blocked(
                        queue_entry_id=entry.queue_entry_id,
                        expected_record_version=entry.record_version,
                        status="READY",
                        block_reason_code="CONCURRENCY_LIMIT_REACHED",
                        block_reason=(
                            f"Bucket '{bucket.quota_bucket_key}' concurrency at capacity "
                            f"({active_slots}/{bucket.max_concurrency})."
                        ),
                        retry_count=entry.retry_count,
                        next_retry_at=None,
                        updated_at=now,
                    )
                    blocked_entries.append(blocked)
                    if primary_block_outcome in (None, "BLOCKED_GOVERNANCE"):
                        primary_block_outcome = "BLOCKED_BY_CONCURRENCY"
                        primary_block_code = "CONCURRENCY_LIMIT_REACHED"
                        primary_block_reason = blocked.block_reason or "Concurrency limit reached."
                        primary_bucket_key = bucket.quota_bucket_key
                    continue

                # 3. Sliding-window rate-limit check with bounded backoff and retry limit.
                if (
                    bucket.rate_limit_max_requests is not None
                    and bucket.rate_limit_window_seconds is not None
                ):
                    window_start = now - timedelta(seconds=bucket.rate_limit_window_seconds)
                    recent_cnt = tx.count_rate_events_since(bucket.quota_bucket_key, window_start)
                    if recent_cnt >= bucket.rate_limit_max_requests:
                        next_retry_num = entry.retry_count + 1
                        if next_retry_num > bucket.max_retries:
                            exhausted = tx.cancel_or_fail_queue_entry(
                                queue_entry_id=entry.queue_entry_id,
                                expected_record_version=entry.record_version,
                                status="FAILED",
                                reason_code="RATE_LIMIT_RETRY_BUDGET_EXHAUSTED",
                                reason=(
                                    f"Exceeded max rate-limit retries ({bucket.max_retries}) "
                                    f"for bucket '{bucket.quota_bucket_key}'."
                                ),
                                updated_at=now,
                            )
                            tx.append_event(
                                _make_scheduler_event(
                                    event_type="scheduler.execution_blocked",
                                    auth_context=auth_context,
                                    timestamp=now,
                                    correlation_id=f"sched-blk-{entry.queue_entry_id}",
                                    task_id=entry.task_id,
                                    run_id=entry.run_id,
                                    provider_id=entry.provider_id,
                                    connection_id=entry.connection_id,
                                    payload={
                                        "queue_entry_id": entry.queue_entry_id,
                                        "quota_bucket_key": bucket.quota_bucket_key,
                                        "reason_code": "RATE_LIMIT_RETRY_BUDGET_EXHAUSTED",
                                    },
                                )
                            )
                            blocked_entries.append(exhausted)
                            primary_block_outcome = "BLOCKED_BY_RATE_LIMIT"
                            primary_block_code = "RATE_LIMIT_RETRY_BUDGET_EXHAUSTED"
                            primary_block_reason = exhausted.block_reason or "Retry budget exhausted."
                            primary_bucket_key = bucket.quota_bucket_key
                            continue

                        delay_s = min(
                            bucket.max_backoff_seconds,
                            bucket.base_backoff_seconds * (2 ** (next_retry_num - 1)),
                        )
                        retry_at = now + timedelta(seconds=delay_s)
                        blocked = tx.update_queue_blocked(
                            queue_entry_id=entry.queue_entry_id,
                            expected_record_version=entry.record_version,
                            status="BLOCKED_BY_RATE_LIMIT",
                            block_reason_code="RATE_LIMIT_EXCEEDED",
                            block_reason=(
                                f"Sliding window rate limit ({recent_cnt}/{bucket.rate_limit_max_requests}) "
                                f"reached on '{bucket.quota_bucket_key}'; retry at {retry_at.isoformat()}."
                            ),
                            retry_count=next_retry_num,
                            next_retry_at=retry_at,
                            updated_at=now,
                        )
                        tx.append_event(
                            _make_scheduler_event(
                                event_type="scheduler.execution_blocked",
                                auth_context=auth_context,
                                timestamp=now,
                                correlation_id=f"sched-blk-{entry.queue_entry_id}",
                                task_id=entry.task_id,
                                run_id=entry.run_id,
                                provider_id=entry.provider_id,
                                connection_id=entry.connection_id,
                                payload={
                                    "queue_entry_id": entry.queue_entry_id,
                                    "quota_bucket_key": bucket.quota_bucket_key,
                                    "reason_code": "RATE_LIMIT_EXCEEDED",
                                    "retry_after_seconds": delay_s,
                                    "retry_count": next_retry_num,
                                },
                            )
                        )
                        blocked_entries.append(blocked)
                        primary_block_outcome = "BLOCKED_BY_RATE_LIMIT"
                        primary_block_code = "RATE_LIMIT_EXCEEDED"
                        primary_block_reason = blocked.block_reason or "Rate limit exceeded."
                        primary_retry_after = delay_s
                        primary_next_retry_at = retry_at
                        primary_bucket_key = bucket.quota_bucket_key
                        continue

                # 4. All policy checks passed -> RESERVE atomically!
                lease = ExecutionLease(
                    lease_id=new_lease_id(),
                    queue_entry_id=entry.queue_entry_id,
                    task_id=entry.task_id,
                    run_id=entry.run_id,
                    worker_id=worker_id,
                    owner_token=f"own_{uuid.uuid4().hex[:20]}",
                    provider_id=entry.provider_id,
                    connection_id=entry.connection_id,
                    model_id=entry.model_id,
                    quota_bucket_key=bucket.quota_bucket_key,
                    state="RESERVED",
                    quota_units_reserved=entry.requested_quota_units,
                    acquired_at=now,
                    expires_at=now + timedelta(seconds=lease_ttl_seconds),
                    record_version=1,
                )
                ev = _make_scheduler_event(
                    event_type="scheduler.execution_reserved",
                    auth_context=auth_context,
                    timestamp=now,
                    correlation_id=f"sched-res-{lease.lease_id}",
                    task_id=entry.task_id,
                    run_id=entry.run_id,
                    provider_id=entry.provider_id,
                    connection_id=entry.connection_id,
                    model_id=entry.model_id,
                    payload={
                        "lease_id": lease.lease_id,
                        "queue_entry_id": entry.queue_entry_id,
                        "worker_id": worker_id,
                        "quota_bucket_key": bucket.quota_bucket_key,
                        "expires_at": lease.expires_at.isoformat(),
                    },
                )
                tx.append_event(ev)
                updated_entry, saved_lease = tx.reserve_slot_atomically(
                    queue_entry_id=entry.queue_entry_id,
                    expected_queue_version=entry.record_version,
                    lease=lease,
                    new_bucket_used_units=bucket.quota_used_units + entry.requested_quota_units,
                    rate_event_id=f"rev_{uuid.uuid4().hex[:16]}",
                    now=now,
                )
                return ExecutionReservationResult(
                    decision=SchedulerDecision(
                        outcome="RESERVED",
                        reason_code="SLOT_RESERVED",
                        queue_entry_id=updated_entry.queue_entry_id,
                        task_id=updated_entry.task_id,
                        run_id=updated_entry.run_id,
                        quota_bucket_key=bucket.quota_bucket_key,
                        reason=f"Reserved execution slot '{saved_lease.lease_id}' for '{worker_id}'.",
                        audit_event_id=ev.event_id,
                    ),
                    lease=saved_lease,
                    queue_entry=updated_entry,
                    blocked_entries=blocked_entries,
                )

            return ExecutionReservationResult(
                decision=SchedulerDecision(
                    outcome=primary_block_outcome or "NO_ELIGIBLE_WORK",
                    reason_code=primary_block_code,
                    quota_bucket_key=primary_bucket_key,
                    retry_after_seconds=primary_retry_after,
                    next_retry_at=primary_next_retry_at,
                    reason=primary_block_reason,
                ),
                lease=None,
                queue_entry=None,
                blocked_entries=blocked_entries,
            )

        return repo.execute_transaction(_tx_op)

    def confirm_execution_started(
        self,
        request: ExecutionStartConfirmRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> ExecutionLease:
        """Promote lease RESERVED->ACTIVE and queue RUNNING ONLY after confirming Run.status=='RUNNING'."""
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.transition",
            resource=f"scheduler:start:{request.lease_id}",
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"sched-start-{request.lease_id}",
        )

        def _tx_op(tx: SchedulerTransactionContext) -> ExecutionLease:
            lease = tx.get_lease_for_update(request.lease_id)
            if lease is None:
                raise SchedulerReferenceError(
                    "LEASE_NOT_FOUND",
                    f"Execution lease '{request.lease_id}' does not exist.",
                )
            if lease.owner_token != request.owner_token:
                raise SchedulerGovernanceError(
                    "LEASE_OWNERSHIP_MISMATCH",
                    f"Caller owner_token does not own lease '{request.lease_id}'.",
                )
            if lease.state != "RESERVED":
                raise SchedulerStateError(
                    "INVALID_LEASE_STATE",
                    f"Lease '{request.lease_id}' must be RESERVED before starting, got '{lease.state}'.",
                )
            run = tx.get_run(request.run_id)
            if run is None or run.task_id != lease.task_id:
                raise SchedulerReferenceError(
                    "RUN_MISMATCH_OR_NOT_FOUND",
                    f"Run '{request.run_id}' does not belong to leased Task '{lease.task_id}'.",
                )
            if run.status != "RUNNING":
                raise SchedulerStateError(
                    "RUN_NOT_RUNNING_IN_RUNTIME",
                    (
                        f"Refusing to report RUNNING in Scheduler because Runtime Run '{run.run_id}' "
                        f"is in status '{run.status}'."
                    ),
                )
            tx.append_event(
                _make_scheduler_event(
                    event_type="scheduler.execution_started",
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                    correlation_id=f"sched-start-{lease.lease_id}",
                    task_id=lease.task_id,
                    run_id=request.run_id,
                    provider_id=lease.provider_id,
                    connection_id=lease.connection_id,
                    model_id=lease.model_id,
                    payload={"lease_id": lease.lease_id, "queue_entry_id": lease.queue_entry_id},
                )
            )
            _, updated_lease = tx.confirm_lease_started(
                lease_id=lease.lease_id,
                expected_lease_version=request.expected_record_version,
                run_id=request.run_id,
                started_at=request.timestamp,
            )
            return updated_lease

        return repo.execute_transaction(_tx_op)

    def release_execution(
        self,
        request: ExecutionReleaseRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> ExecutionLease:
        """Release an owned lease slot and update queue completion/requeue state atomically."""
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.transition",
            resource=f"scheduler:release:{request.lease_id}",
            timestamp=request.timestamp,
            correlation_id=f"sched-rel-{request.lease_id}",
        )

        def _tx_op(tx: SchedulerTransactionContext) -> ExecutionLease:
            lease = tx.get_lease_for_update(request.lease_id)
            if lease is None:
                raise SchedulerReferenceError(
                    "LEASE_NOT_FOUND",
                    f"Execution lease '{request.lease_id}' does not exist.",
                )
            if lease.owner_token != request.owner_token:
                raise SchedulerGovernanceError(
                    "LEASE_OWNERSHIP_MISMATCH",
                    f"Caller owner_token does not own lease '{request.lease_id}'.",
                )
            if lease.state not in {"RESERVED", "ACTIVE"}:
                raise SchedulerStateError(
                    "INVALID_LEASE_STATE",
                    f"Lease '{request.lease_id}' is already finalized ({lease.state}).",
                )
            tx.append_event(
                _make_scheduler_event(
                    event_type="scheduler.execution_released",
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                    correlation_id=f"sched-rel-{lease.lease_id}",
                    task_id=lease.task_id,
                    run_id=lease.run_id,
                    provider_id=lease.provider_id,
                    connection_id=lease.connection_id,
                    payload={
                        "lease_id": lease.lease_id,
                        "queue_entry_id": lease.queue_entry_id,
                        "terminal_queue_status": request.terminal_queue_status,
                        "reason": request.reason,
                    },
                )
            )
            _, released = tx.release_lease_and_queue(
                lease_id=lease.lease_id,
                expected_lease_version=lease.record_version,
                lease_state="RELEASED",
                queue_status=request.terminal_queue_status,
                reason=request.reason,
                released_at=request.timestamp,
            )
            return released

        return repo.execute_transaction(_tx_op)

    def recover_expired_leases(
        self,
        *,
        now: datetime,
        auth_context: TrustedAuthContext,
    ) -> list[ExecutionLease]:
        """Reclaim expired leases cleanly on timeout or process restart without losing queued work."""
        validate_aware_datetime(now, "now")
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.transition",
            resource="scheduler:leases:recover",
            timestamp=now,
            correlation_id="sched-lease-recover",
        )

        def _tx_op(tx: SchedulerTransactionContext) -> list[ExecutionLease]:
            expired = tx.expire_overdue_leases(now=now)
            out: list[ExecutionLease] = []
            for exp_lease, _ in expired:
                tx.append_event(
                    _make_scheduler_event(
                        event_type="scheduler.lease_expired",
                        auth_context=auth_context,
                        timestamp=now,
                        correlation_id=f"sched-exp-{exp_lease.lease_id}",
                        task_id=exp_lease.task_id,
                        run_id=exp_lease.run_id,
                        provider_id=exp_lease.provider_id,
                        connection_id=exp_lease.connection_id,
                        payload={
                            "lease_id": exp_lease.lease_id,
                            "queue_entry_id": exp_lease.queue_entry_id,
                            "worker_id": exp_lease.worker_id,
                        },
                    )
                )
                out.append(exp_lease)
            return out

        return repo.execute_transaction(_tx_op)

    def evaluate_connection_switch(
        self,
        request: ConnectionSwitchEvaluationRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> FallbackDecision:
        """Strictly forbid key/connection rotation to evade rate or quota limits."""
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="connection.read",
            resource=f"scheduler:switch:{request.current_target.connection_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"sched-sw-{request.run_id}",
        )

        def _tx_op(tx: SchedulerTransactionContext) -> FallbackDecision:
            src_pol = tx.get_policy(
                request.current_target.provider_id,
                request.current_target.connection_id,
            )
            dst_pol = tx.get_policy(
                request.candidate_target.provider_id,
                request.candidate_target.connection_id,
            )
            same_bucket = (
                src_pol is not None
                and dst_pol is not None
                and src_pol.quota_bucket_key == dst_pol.quota_bucket_key
            )
            same_provider_rotation = (
                request.current_target.provider_id == request.candidate_target.provider_id
                and request.current_target.connection_id != request.candidate_target.connection_id
                and (
                    request.failure_reason_code in {"RATE_LIMITED", "QUOTA_EXCEEDED"}
                    or same_bucket
                    or (src_pol is not None and not src_pol.independent_quota_proof)
                )
            )
            if same_provider_rotation:
                ev = _make_scheduler_event(
                    event_type="scheduler.key_rotation_blocked",
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                    correlation_id=f"sched-keyrot-{request.run_id}",
                    task_id=request.task_id,
                    run_id=request.run_id,
                    provider_id=request.current_target.provider_id,
                    connection_id=request.current_target.connection_id,
                    payload={
                        "from_connection_id": request.current_target.connection_id,
                        "to_connection_id": request.candidate_target.connection_id,
                        "failure_reason_code": request.failure_reason_code,
                        "same_bucket": same_bucket,
                    },
                )
                tx.append_event(ev)
                return FallbackDecision(
                    decision="DENY",
                    reason_code="KEY_ROTATION_FORBIDDEN",
                    message=(
                        "Switching keys/connections within the same provider or unproven quota bucket "
                        "to bypass rate/quota limits is forbidden."
                    ),
                    audit_event=ev,
                )

            decision = evaluate_fallback(
                request.fallback_policy,
                FallbackEvaluationRequest(
                    task_id=request.task_id,
                    run_id=request.run_id,
                    current_target=request.current_target,
                    candidate_target=request.candidate_target,
                    attempt_number=request.attempt_number,
                    operation_is_idempotent=request.operation_is_idempotent,
                    failure_reason_code=request.failure_reason_code,
                    available_target_capabilities=request.available_target_capabilities,
                    is_key_rotation_attempt=False,
                    requested_at=request.timestamp,
                    correlation_id=f"sched-fb-{request.run_id}",
                ),
            )
            tx.append_event(decision.audit_event)
            return decision

        return repo.execute_transaction(_tx_op)

    def register_scheduled_task(
        self,
        request: RegisterScheduledTaskRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> ScheduledTaskMetadata:
        """Persist timezone-aware scheduled execution metadata without fabricating calendar triggers."""
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.transition",
            resource=f"scheduler:schedule:{request.schedule_id}",
            task_id=request.task_id,
            timestamp=request.created_at,
            correlation_id=f"sched-reg-{request.schedule_id}",
        )

        def _tx_op(tx: SchedulerTransactionContext) -> ScheduledTaskMetadata:
            policy = tx.get_policy(request.provider_id, request.connection_id)
            if policy is None:
                raise SchedulerReferenceError(
                    "QUOTA_POLICY_NOT_FOUND",
                    f"No quota policy registered for {request.provider_id}/{request.connection_id}.",
                )
            if request.task_id is not None:
                task = tx.get_task(request.task_id)
                if task is None:
                    raise SchedulerReferenceError(
                        "TASK_NOT_FOUND",
                        f"Referenced Task '{request.task_id}' does not exist.",
                    )
                if task.status in TERMINAL_TASK_STATUSES:
                    raise SchedulerStateError(
                        "TERMINAL_TASK_CANNOT_SCHEDULE",
                        f"Cannot bind schedule to terminal Task '{request.task_id}'.",
                    )

            ev = _make_scheduler_event(
                event_type="scheduler.schedule_registered",
                auth_context=auth_context,
                timestamp=request.created_at,
                correlation_id=f"sched-reg-{request.schedule_id}",
                task_id=request.task_id,
                provider_id=request.provider_id,
                connection_id=request.connection_id,
                model_id=request.model_id,
                payload={
                    "schedule_id": request.schedule_id,
                    "timezone_name": request.timezone_name,
                    "scheduled_at": request.scheduled_at.isoformat(),
                    "requires_approval": request.requires_approval,
                },
            )
            tx.append_event(ev)
            record = ScheduledTaskMetadata(
                schedule_id=request.schedule_id,
                title=request.title,
                description=request.description,
                project_id=request.project_id,
                task_id=request.task_id,
                provider_id=request.provider_id,
                connection_id=request.connection_id,
                model_id=request.model_id,
                owner_actor_id=auth_context.actor_id,
                owner_actor_kind=auth_context.actor_kind,
                timezone_name=request.timezone_name,
                scheduled_at=request.scheduled_at,
                recurrence_rule=request.recurrence_rule,
                recurrence_interval_seconds=request.recurrence_interval_seconds,
                next_execution_at=request.scheduled_at,
                previous_execution_at=None,
                status="SCHEDULED",
                requires_approval=request.requires_approval,
                approval_id=request.approval_id,
                policy_note=request.policy_note,
                audit_event_id=ev.event_id,
                record_version=1,
                created_at=request.created_at,
                updated_at=request.created_at,
            )
            return tx.insert_scheduled_task(record)

        return repo.execute_transaction(_tx_op)

    def evaluate_due_schedules(
        self,
        *,
        now: datetime,
        auth_context: TrustedAuthContext,
    ) -> list[ScheduleEvaluationOutcome]:
        """Enqueue due scheduled tasks ONLY when approval, policy, and Task eligibility succeed."""
        validate_aware_datetime(now, "now")
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.transition",
            resource="scheduler:schedules:evaluate",
            timestamp=now,
            correlation_id="sched-eval-due",
        )

        def _tx_op(tx: SchedulerTransactionContext) -> list[ScheduleEvaluationOutcome]:
            due = tx.list_due_schedules(now=now)
            outcomes: list[ScheduleEvaluationOutcome] = []
            for sched in due:
                policy = tx.get_policy(sched.provider_id, sched.connection_id)
                if policy is None or not policy.enabled:
                    outcomes.append(
                        ScheduleEvaluationOutcome(
                            schedule=sched,
                            triggered=False,
                            skip_reason_code="CONNECTION_UNAVAILABLE_OR_DISABLED",
                            skip_reason="Scheduled task skipped because connection policy is disabled or missing.",
                        )
                    )
                    continue
                if sched.requires_approval:
                    if sched.approval_id is None or not tx.is_approval_approved(sched.approval_id):
                        outcomes.append(
                            ScheduleEvaluationOutcome(
                                schedule=sched,
                                triggered=False,
                                skip_reason_code="APPROVAL_REQUIRED_NOT_GRANTED",
                                skip_reason="Scheduled task requires valid APPROVED operator approval before queuing.",
                            )
                        )
                        continue
                if sched.task_id is None:
                    outcomes.append(
                        ScheduleEvaluationOutcome(
                            schedule=sched,
                            triggered=False,
                            skip_reason_code="TASK_NOT_BOUND",
                            skip_reason="Schedule has no materialized Task bound yet.",
                        )
                    )
                    continue
                task = tx.get_task(sched.task_id)
                if task is None or task.status in TERMINAL_TASK_STATUSES:
                    outcomes.append(
                        ScheduleEvaluationOutcome(
                            schedule=sched,
                            triggered=False,
                            skip_reason_code="TASK_TERMINAL_OR_MISSING",
                            skip_reason="Bound Task is terminal or missing.",
                        )
                    )
                    continue

                idem_key = f"sched:{sched.schedule_id}:{(sched.next_execution_at or now).isoformat()}"
                existing = tx.find_queue_by_idempotency(idem_key)
                if existing is None:
                    req = EnqueueTaskRequest(
                        task_id=task.task_id,
                        schedule_id=sched.schedule_id,
                        provider_id=sched.provider_id,
                        connection_id=sched.connection_id,
                        model_id=sched.model_id or policy.model_id,
                        priority=task.priority,
                        idempotency_key=idem_key,
                        timestamp=now,
                    )
                    enq_entry = QueueEntry(
                        queue_entry_id=req.queue_entry_id,
                        task_id=task.task_id,
                        schedule_id=sched.schedule_id,
                        provider_id=sched.provider_id,
                        connection_id=sched.connection_id,
                        model_id=req.model_id,
                        quota_bucket_key=policy.quota_bucket_key,
                        priority=task.priority,
                        priority_rank=_PRIORITY_BASE_RANK[task.priority],
                        fairness_aging_seconds=req.fairness_aging_seconds,
                        requested_quota_units=req.requested_quota_units,
                        status="READY",
                        idempotency_key=idem_key,
                        request_fingerprint=_enqueue_fingerprint(req),
                        requested_by=sched.owner_actor_id,
                        enqueued_at=now,
                        updated_at=now,
                    )
                    tx.append_event(
                        _make_scheduler_event(
                            event_type="scheduler.task_enqueued",
                            auth_context=auth_context,
                            timestamp=now,
                            correlation_id=f"sched-due-{sched.schedule_id}",
                            task_id=task.task_id,
                            provider_id=sched.provider_id,
                            connection_id=sched.connection_id,
                            payload={
                                "queue_entry_id": enq_entry.queue_entry_id,
                                "schedule_id": sched.schedule_id,
                                "quota_bucket_key": policy.quota_bucket_key,
                            },
                        )
                    )
                    saved_entry = tx.insert_queue_entry(enq_entry)
                else:
                    saved_entry = existing

                next_exec = (
                    (sched.next_execution_at or now) + timedelta(seconds=sched.recurrence_interval_seconds)
                    if sched.recurrence_interval_seconds is not None
                    else None
                )
                next_status: ScheduleStatus = "SCHEDULED" if next_exec is not None else "TRIGGERED"
                updated_sched = tx.update_schedule_execution(
                    schedule_id=sched.schedule_id,
                    expected_record_version=sched.record_version,
                    status=next_status,
                    previous_execution_at=now,
                    next_execution_at=next_exec,
                    task_id=task.task_id,
                    updated_at=now,
                )
                outcomes.append(
                    ScheduleEvaluationOutcome(
                        schedule=updated_sched,
                        triggered=True,
                        enqueued_entry=saved_entry,
                    )
                )
            return outcomes

        return repo.execute_transaction(_tx_op)

    def get_queue_visibility(
        self,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> QueueVisibilitySnapshot:
        """Return truthful queue state grouped into ready, reserved, running, and blocked queues."""
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource="scheduler:queue:visibility",
            timestamp=timestamp,
            correlation_id="sched-vis",
        )
        entries = repo.list_queue_entries(include_terminal=False)
        buckets = repo.list_buckets()
        return QueueVisibilitySnapshot(
            total_entries=len(entries),
            ready_entries=[e for e in entries if e.status in {"QUEUED", "READY"}],
            reserved_entries=[e for e in entries if e.status == "RESERVED"],
            running_entries=[e for e in entries if e.status == "RUNNING"],
            blocked_by_quota=[e for e in entries if e.status == "BLOCKED_BY_QUOTA"],
            blocked_by_rate_limit=[e for e in entries if e.status == "BLOCKED_BY_RATE_LIMIT"],
            blocked_by_governance=[e for e in entries if e.status == "BLOCKED_GOVERNANCE"],
            buckets=buckets,
        )

    def get_active_executions(
        self,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> ActiveExecutionSnapshot:
        """List active RESERVED and Runtime-confirmed ACTIVE execution leases cleanly."""
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource="scheduler:active",
            timestamp=timestamp,
            correlation_id="sched-active",
        )
        leases = repo.list_active_leases()
        items = [
            ActiveExecutionItem(
                lease_id=lease.lease_id,
                queue_entry_id=lease.queue_entry_id,
                task_id=lease.task_id,
                run_id=lease.run_id,
                worker_id=lease.worker_id,
                provider_id=lease.provider_id,
                connection_id=lease.connection_id,
                model_id=lease.model_id,
                quota_bucket_key=lease.quota_bucket_key,
                lease_state=lease.state,  # type: ignore[arg-type]
                started_at=lease.started_at,
                acquired_at=lease.acquired_at,
                expires_at=lease.expires_at,
            )
            for lease in leases
            if lease.state in {"RESERVED", "ACTIVE"}
        ]
        return ActiveExecutionSnapshot(
            items=items,
            total_reserved=sum(1 for item in items if item.lease_state == "RESERVED"),
            total_running=sum(1 for item in items if item.lease_state == "ACTIVE"),
        )

    def list_scheduled_tasks(
        self,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> list[ScheduledTaskMetadata]:
        """List persisted scheduled task metadata owned by caller or visible to Master."""
        repo = self._require_repo()
        _map_governed_auth(
            self._pm,
            auth_context=auth_context,
            action="task.read",
            resource="scheduler:schedules:list",
            timestamp=timestamp,
            correlation_id="sched-list",
        )
        owner_filter = (
            auth_context.actor_id if auth_context.actor_kind == "worker_agent" else None
        )
        return repo.list_schedules(owner_actor_id=owner_filter)
