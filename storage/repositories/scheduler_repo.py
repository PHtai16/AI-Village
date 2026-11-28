"""SQLite repository implementing P11.03 Scheduler queue, quota buckets, leases & schedules."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import sqlite3
from typing import Any

from core.contracts.event import Event
from core.contracts.run import Run
from core.contracts.task import Task
from core.optimization.scheduler_models import (
    ConnectionQuotaPolicy,
    ExecutionLease,
    QueueEntry,
    QueueEntryStatus,
    QuotaBucketState,
    ScheduleStatus,
    ScheduledTaskMetadata,
    SchedulerConflictError,
    SchedulerPersistenceError,
    SchedulerTransactionContext,
)
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore, _row_to_run, _row_to_task


def _row_to_bucket(row: sqlite3.Row) -> QuotaBucketState:
    """Rehydrate ``QuotaBucketState`` from SQLite row."""
    return QuotaBucketState.model_validate(
        {
            "quota_bucket_key": row["quota_bucket_key"],
            "provider_id": row["provider_id"],
            "quota_scope_kind": row["quota_scope_kind"],
            "max_concurrency": int(row["max_concurrency"]),
            "rate_limit_max_requests": row["rate_limit_max_requests"],
            "rate_limit_window_seconds": row["rate_limit_window_seconds"],
            "quota_limit_units": row["quota_limit_units"],
            "quota_used_units": int(row["quota_used_units"]),
            "base_backoff_seconds": int(row["base_backoff_seconds"]),
            "max_backoff_seconds": int(row["max_backoff_seconds"]),
            "max_retries": int(row["max_retries"]),
            "record_version": int(row["record_version"]),
            "updated_at": row["updated_at"],
        }
    )


def _row_to_policy(row: sqlite3.Row) -> ConnectionQuotaPolicy:
    """Rehydrate ``ConnectionQuotaPolicy`` from SQLite row."""
    return ConnectionQuotaPolicy.model_validate(
        {
            "policy_id": row["policy_id"],
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "project_ref": row["project_ref"],
            "account_ref": row["account_ref"],
            "model_id": row["model_id"],
            "quota_scope_kind": row["quota_scope_kind"],
            "quota_bucket_key": row["quota_bucket_key"],
            "independent_quota_proof": bool(int(row["independent_quota_proof"])),
            "max_concurrency": int(row["max_concurrency"]),
            "rate_limit_max_requests": row["rate_limit_max_requests"],
            "rate_limit_window_seconds": row["rate_limit_window_seconds"],
            "quota_limit_units": row["quota_limit_units"],
            "base_backoff_seconds": int(row["base_backoff_seconds"]),
            "max_backoff_seconds": int(row["max_backoff_seconds"]),
            "max_retries": int(row["max_retries"]),
            "enabled": bool(int(row["enabled"])),
            "record_version": int(row["record_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _row_to_queue_entry(row: sqlite3.Row) -> QueueEntry:
    """Rehydrate ``QueueEntry`` from SQLite row."""
    return QueueEntry.model_validate(
        {
            "queue_entry_id": row["queue_entry_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "schedule_id": row["schedule_id"],
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "model_id": row["model_id"],
            "quota_bucket_key": row["quota_bucket_key"],
            "priority": row["priority"],
            "priority_rank": int(row["priority_rank"]),
            "fairness_aging_seconds": int(row["fairness_aging_seconds"]),
            "requested_quota_units": int(row["requested_quota_units"]),
            "status": row["status"],
            "block_reason_code": row["block_reason_code"],
            "block_reason": row["block_reason"],
            "next_retry_at": row["next_retry_at"],
            "retry_count": int(row["retry_count"]),
            "idempotency_key": row["idempotency_key"],
            "request_fingerprint": row["request_fingerprint"],
            "requested_by": row["requested_by"],
            "assigned_worker_id": row["assigned_worker_id"],
            "enqueued_at": row["enqueued_at"],
            "reserved_at": row["reserved_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "updated_at": row["updated_at"],
            "record_version": int(row["record_version"]),
        }
    )


def _row_to_lease(row: sqlite3.Row) -> ExecutionLease:
    """Rehydrate ``ExecutionLease`` from SQLite row."""
    return ExecutionLease.model_validate(
        {
            "lease_id": row["lease_id"],
            "queue_entry_id": row["queue_entry_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "worker_id": row["worker_id"],
            "owner_token": row["owner_token"],
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "model_id": row["model_id"],
            "quota_bucket_key": row["quota_bucket_key"],
            "state": row["state"],
            "quota_units_reserved": int(row["quota_units_reserved"]),
            "acquired_at": row["acquired_at"],
            "expires_at": row["expires_at"],
            "started_at": row["started_at"],
            "released_at": row["released_at"],
            "release_reason": row["release_reason"],
            "record_version": int(row["record_version"]),
        }
    )


def _row_to_schedule(row: sqlite3.Row) -> ScheduledTaskMetadata:
    """Rehydrate ``ScheduledTaskMetadata`` from SQLite row."""
    return ScheduledTaskMetadata.model_validate(
        {
            "schedule_id": row["schedule_id"],
            "title": row["title"],
            "description": row["description"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "model_id": row["model_id"],
            "owner_actor_id": row["owner_actor_id"],
            "owner_actor_kind": row["owner_actor_kind"],
            "timezone_name": row["timezone_name"],
            "scheduled_at": row["scheduled_at"],
            "recurrence_rule": row["recurrence_rule"],
            "recurrence_interval_seconds": row["recurrence_interval_seconds"],
            "next_execution_at": row["next_execution_at"],
            "previous_execution_at": row["previous_execution_at"],
            "status": row["status"],
            "requires_approval": bool(int(row["requires_approval"])),
            "approval_id": row["approval_id"],
            "policy_note": row["policy_note"],
            "audit_event_id": row["audit_event_id"],
            "record_version": int(row["record_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


class _SQLiteSchedulerTransactionContext:
    """Transaction-scoped SQLite implementation of ``SchedulerTransactionContext``."""

    def __init__(self, store: SQLiteRepositoryStore, conn: sqlite3.Connection) -> None:
        self._store = store
        self._conn = conn

    def get_policy(self, provider_id: str, connection_id: str) -> ConnectionQuotaPolicy | None:
        row = self._conn.execute(
            "SELECT * FROM scheduler_quota_policies WHERE provider_id = ? AND connection_id = ?;",
            (provider_id, connection_id),
        ).fetchone()
        return _row_to_policy(row) if row is not None else None

    def get_bucket(self, quota_bucket_key: str) -> QuotaBucketState | None:
        row = self._conn.execute(
            "SELECT * FROM scheduler_quota_buckets WHERE quota_bucket_key = ?;",
            (quota_bucket_key,),
        ).fetchone()
        return _row_to_bucket(row) if row is not None else None

    def upsert_bucket_and_policy(
        self,
        bucket: QuotaBucketState,
        policy: ConnectionQuotaPolicy,
    ) -> ConnectionQuotaPolicy:
        self._conn.execute(
            """
            INSERT INTO scheduler_quota_buckets (
                quota_bucket_key, provider_id, quota_scope_kind, max_concurrency,
                rate_limit_max_requests, rate_limit_window_seconds, quota_limit_units,
                quota_used_units, base_backoff_seconds, max_backoff_seconds, max_retries,
                record_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(quota_bucket_key) DO UPDATE SET
                quota_scope_kind = excluded.quota_scope_kind,
                max_concurrency = excluded.max_concurrency,
                rate_limit_max_requests = excluded.rate_limit_max_requests,
                rate_limit_window_seconds = excluded.rate_limit_window_seconds,
                quota_limit_units = excluded.quota_limit_units,
                base_backoff_seconds = excluded.base_backoff_seconds,
                max_backoff_seconds = excluded.max_backoff_seconds,
                max_retries = excluded.max_retries,
                record_version = scheduler_quota_buckets.record_version + 1,
                updated_at = excluded.updated_at;
            """,
            (
                bucket.quota_bucket_key,
                bucket.provider_id,
                bucket.quota_scope_kind,
                bucket.max_concurrency,
                bucket.rate_limit_max_requests,
                bucket.rate_limit_window_seconds,
                bucket.quota_limit_units,
                bucket.quota_used_units,
                bucket.base_backoff_seconds,
                bucket.max_backoff_seconds,
                bucket.max_retries,
                bucket.record_version,
                bucket.updated_at.isoformat(),
            ),
        )
        self._conn.execute(
            """
            INSERT INTO scheduler_quota_policies (
                policy_id, provider_id, connection_id, project_ref, account_ref, model_id,
                quota_scope_kind, quota_bucket_key, independent_quota_proof, max_concurrency,
                rate_limit_max_requests, rate_limit_window_seconds, quota_limit_units,
                base_backoff_seconds, max_backoff_seconds, max_retries, enabled,
                record_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(policy_id) DO UPDATE SET
                project_ref = excluded.project_ref,
                account_ref = excluded.account_ref,
                model_id = excluded.model_id,
                quota_scope_kind = excluded.quota_scope_kind,
                quota_bucket_key = excluded.quota_bucket_key,
                independent_quota_proof = excluded.independent_quota_proof,
                max_concurrency = excluded.max_concurrency,
                rate_limit_max_requests = excluded.rate_limit_max_requests,
                rate_limit_window_seconds = excluded.rate_limit_window_seconds,
                quota_limit_units = excluded.quota_limit_units,
                base_backoff_seconds = excluded.base_backoff_seconds,
                max_backoff_seconds = excluded.max_backoff_seconds,
                max_retries = excluded.max_retries,
                enabled = excluded.enabled,
                record_version = scheduler_quota_policies.record_version + 1,
                updated_at = excluded.updated_at;
            """,
            (
                policy.policy_id,
                policy.provider_id,
                policy.connection_id,
                policy.project_ref,
                policy.account_ref,
                policy.model_id,
                policy.quota_scope_kind,
                policy.quota_bucket_key,
                1 if policy.independent_quota_proof else 0,
                policy.max_concurrency,
                policy.rate_limit_max_requests,
                policy.rate_limit_window_seconds,
                policy.quota_limit_units,
                policy.base_backoff_seconds,
                policy.max_backoff_seconds,
                policy.max_retries,
                1 if policy.enabled else 0,
                policy.record_version,
                policy.created_at.isoformat(),
                policy.updated_at.isoformat(),
            ),
        )
        loaded = self.get_policy(policy.provider_id, policy.connection_id)
        assert loaded is not None
        return loaded

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?;", (task_id,)).fetchone()
        return _row_to_task(row) if row is not None else None

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?;", (run_id,)).fetchone()
        return _row_to_run(row) if row is not None else None

    def has_pending_clarification_intake(self, task_id: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM intake_requests
            WHERE (related_task_id = ? OR created_task_id = ?)
              AND processing_status = 'NEEDS_CLARIFICATION'
            LIMIT 1;
            """,
            (task_id, task_id),
        ).fetchone()
        return row is not None

    def is_approval_approved(self, approval_id: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM approval_requests WHERE approval_id = ?;",
            (approval_id,),
        ).fetchone()
        return row is not None and str(row["status"]) == "APPROVED"

    def find_queue_by_idempotency(self, idempotency_key: str) -> QueueEntry | None:
        row = self._conn.execute(
            "SELECT * FROM scheduler_queue_entries WHERE idempotency_key = ?;",
            (idempotency_key,),
        ).fetchone()
        return _row_to_queue_entry(row) if row is not None else None

    def find_active_queue_for_task(self, task_id: str) -> QueueEntry | None:
        row = self._conn.execute(
            """
            SELECT * FROM scheduler_queue_entries
            WHERE task_id = ? AND status NOT IN ('CANCELLED', 'COMPLETED', 'FAILED')
            LIMIT 1;
            """,
            (task_id,),
        ).fetchone()
        return _row_to_queue_entry(row) if row is not None else None

    def _get_queue_entry_conn(self, queue_entry_id: str) -> QueueEntry | None:
        row = self._conn.execute(
            "SELECT * FROM scheduler_queue_entries WHERE queue_entry_id = ?;",
            (queue_entry_id,),
        ).fetchone()
        return _row_to_queue_entry(row) if row is not None else None

    def insert_queue_entry(self, entry: QueueEntry) -> QueueEntry:
        self._conn.execute(
            """
            INSERT INTO scheduler_queue_entries (
                queue_entry_id, task_id, run_id, schedule_id, provider_id, connection_id,
                model_id, quota_bucket_key, priority, priority_rank, fairness_aging_seconds,
                requested_quota_units, status, block_reason_code, block_reason, next_retry_at,
                retry_count, idempotency_key, request_fingerprint, requested_by, assigned_worker_id,
                enqueued_at, reserved_at, started_at, completed_at, updated_at, record_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                entry.queue_entry_id,
                entry.task_id,
                entry.run_id,
                entry.schedule_id,
                entry.provider_id,
                entry.connection_id,
                entry.model_id,
                entry.quota_bucket_key,
                entry.priority,
                entry.priority_rank,
                entry.fairness_aging_seconds,
                entry.requested_quota_units,
                entry.status,
                entry.block_reason_code,
                entry.block_reason,
                entry.next_retry_at.isoformat() if entry.next_retry_at else None,
                entry.retry_count,
                entry.idempotency_key,
                entry.request_fingerprint,
                entry.requested_by,
                entry.assigned_worker_id,
                entry.enqueued_at.isoformat(),
                entry.reserved_at.isoformat() if entry.reserved_at else None,
                entry.started_at.isoformat() if entry.started_at else None,
                entry.completed_at.isoformat() if entry.completed_at else None,
                entry.updated_at.isoformat(),
                entry.record_version,
            ),
        )
        loaded = self._get_queue_entry_conn(entry.queue_entry_id)
        assert loaded is not None
        return loaded

    def list_candidate_queue_entries(self) -> list[QueueEntry]:
        rows = self._conn.execute(
            """
            SELECT * FROM scheduler_queue_entries
            WHERE status IN (
                'QUEUED', 'READY', 'BLOCKED_BY_QUOTA', 'BLOCKED_BY_RATE_LIMIT', 'BLOCKED_GOVERNANCE'
            )
            ORDER BY priority_rank ASC, enqueued_at ASC, queue_entry_id ASC;
            """
        ).fetchall()
        return [_row_to_queue_entry(r) for r in rows]

    def count_active_leases_for_bucket(self, quota_bucket_key: str) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM scheduler_execution_leases
            WHERE quota_bucket_key = ? AND state IN ('RESERVED', 'ACTIVE');
            """,
            (quota_bucket_key,),
        ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def count_rate_events_since(self, quota_bucket_key: str, window_start: datetime) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM scheduler_rate_limit_events
            WHERE quota_bucket_key = ? AND recorded_at > ?;
            """,
            (quota_bucket_key, window_start.isoformat()),
        ).fetchone()
        return int(row["cnt"]) if row is not None else 0

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
        cur = self._conn.execute(
            """
            UPDATE scheduler_queue_entries
            SET status = ?,
                block_reason_code = ?,
                block_reason = ?,
                retry_count = ?,
                next_retry_at = ?,
                updated_at = ?,
                record_version = record_version + 1
            WHERE queue_entry_id = ? AND record_version = ?;
            """,
            (
                status,
                block_reason_code,
                block_reason,
                retry_count,
                next_retry_at.isoformat() if next_retry_at else None,
                updated_at.isoformat(),
                queue_entry_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise SchedulerConflictError(
                "STALE_QUEUE_VERSION",
                f"Optimistic concurrency mismatch updating queue entry '{queue_entry_id}'.",
            )
        loaded = self._get_queue_entry_conn(queue_entry_id)
        assert loaded is not None
        return loaded

    def cancel_or_fail_queue_entry(
        self,
        *,
        queue_entry_id: str,
        expected_record_version: int,
        status: str,
        reason_code: str,
        reason: str,
        updated_at: datetime,
    ) -> QueueEntry:
        cur = self._conn.execute(
            """
            UPDATE scheduler_queue_entries
            SET status = ?,
                block_reason_code = ?,
                block_reason = ?,
                completed_at = ?,
                updated_at = ?,
                record_version = record_version + 1
            WHERE queue_entry_id = ? AND record_version = ?;
            """,
            (
                status,
                reason_code,
                reason,
                updated_at.isoformat(),
                updated_at.isoformat(),
                queue_entry_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise SchedulerConflictError(
                "STALE_QUEUE_VERSION",
                f"Optimistic concurrency mismatch closing queue entry '{queue_entry_id}'.",
            )
        loaded = self._get_queue_entry_conn(queue_entry_id)
        assert loaded is not None
        return loaded

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
        cur_q = self._conn.execute(
            """
            UPDATE scheduler_queue_entries
            SET status = 'RESERVED',
                block_reason_code = NULL,
                block_reason = NULL,
                next_retry_at = NULL,
                assigned_worker_id = ?,
                reserved_at = ?,
                updated_at = ?,
                record_version = record_version + 1
            WHERE queue_entry_id = ? AND record_version = ?;
            """,
            (
                lease.worker_id,
                now.isoformat(),
                now.isoformat(),
                queue_entry_id,
                expected_queue_version,
            ),
        )
        if cur_q.rowcount != 1:
            raise SchedulerConflictError(
                "STALE_QUEUE_VERSION",
                f"Concurrent reservation conflict on queue entry '{queue_entry_id}'.",
            )
        self._conn.execute(
            """
            UPDATE scheduler_quota_buckets
            SET quota_used_units = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE quota_bucket_key = ?;
            """,
            (new_bucket_used_units, now.isoformat(), lease.quota_bucket_key),
        )
        self._conn.execute(
            """
            INSERT INTO scheduler_execution_leases (
                lease_id, queue_entry_id, task_id, run_id, worker_id, owner_token,
                provider_id, connection_id, model_id, quota_bucket_key, state,
                quota_units_reserved, acquired_at, expires_at, started_at, released_at,
                release_reason, record_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                lease.lease_id,
                lease.queue_entry_id,
                lease.task_id,
                lease.run_id,
                lease.worker_id,
                lease.owner_token,
                lease.provider_id,
                lease.connection_id,
                lease.model_id,
                lease.quota_bucket_key,
                lease.state,
                lease.quota_units_reserved,
                lease.acquired_at.isoformat(),
                lease.expires_at.isoformat(),
                None,
                None,
                None,
                lease.record_version,
            ),
        )
        self._conn.execute(
            """
            INSERT INTO scheduler_rate_limit_events (
                rate_event_id, quota_bucket_key, provider_id, connection_id,
                queue_entry_id, lease_id, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                rate_event_id,
                lease.quota_bucket_key,
                lease.provider_id,
                lease.connection_id,
                lease.queue_entry_id,
                lease.lease_id,
                now.isoformat(),
            ),
        )
        updated_q = self._get_queue_entry_conn(queue_entry_id)
        updated_l = self.get_lease_for_update(lease.lease_id)
        assert updated_q is not None and updated_l is not None
        return updated_q, updated_l

    def get_lease_for_update(self, lease_id: str) -> ExecutionLease | None:
        row = self._conn.execute(
            "SELECT * FROM scheduler_execution_leases WHERE lease_id = ?;",
            (lease_id,),
        ).fetchone()
        return _row_to_lease(row) if row is not None else None

    def confirm_lease_started(
        self,
        *,
        lease_id: str,
        expected_lease_version: int,
        run_id: str,
        started_at: datetime,
    ) -> tuple[QueueEntry, ExecutionLease]:
        cur = self._conn.execute(
            """
            UPDATE scheduler_execution_leases
            SET state = 'ACTIVE',
                run_id = ?,
                started_at = ?,
                record_version = record_version + 1
            WHERE lease_id = ? AND record_version = ?;
            """,
            (run_id, started_at.isoformat(), lease_id, expected_lease_version),
        )
        if cur.rowcount != 1:
            raise SchedulerConflictError(
                "STALE_LEASE_VERSION",
                f"Optimistic lock mismatch confirming lease '{lease_id}'.",
            )
        lease = self.get_lease_for_update(lease_id)
        assert lease is not None
        self._conn.execute(
            """
            UPDATE scheduler_queue_entries
            SET status = 'RUNNING',
                run_id = ?,
                started_at = ?,
                updated_at = ?,
                record_version = record_version + 1
            WHERE queue_entry_id = ?;
            """,
            (run_id, started_at.isoformat(), started_at.isoformat(), lease.queue_entry_id),
        )
        q_row = self._get_queue_entry_conn(lease.queue_entry_id)
        assert q_row is not None
        return q_row, lease

    def release_lease_and_queue(
        self,
        *,
        lease_id: str,
        expected_lease_version: int,
        lease_state: str,
        queue_status: str,
        reason: str,
        released_at: datetime,
    ) -> tuple[QueueEntry, ExecutionLease]:
        cur = self._conn.execute(
            """
            UPDATE scheduler_execution_leases
            SET state = ?,
                released_at = ?,
                release_reason = ?,
                record_version = record_version + 1
            WHERE lease_id = ? AND record_version = ?;
            """,
            (lease_state, released_at.isoformat(), reason, lease_id, expected_lease_version),
        )
        if cur.rowcount != 1:
            raise SchedulerConflictError(
                "STALE_LEASE_VERSION",
                f"Optimistic lock mismatch releasing lease '{lease_id}'.",
            )
        lease = self.get_lease_for_update(lease_id)
        assert lease is not None
        comp_ts = released_at.isoformat() if queue_status in {"COMPLETED", "FAILED", "CANCELLED"} else None
        self._conn.execute(
            """
            UPDATE scheduler_queue_entries
            SET status = ?,
                completed_at = ?,
                updated_at = ?,
                record_version = record_version + 1
            WHERE queue_entry_id = ?;
            """,
            (queue_status, comp_ts, released_at.isoformat(), lease.queue_entry_id),
        )
        q_row = self._get_queue_entry_conn(lease.queue_entry_id)
        assert q_row is not None
        return q_row, lease

    def expire_overdue_leases(self, *, now: datetime) -> list[tuple[ExecutionLease, QueueEntry]]:
        rows = self._conn.execute(
            """
            SELECT * FROM scheduler_execution_leases
            WHERE state IN ('RESERVED', 'ACTIVE') AND expires_at <= ?
            ORDER BY expires_at ASC, lease_id ASC;
            """,
            (now.isoformat(),),
        ).fetchall()
        results: list[tuple[ExecutionLease, QueueEntry]] = []
        for row in rows:
            lease_id = str(row["lease_id"])
            q_id = str(row["queue_entry_id"])
            self._conn.execute(
                """
                UPDATE scheduler_execution_leases
                SET state = 'EXPIRED',
                    released_at = ?,
                    release_reason = 'LEASE_TTL_EXPIRED',
                    record_version = record_version + 1
                WHERE lease_id = ?;
                """,
                (now.isoformat(), lease_id),
            )
            self._conn.execute(
                """
                UPDATE scheduler_queue_entries
                SET status = 'READY',
                    assigned_worker_id = NULL,
                    reserved_at = NULL,
                    block_reason_code = 'LEASE_EXPIRED_REQUEUED',
                    block_reason = 'Prior execution lease expired; returned safely to READY.',
                    updated_at = ?,
                    record_version = record_version + 1
                WHERE queue_entry_id = ? AND status IN ('RESERVED', 'RUNNING');
                """,
                (now.isoformat(), q_id),
            )
            exp_lease = self.get_lease_for_update(lease_id)
            re_entry = self._get_queue_entry_conn(q_id)
            assert exp_lease is not None and re_entry is not None
            results.append((exp_lease, re_entry))
        return results

    def insert_scheduled_task(self, record: ScheduledTaskMetadata) -> ScheduledTaskMetadata:
        self._conn.execute(
            """
            INSERT INTO scheduled_tasks (
                schedule_id, title, description, project_id, task_id,
                provider_id, connection_id, model_id, owner_actor_id, owner_actor_kind,
                timezone_name, scheduled_at, recurrence_rule, recurrence_interval_seconds,
                next_execution_at, previous_execution_at, status, requires_approval,
                approval_id, policy_note, audit_event_id, record_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                record.schedule_id,
                record.title,
                record.description,
                record.project_id,
                record.task_id,
                record.provider_id,
                record.connection_id,
                record.model_id,
                record.owner_actor_id,
                record.owner_actor_kind,
                record.timezone_name,
                record.scheduled_at.isoformat(),
                record.recurrence_rule,
                record.recurrence_interval_seconds,
                record.next_execution_at.isoformat() if record.next_execution_at else None,
                record.previous_execution_at.isoformat() if record.previous_execution_at else None,
                record.status,
                1 if record.requires_approval else 0,
                record.approval_id,
                record.policy_note,
                record.audit_event_id,
                record.record_version,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE schedule_id = ?;",
            (record.schedule_id,),
        ).fetchone()
        assert row is not None
        return _row_to_schedule(row)

    def list_due_schedules(self, *, now: datetime) -> list[ScheduledTaskMetadata]:
        rows = self._conn.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE status = 'SCHEDULED' AND next_execution_at IS NOT NULL AND next_execution_at <= ?
            ORDER BY next_execution_at ASC, schedule_id ASC;
            """,
            (now.isoformat(),),
        ).fetchall()
        return [_row_to_schedule(r) for r in rows]

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
        cur = self._conn.execute(
            """
            UPDATE scheduled_tasks
            SET status = ?,
                previous_execution_at = ?,
                next_execution_at = ?,
                task_id = COALESCE(?, task_id),
                updated_at = ?,
                record_version = record_version + 1
            WHERE schedule_id = ? AND record_version = ?;
            """,
            (
                status,
                previous_execution_at.isoformat(),
                next_execution_at.isoformat() if next_execution_at else None,
                task_id,
                updated_at.isoformat(),
                schedule_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise SchedulerConflictError(
                "STALE_SCHEDULE_VERSION",
                f"Optimistic lock mismatch updating schedule '{schedule_id}'.",
            )
        row = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE schedule_id = ?;",
            (schedule_id,),
        ).fetchone()
        assert row is not None
        return _row_to_schedule(row)

    def append_event(self, event: Event) -> Event:
        res = self._store.events.append_on_connection(self._conn, event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Audit event append rejected."
            raise SchedulerPersistenceError("AUDIT_EVENT_REJECTED", msg)
        return res.entry.event


class SQLiteSchedulerRepository:
    """SQLite repository implementing durable P11.03 scheduler state & lease isolation."""

    def __init__(
        self,
        store: SQLiteRepositoryStore,
        *,
        before_commit_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Bind Scheduler repository to ``SQLiteRepositoryStore``."""
        self._store = store
        self._before_commit_hook = before_commit_hook

    def execute_transaction(
        self,
        processor: Callable[[SchedulerTransactionContext], Any],
    ) -> Any:
        """Execute ``processor`` inside one atomic ``BEGIN IMMEDIATE`` SQLite transaction."""
        try:
            with self._store.transaction_scope() as conn:
                tx = _SQLiteSchedulerTransactionContext(self._store, conn)
                result = processor(tx)
                if self._before_commit_hook is not None:
                    self._before_commit_hook(conn)
                return result
        except (sqlite3.Error, RepositoryError) as exc:
            raise SchedulerPersistenceError(
                "SCHEDULER_PERSISTENCE_FAILED",
                f"Atomic scheduler transaction failed and rolled back: {exc}",
            ) from exc

    def get_policy(self, provider_id: str, connection_id: str) -> ConnectionQuotaPolicy | None:
        """Fetch persisted ``ConnectionQuotaPolicy`` by provider & connection IDs."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM scheduler_quota_policies WHERE provider_id = ? AND connection_id = ?;",
                (provider_id, connection_id),
            ).fetchone()
            return _row_to_policy(row) if row is not None else None

    def list_buckets(self) -> list[QuotaBucketState]:
        """List persisted shared quota buckets ordered deterministically."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduler_quota_buckets ORDER BY quota_bucket_key ASC;"
            ).fetchall()
            return [_row_to_bucket(r) for r in rows]

    def get_queue_entry(self, queue_entry_id: str) -> QueueEntry | None:
        """Fetch ``QueueEntry`` by ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM scheduler_queue_entries WHERE queue_entry_id = ?;",
                (queue_entry_id,),
            ).fetchone()
            return _row_to_queue_entry(row) if row is not None else None

    def list_queue_entries(self, *, include_terminal: bool = False) -> list[QueueEntry]:
        """List persisted queue entries."""
        where_sql = (
            ""
            if include_terminal
            else "WHERE status NOT IN ('CANCELLED', 'COMPLETED', 'FAILED') "
        )
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM scheduler_queue_entries {where_sql}"
                "ORDER BY priority_rank ASC, enqueued_at ASC, queue_entry_id ASC;"
            ).fetchall()
            return [_row_to_queue_entry(r) for r in rows]

    def get_lease(self, lease_id: str) -> ExecutionLease | None:
        """Fetch ``ExecutionLease`` by ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM scheduler_execution_leases WHERE lease_id = ?;",
                (lease_id,),
            ).fetchone()
            return _row_to_lease(row) if row is not None else None

    def list_active_leases(self) -> list[ExecutionLease]:
        """List active RESERVED and ACTIVE execution leases."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduler_execution_leases
                WHERE state IN ('RESERVED', 'ACTIVE')
                ORDER BY acquired_at ASC, lease_id ASC;
                """
            ).fetchall()
            return [_row_to_lease(r) for r in rows]

    def get_schedule(self, schedule_id: str) -> ScheduledTaskMetadata | None:
        """Fetch ``ScheduledTaskMetadata`` by ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE schedule_id = ?;",
                (schedule_id,),
            ).fetchone()
            return _row_to_schedule(row) if row is not None else None

    def list_schedules(self, *, owner_actor_id: str | None = None) -> list[ScheduledTaskMetadata]:
        """List scheduled tasks optionally filtered by owner actor ID."""
        with self._store.open_connection() as conn:
            if owner_actor_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM scheduled_tasks
                    WHERE owner_actor_id = ?
                    ORDER BY scheduled_at ASC, schedule_id ASC;
                    """,
                    (owner_actor_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scheduled_tasks ORDER BY scheduled_at ASC, schedule_id ASC;"
                ).fetchall()
            return [_row_to_schedule(r) for r in rows]
