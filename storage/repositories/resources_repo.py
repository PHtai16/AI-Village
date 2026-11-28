"""SQLite repository implementing durable P11.04 Resource Ledger, Budget Policies & Threshold Alerts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
import sqlite3
from typing import Any

from core.contracts.event import Event
from core.contracts.references import NotificationRef
from core.contracts.run import Run
from core.contracts.task import Task
from core.departments.reception_models import ActionInboxItem, ActionInboxStatus
from core.departments.resources_models import (
    BucketQuotaSummary,
    ResourceAlertSeverity,
    ResourceBudgetPolicy,
    ResourceLedgerConflictError,
    ResourceLedgerEntry,
    ResourceLedgerPersistenceError,
    ResourceLedgerQuery,
    ResourceLedgerTransactionContext,
    ResourceScopeKind,
    ResourceThresholdAlert,
    ScopeMetricTotals,
    TaskResourceUsageSummary,
)
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore, _row_to_run, _row_to_task


def _dumps(value: Any) -> str:
    """Serialize deterministic compact JSON for SQLite columns."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row_to_policy(row: sqlite3.Row) -> ResourceBudgetPolicy:
    return ResourceBudgetPolicy.model_validate(
        {
            "policy_id": row["policy_id"],
            "scope_kind": row["scope_kind"],
            "scope_ref": row["scope_ref"],
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "model_id": row["model_id"],
            "project_ref": row["project_ref"],
            "account_ref": row["account_ref"],
            "quota_scope_kind": row["quota_scope_kind"],
            "quota_bucket_key": row["quota_bucket_key"],
            "independent_quota_proof": bool(int(row["independent_quota_proof"])),
            "budget_cost_usd_limit": float(row["budget_cost_usd_limit"])
            if row["budget_cost_usd_limit"] is not None
            else None,
            "duration_ms_limit": int(row["duration_ms_limit"])
            if row["duration_ms_limit"] is not None
            else None,
            "memory_mb_limit": float(row["memory_mb_limit"])
            if row["memory_mb_limit"] is not None
            else None,
            "total_tokens_limit": int(row["total_tokens_limit"])
            if row["total_tokens_limit"] is not None
            else None,
            "quota_units_limit": int(row["quota_units_limit"])
            if row["quota_units_limit"] is not None
            else None,
            "warning_ratio": float(row["warning_ratio"]),
            "critical_ratio": float(row["critical_ratio"]),
            "requires_operator_approval_above_critical": bool(
                int(row["requires_operator_approval_above_critical"])
            ),
            "approval_id": row["approval_id"],
            "policy_note": row["policy_note"],
            "enabled": bool(int(row["enabled"])),
            "record_version": int(row["record_version"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _row_to_ledger(row: sqlite3.Row) -> ResourceLedgerEntry:
    return ResourceLedgerEntry.model_validate(
        {
            "ledger_entry_id": row["ledger_entry_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "project_id": row["project_id"],
            "agent_id": row["agent_id"],
            "session_id": row["session_id"],
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "model_id": row["model_id"],
            "project_ref": row["project_ref"],
            "account_ref": row["account_ref"],
            "quota_scope_kind": row["quota_scope_kind"],
            "quota_bucket_key": row["quota_bucket_key"],
            "quota_independence_proven": bool(int(row["quota_independence_proven"])),
            "duration_ms": int(row["duration_ms"]) if row["duration_ms"] is not None else None,
            "provider_duration_ms": int(row["provider_duration_ms"])
            if row["provider_duration_ms"] is not None
            else None,
            "duration_source": row["duration_source"],
            "memory_mb": float(row["memory_mb"]) if row["memory_mb"] is not None else None,
            "peak_memory_mb": float(row["peak_memory_mb"])
            if row["peak_memory_mb"] is not None
            else None,
            "memory_source": row["memory_source"],
            "input_tokens": int(row["input_tokens"]) if row["input_tokens"] is not None else None,
            "output_tokens": int(row["output_tokens"]) if row["output_tokens"] is not None else None,
            "thinking_tokens": int(row["thinking_tokens"])
            if row["thinking_tokens"] is not None
            else None,
            "cached_tokens": int(row["cached_tokens"]) if row["cached_tokens"] is not None else None,
            "total_tokens": int(row["total_tokens"]) if row["total_tokens"] is not None else None,
            "token_usage_status": row["token_usage_status"],
            "actual_cost_usd": float(row["actual_cost_usd"])
            if row["actual_cost_usd"] is not None
            else None,
            "estimated_cost_usd": float(row["estimated_cost_usd"])
            if row["estimated_cost_usd"] is not None
            else None,
            "cost_source": row["cost_source"],
            "pricing_table_ref": row["pricing_table_ref"],
            "telemetry_status": row["telemetry_status"],
            "is_estimated": bool(int(row["is_estimated"])),
            "estimate_label": row["estimate_label"],
            "quota_units_used": int(row["quota_units_used"])
            if row["quota_units_used"] is not None
            else None,
            "quota_limit_units": int(row["quota_limit_units"])
            if row["quota_limit_units"] is not None
            else None,
            "quota_remaining_units": int(row["quota_remaining_units"])
            if row["quota_remaining_units"] is not None
            else None,
            "idempotency_key": row["idempotency_key"],
            "request_fingerprint": row["request_fingerprint"],
            "note": row["note"],
            "metadata": json.loads(str(row["metadata_json"])),
            "recorded_by": row["recorded_by"],
            "audit_event_id": row["audit_event_id"],
            "recorded_at": row["recorded_at"],
        }
    )


def _row_to_alert(row: sqlite3.Row) -> ResourceThresholdAlert:
    return ResourceThresholdAlert.model_validate(
        {
            "alert_id": row["alert_id"],
            "dedup_key": row["dedup_key"],
            "policy_id": row["policy_id"],
            "alert_category": row["alert_category"],
            "scope_kind": row["scope_kind"],
            "scope_ref": row["scope_ref"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "model_id": row["model_id"],
            "quota_bucket_key": row["quota_bucket_key"],
            "severity": row["severity"],
            "status": row["status"],
            "observed_value": float(row["observed_value"]),
            "threshold_limit": float(row["threshold_limit"]),
            "utilization_ratio": float(row["utilization_ratio"]),
            "is_estimated_basis": bool(int(row["is_estimated_basis"])),
            "estimate_label": row["estimate_label"],
            "summary": row["summary"],
            "recommended_action": row["recommended_action"],
            "occurrence_count": int(row["occurrence_count"]),
            "notification_id": row["notification_id"],
            "action_inbox_item_id": row["action_inbox_item_id"],
            "master_notified": bool(int(row["master_notified"])),
            "requires_operator_approval": bool(int(row["requires_operator_approval"])),
            "acknowledged_by": row["acknowledged_by"],
            "acknowledged_at": row["acknowledged_at"],
            "acknowledgement_note": row["acknowledgement_note"],
            "resolved_by": row["resolved_by"],
            "resolved_at": row["resolved_at"],
            "resolution_note": row["resolution_note"],
            "audit_event_id": row["audit_event_id"],
            "record_version": int(row["record_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _policy_matches_entry(policy: ResourceBudgetPolicy, entry: ResourceLedgerEntry) -> bool:
    if not policy.enabled:
        return False
    match policy.scope_kind:
        case "TASK":
            return policy.scope_ref == entry.task_id
        case "PROJECT":
            return policy.scope_ref in {v for v in (entry.project_id, entry.project_ref) if v is not None}
        case "CONNECTION":
            if policy.provider_id != entry.provider_id or entry.connection_id is None:
                return False
            return (
                policy.connection_id == entry.connection_id
                or policy.scope_ref == entry.connection_id
                or policy.scope_ref == f"{entry.provider_id}:{entry.connection_id}"
            )
        case "QUOTA_BUCKET":
            return policy.quota_bucket_key == entry.quota_bucket_key or policy.scope_ref == entry.quota_bucket_key
        case "PROVIDER_MODEL":
            if policy.provider_id != entry.provider_id:
                return False
            return (
                policy.model_id == entry.model_id
                or policy.scope_ref == f"{entry.provider_id}:{entry.model_id or ''}"
            )


def _scope_sql_filter(policy: ResourceBudgetPolicy) -> tuple[str, tuple[Any, ...]]:
    match policy.scope_kind:
        case "TASK":
            return "task_id = ?", (policy.scope_ref,)
        case "PROJECT":
            return "(project_id = ? OR project_ref = ?)", (policy.scope_ref, policy.scope_ref)
        case "CONNECTION":
            conn_ref = policy.connection_id or policy.scope_ref.split(":", 1)[-1]
            return "provider_id = ? AND connection_id = ?", (policy.provider_id or "", conn_ref)
        case "QUOTA_BUCKET":
            bucket = policy.quota_bucket_key or policy.scope_ref
            return "quota_bucket_key = ?", (bucket,)
        case "PROVIDER_MODEL":
            model_ref = policy.model_id or policy.scope_ref.split(":", 1)[-1]
            return "provider_id = ? AND model_id = ?", (policy.provider_id or "", model_ref)


class _SQLiteResourcesTransactionContext:
    """Transaction-scoped SQLite backend for ``ResourceLedgerTransactionContext``."""

    def __init__(self, store: SQLiteRepositoryStore, conn: sqlite3.Connection) -> None:
        self._store = store
        self._conn = conn

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?;", (task_id,)).fetchone()
        return _row_to_task(row) if row is not None else None

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?;", (run_id,)).fetchone()
        return _row_to_run(row) if row is not None else None

    def is_approval_approved(self, approval_id: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM approval_requests WHERE approval_id = ?;",
            (approval_id,),
        ).fetchone()
        return row is not None and str(row["status"]) == "APPROVED"

    def get_policy(self, scope_kind: ResourceScopeKind, scope_ref: str) -> ResourceBudgetPolicy | None:
        row = self._conn.execute(
            "SELECT * FROM resource_budget_policies WHERE scope_kind = ? AND scope_ref = ?;",
            (scope_kind, scope_ref),
        ).fetchone()
        return _row_to_policy(row) if row is not None else None

    def upsert_policy(self, policy: ResourceBudgetPolicy) -> ResourceBudgetPolicy:
        validated = ResourceBudgetPolicy.model_validate_json(policy.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO resource_budget_policies (
                policy_id, scope_kind, scope_ref, provider_id, connection_id, model_id,
                project_ref, account_ref, quota_scope_kind, quota_bucket_key, independent_quota_proof,
                budget_cost_usd_limit, duration_ms_limit, memory_mb_limit, total_tokens_limit, quota_units_limit,
                warning_ratio, critical_ratio, requires_operator_approval_above_critical, approval_id,
                policy_note, enabled, record_version, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_kind, scope_ref) DO UPDATE SET
                provider_id = excluded.provider_id,
                connection_id = excluded.connection_id,
                model_id = excluded.model_id,
                project_ref = excluded.project_ref,
                account_ref = excluded.account_ref,
                quota_scope_kind = excluded.quota_scope_kind,
                quota_bucket_key = excluded.quota_bucket_key,
                independent_quota_proof = excluded.independent_quota_proof,
                budget_cost_usd_limit = excluded.budget_cost_usd_limit,
                duration_ms_limit = excluded.duration_ms_limit,
                memory_mb_limit = excluded.memory_mb_limit,
                total_tokens_limit = excluded.total_tokens_limit,
                quota_units_limit = excluded.quota_units_limit,
                warning_ratio = excluded.warning_ratio,
                critical_ratio = excluded.critical_ratio,
                requires_operator_approval_above_critical = excluded.requires_operator_approval_above_critical,
                approval_id = excluded.approval_id,
                policy_note = excluded.policy_note,
                enabled = excluded.enabled,
                record_version = resource_budget_policies.record_version + 1,
                updated_at = excluded.updated_at;
            """,
            (
                validated.policy_id,
                validated.scope_kind,
                validated.scope_ref,
                validated.provider_id,
                validated.connection_id,
                validated.model_id,
                validated.project_ref,
                validated.account_ref,
                validated.quota_scope_kind,
                validated.quota_bucket_key,
                1 if validated.independent_quota_proof else 0,
                validated.budget_cost_usd_limit,
                validated.duration_ms_limit,
                validated.memory_mb_limit,
                validated.total_tokens_limit,
                validated.quota_units_limit,
                validated.warning_ratio,
                validated.critical_ratio,
                1 if validated.requires_operator_approval_above_critical else 0,
                validated.approval_id,
                validated.policy_note,
                1 if validated.enabled else 0,
                validated.record_version,
                validated.created_by,
                validated.created_at.isoformat(),
                validated.updated_at.isoformat(),
            ),
        )
        loaded = self.get_policy(validated.scope_kind, validated.scope_ref)
        assert loaded is not None
        return loaded

    def find_ledger_by_idempotency(
        self, task_id: str, run_id: str | None, idempotency_key: str
    ) -> ResourceLedgerEntry | None:
        row = self._conn.execute(
            """
            SELECT * FROM resource_ledger_entries
            WHERE task_id = ? AND COALESCE(run_id, '') = COALESCE(?, '') AND idempotency_key = ?;
            """,
            (task_id, run_id, idempotency_key),
        ).fetchone()
        return _row_to_ledger(row) if row is not None else None

    def insert_ledger_entry(self, entry: ResourceLedgerEntry) -> ResourceLedgerEntry:
        validated = ResourceLedgerEntry.model_validate_json(entry.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO resource_ledger_entries (
                ledger_entry_id, task_id, run_id, project_id, agent_id, session_id,
                provider_id, connection_id, model_id, project_ref, account_ref,
                quota_scope_kind, quota_bucket_key, quota_independence_proven,
                duration_ms, provider_duration_ms, duration_source,
                memory_mb, peak_memory_mb, memory_source,
                input_tokens, output_tokens, thinking_tokens, cached_tokens, total_tokens, token_usage_status,
                actual_cost_usd, estimated_cost_usd, cost_source, pricing_table_ref,
                telemetry_status, is_estimated, estimate_label,
                quota_units_used, quota_limit_units, quota_remaining_units,
                idempotency_key, request_fingerprint, note, metadata_json,
                recorded_by, audit_event_id, recorded_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            );
            """,
            (
                validated.ledger_entry_id,
                validated.task_id,
                validated.run_id,
                validated.project_id,
                validated.agent_id,
                validated.session_id,
                validated.provider_id,
                validated.connection_id,
                validated.model_id,
                validated.project_ref,
                validated.account_ref,
                validated.quota_scope_kind,
                validated.quota_bucket_key,
                1 if validated.quota_independence_proven else 0,
                validated.duration_ms,
                validated.provider_duration_ms,
                validated.duration_source,
                validated.memory_mb,
                validated.peak_memory_mb,
                validated.memory_source,
                validated.input_tokens,
                validated.output_tokens,
                validated.thinking_tokens,
                validated.cached_tokens,
                validated.total_tokens,
                validated.token_usage_status,
                validated.actual_cost_usd,
                validated.estimated_cost_usd,
                validated.cost_source,
                validated.pricing_table_ref,
                validated.telemetry_status,
                1 if validated.is_estimated else 0,
                validated.estimate_label,
                validated.quota_units_used,
                validated.quota_limit_units,
                validated.quota_remaining_units,
                validated.idempotency_key,
                validated.request_fingerprint,
                validated.note,
                _dumps(validated.metadata),
                validated.recorded_by,
                validated.audit_event_id,
                validated.recorded_at.isoformat(),
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM resource_ledger_entries WHERE ledger_entry_id = ?;",
            (validated.ledger_entry_id,),
        ).fetchone()
        assert row is not None
        return _row_to_ledger(row)

    def list_matching_enabled_policies(self, entry: ResourceLedgerEntry) -> list[ResourceBudgetPolicy]:
        rows = self._conn.execute(
            "SELECT * FROM resource_budget_policies WHERE enabled = 1 ORDER BY policy_id ASC;"
        ).fetchall()
        return [p for p in (_row_to_policy(r) for r in rows) if _policy_matches_entry(p, entry)]

    def compute_scope_totals(self, policy: ResourceBudgetPolicy) -> ScopeMetricTotals:
        where_sql, params = _scope_sql_filter(policy)
        rows = self._conn.execute(
            f"SELECT * FROM resource_ledger_entries WHERE {where_sql} ORDER BY recorded_at ASC;",
            params,
        ).fetchall()
        entries = [_row_to_ledger(r) for r in rows]

        cost_vals = [
            (e.actual_cost_usd if e.actual_cost_usd is not None else e.estimated_cost_usd)
            for e in entries
            if e.actual_cost_usd is not None or e.estimated_cost_usd is not None
        ]
        total_cost = round(sum(float(v) for v in cost_vals if v is not None), 8) if cost_vals else None
        has_est_cost = any(e.estimated_cost_usd is not None for e in entries)
        est_labels = list(
            dict.fromkeys(
                e.estimate_label for e in entries if e.is_estimated and e.estimate_label is not None
            )
        )

        dur_vals = [
            e.duration_ms if e.duration_ms is not None else e.provider_duration_ms
            for e in entries
            if e.duration_ms is not None or e.provider_duration_ms is not None
        ]
        total_dur = sum(int(v) for v in dur_vals if v is not None) if dur_vals else None

        mem_vals = [
            e.peak_memory_mb if e.peak_memory_mb is not None else e.memory_mb
            for e in entries
            if e.peak_memory_mb is not None or e.memory_mb is not None
        ]
        peak_mem = max(float(v) for v in mem_vals if v is not None) if mem_vals else None

        tok_vals: list[int] = []
        for e in entries:
            if e.total_tokens is not None:
                tok_vals.append(e.total_tokens)
            elif e.input_tokens is not None or e.output_tokens is not None:
                tok_vals.append((e.input_tokens or 0) + (e.output_tokens or 0) + (e.thinking_tokens or 0))
        total_tok = sum(tok_vals) if tok_vals else None

        # Always evaluate quota across shared quota_bucket_key when independence is not proven!
        if policy.quota_bucket_key is not None and not policy.independent_quota_proof:
            quota_row = self._conn.execute(
                """
                SELECT SUM(quota_units_used) AS qsum, COUNT(quota_units_used) AS qcnt
                FROM resource_ledger_entries
                WHERE quota_bucket_key = ?;
                """,
                (policy.quota_bucket_key,),
            ).fetchone()
            total_quota = (
                int(quota_row["qsum"])
                if quota_row is not None and int(quota_row["qcnt"]) > 0 and quota_row["qsum"] is not None
                else None
            )
        else:
            q_vals = [e.quota_units_used for e in entries if e.quota_units_used is not None]
            total_quota = sum(q_vals) if q_vals else None

        return ScopeMetricTotals(
            total_cost_usd=total_cost,
            has_estimated_cost=has_est_cost,
            estimate_labels=est_labels,
            total_duration_ms=total_dur,
            peak_memory_mb=peak_mem,
            total_tokens=total_tok,
            total_quota_units_used=total_quota,
        )

    def find_active_alert(self, dedup_key: str) -> ResourceThresholdAlert | None:
        row = self._conn.execute(
            """
            SELECT * FROM resource_threshold_alerts
            WHERE dedup_key = ? AND status IN ('OPEN', 'ACKNOWLEDGED')
            LIMIT 1;
            """,
            (dedup_key,),
        ).fetchone()
        return _row_to_alert(row) if row is not None else None

    def list_open_alerts_for_policy(self, policy_id: str) -> list[ResourceThresholdAlert]:
        rows = self._conn.execute(
            """
            SELECT * FROM resource_threshold_alerts
            WHERE policy_id = ? AND status IN ('OPEN', 'ACKNOWLEDGED')
            ORDER BY created_at ASC;
            """,
            (policy_id,),
        ).fetchall()
        return [_row_to_alert(r) for r in rows]

    def get_alert_for_update(self, alert_id: str) -> ResourceThresholdAlert | None:
        row = self._conn.execute(
            "SELECT * FROM resource_threshold_alerts WHERE alert_id = ?;",
            (alert_id,),
        ).fetchone()
        return _row_to_alert(row) if row is not None else None

    def insert_alert(self, alert: ResourceThresholdAlert) -> ResourceThresholdAlert:
        validated = ResourceThresholdAlert.model_validate_json(alert.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO resource_threshold_alerts (
                alert_id, dedup_key, policy_id, alert_category, scope_kind, scope_ref,
                task_id, run_id, provider_id, connection_id, model_id, quota_bucket_key,
                severity, status, observed_value, threshold_limit, utilization_ratio,
                is_estimated_basis, estimate_label, summary, recommended_action,
                occurrence_count, notification_id, action_inbox_item_id,
                master_notified, requires_operator_approval,
                acknowledged_by, acknowledged_at, acknowledgement_note,
                resolved_by, resolved_at, resolution_note,
                audit_event_id, record_version, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            );
            """,
            (
                validated.alert_id,
                validated.dedup_key,
                validated.policy_id,
                validated.alert_category,
                validated.scope_kind,
                validated.scope_ref,
                validated.task_id,
                validated.run_id,
                validated.provider_id,
                validated.connection_id,
                validated.model_id,
                validated.quota_bucket_key,
                validated.severity,
                validated.status,
                validated.observed_value,
                validated.threshold_limit,
                validated.utilization_ratio,
                1 if validated.is_estimated_basis else 0,
                validated.estimate_label,
                validated.summary,
                validated.recommended_action,
                validated.occurrence_count,
                validated.notification_id,
                validated.action_inbox_item_id,
                1 if validated.master_notified else 0,
                1 if validated.requires_operator_approval else 0,
                validated.acknowledged_by,
                validated.acknowledged_at.isoformat() if validated.acknowledged_at else None,
                validated.acknowledgement_note,
                validated.resolved_by,
                validated.resolved_at.isoformat() if validated.resolved_at else None,
                validated.resolution_note,
                validated.audit_event_id,
                validated.record_version,
                validated.created_at.isoformat(),
                validated.updated_at.isoformat(),
            ),
        )
        loaded = self.get_alert_for_update(validated.alert_id)
        assert loaded is not None
        return loaded

    def update_alert_observation(
        self,
        *,
        alert_id: str,
        expected_record_version: int,
        severity: ResourceAlertSeverity,
        observed_value: float,
        threshold_limit: float,
        utilization_ratio: float,
        is_estimated_basis: bool,
        estimate_label: str | None,
        summary: str,
        recommended_action: str,
        occurrence_count: int,
        notification_id: str | None,
        action_inbox_item_id: str | None,
        master_notified: bool,
        requires_operator_approval: bool,
        audit_event_id: str,
        updated_at: datetime,
    ) -> ResourceThresholdAlert:
        cur = self._conn.execute(
            """
            UPDATE resource_threshold_alerts
            SET severity = ?,
                observed_value = ?,
                threshold_limit = ?,
                utilization_ratio = ?,
                is_estimated_basis = ?,
                estimate_label = ?,
                summary = ?,
                recommended_action = ?,
                occurrence_count = ?,
                notification_id = COALESCE(?, notification_id),
                action_inbox_item_id = COALESCE(?, action_inbox_item_id),
                master_notified = ?,
                requires_operator_approval = ?,
                audit_event_id = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE alert_id = ? AND record_version = ?;
            """,
            (
                severity,
                observed_value,
                threshold_limit,
                utilization_ratio,
                1 if is_estimated_basis else 0,
                estimate_label,
                summary,
                recommended_action,
                occurrence_count,
                notification_id,
                action_inbox_item_id,
                1 if master_notified else 0,
                1 if requires_operator_approval else 0,
                audit_event_id,
                updated_at.isoformat(),
                alert_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise ResourceLedgerConflictError(
                "STALE_ALERT_VERSION",
                f"Optimistic lock mismatch updating alert '{alert_id}'.",
            )
        loaded = self.get_alert_for_update(alert_id)
        assert loaded is not None
        return loaded

    def mark_alert_acknowledged(
        self,
        *,
        alert_id: str,
        expected_record_version: int,
        acknowledged_by: str,
        acknowledged_at: datetime,
        note: str,
        audit_event_id: str,
    ) -> ResourceThresholdAlert:
        cur = self._conn.execute(
            """
            UPDATE resource_threshold_alerts
            SET status = 'ACKNOWLEDGED',
                acknowledged_by = ?,
                acknowledged_at = ?,
                acknowledgement_note = ?,
                audit_event_id = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE alert_id = ? AND record_version = ?;
            """,
            (
                acknowledged_by,
                acknowledged_at.isoformat(),
                note,
                audit_event_id,
                acknowledged_at.isoformat(),
                alert_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise ResourceLedgerConflictError(
                "STALE_ALERT_VERSION",
                f"Optimistic lock mismatch acknowledging alert '{alert_id}'.",
            )
        loaded = self.get_alert_for_update(alert_id)
        assert loaded is not None
        return loaded

    def mark_alert_resolved(
        self,
        *,
        alert_id: str,
        expected_record_version: int,
        resolved_by: str,
        resolved_at: datetime,
        note: str,
        audit_event_id: str,
    ) -> ResourceThresholdAlert:
        cur = self._conn.execute(
            """
            UPDATE resource_threshold_alerts
            SET status = 'RESOLVED',
                resolved_by = ?,
                resolved_at = ?,
                resolution_note = ?,
                audit_event_id = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE alert_id = ? AND record_version = ?;
            """,
            (
                resolved_by,
                resolved_at.isoformat(),
                note,
                audit_event_id,
                resolved_at.isoformat(),
                alert_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise ResourceLedgerConflictError(
                "STALE_ALERT_VERSION",
                f"Optimistic lock mismatch resolving alert '{alert_id}'.",
            )
        loaded = self.get_alert_for_update(alert_id)
        assert loaded is not None
        return loaded

    def save_notification(self, notification: NotificationRef) -> NotificationRef:
        validated = NotificationRef.model_validate_json(notification.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO notifications (
                notification_id, task_id, run_id, event_id, severity, notification_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                validated.notification_id,
                validated.task_id,
                validated.run_id,
                validated.event_id,
                validated.severity,
                validated.notification_type,
                validated.created_at.isoformat(),
            ),
        )
        return validated

    def insert_action_inbox_item(
        self, item: ActionInboxItem, *, audit_event_id: str
    ) -> ActionInboxItem:
        validated = ActionInboxItem.model_validate_json(item.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO action_inbox_items (
                inbox_item_id, intake_id, conversation_id, item_kind, priority, status,
                summary, task_id, run_id, approval_id, assigned_route_json, audit_event_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                validated.inbox_item_id,
                validated.intake_id,
                validated.conversation_id,
                validated.item_kind,
                validated.priority,
                validated.status,
                validated.summary,
                validated.task_id,
                validated.run_id,
                validated.approval_id,
                _dumps(validated.assigned_route),
                audit_event_id,
                validated.created_at.isoformat(),
                validated.created_at.isoformat(),
            ),
        )
        return validated

    def update_action_inbox_status(
        self, inbox_item_id: str, *, status: ActionInboxStatus, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE action_inbox_items SET status = ?, updated_at = ? WHERE inbox_item_id = ?;",
            (status, updated_at.isoformat(), inbox_item_id),
        )

    def append_event(self, event: Event) -> Event:
        res = self._store.events.append_on_connection(self._conn, event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Audit event append rejected."
            raise ResourceLedgerPersistenceError("AUDIT_EVENT_REJECTED", msg)
        return res.entry.event


class SQLiteResourcesRepository:
    """SQLite repository implementing durable P11.04 resource ledger, policies, and alerts."""

    def __init__(
        self,
        store: SQLiteRepositoryStore,
        *,
        before_commit_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Bind repository to ``SQLiteRepositoryStore``."""
        self._store = store
        self._before_commit_hook = before_commit_hook

    def execute_transaction(
        self,
        processor: Callable[[ResourceLedgerTransactionContext], Any],
    ) -> Any:
        """Execute ``processor`` inside one atomic ``BEGIN IMMEDIATE`` SQLite transaction."""
        try:
            with self._store.transaction_scope() as conn:
                tx = _SQLiteResourcesTransactionContext(self._store, conn)
                result = processor(tx)
                if self._before_commit_hook is not None:
                    self._before_commit_hook(conn)
                return result
        except (sqlite3.Error, RepositoryError) as exc:
            raise ResourceLedgerPersistenceError(
                "RESOURCE_LEDGER_PERSISTENCE_FAILED",
                f"Atomic resource ledger transaction failed and rolled back: {exc}",
            ) from exc

    def list_policies(self, *, enabled_only: bool = False) -> list[ResourceBudgetPolicy]:
        """List registered resource budget policies deterministically."""
        sql = "SELECT * FROM resource_budget_policies"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at ASC, policy_id ASC;"
        with self._store.open_connection() as conn:
            return [_row_to_policy(r) for r in conn.execute(sql).fetchall()]

    def list_ledger_entries(self, query: ResourceLedgerQuery) -> list[ResourceLedgerEntry]:
        """Query persisted immutable resource ledger rows."""
        clauses: list[str] = []
        params: list[Any] = []
        if query.task_id is not None:
            clauses.append("task_id = ?")
            params.append(query.task_id)
        if query.run_id is not None:
            clauses.append("run_id = ?")
            params.append(query.run_id)
        if query.provider_id is not None:
            clauses.append("provider_id = ?")
            params.append(query.provider_id.lower())
        if query.connection_id is not None:
            clauses.append("connection_id = ?")
            params.append(query.connection_id.lower())
        if query.model_id is not None:
            clauses.append("model_id = ?")
            params.append(query.model_id.lower())
        if query.quota_bucket_key is not None:
            clauses.append("quota_bucket_key = ?")
            params.append(query.quota_bucket_key)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM resource_ledger_entries {where_sql} ORDER BY recorded_at ASC, ledger_entry_id ASC;",
                params,
            ).fetchall()
            return [_row_to_ledger(r) for r in rows]

    def list_alerts(
        self, *, scope_ref: str | None = None, include_resolved: bool = False
    ) -> list[ResourceThresholdAlert]:
        """List persisted budget and quota threshold alerts."""
        clauses: list[str] = []
        params: list[Any] = []
        if scope_ref is not None:
            clauses.append("scope_ref = ?")
            params.append(scope_ref)
        if not include_resolved:
            clauses.append("status IN ('OPEN', 'ACKNOWLEDGED')")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM resource_threshold_alerts {where_sql} ORDER BY created_at ASC, alert_id ASC;",
                params,
            ).fetchall()
            return [_row_to_alert(r) for r in rows]

    def get_task(self, task_id: str) -> Task | None:
        """Fetch persisted Task without mutating state."""
        with self._store.open_connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?;", (task_id,)).fetchone()
            return _row_to_task(row) if row is not None else None

    def summarize_task(self, task_id: str) -> TaskResourceUsageSummary | None:
        """Compile truthful aggregated Task resource summary never zero-coercing None metrics."""
        with self._store.open_connection() as conn:
            task_row = conn.execute("SELECT task_id FROM tasks WHERE task_id = ?;", (task_id,)).fetchone()
            if task_row is None:
                return None
            rows = conn.execute(
                "SELECT * FROM resource_ledger_entries WHERE task_id = ? ORDER BY recorded_at ASC;",
                (task_id,),
            ).fetchall()
            entries = [_row_to_ledger(r) for r in rows]
            alert_rows = conn.execute(
                """
                SELECT severity FROM resource_threshold_alerts
                WHERE status IN ('OPEN', 'ACKNOWLEDGED')
                  AND (task_id = ? OR (scope_kind = 'TASK' AND scope_ref = ?));
                """,
                (task_id, task_id),
            ).fetchall()

        dur_vals = [
            e.duration_ms if e.duration_ms is not None else e.provider_duration_ms
            for e in entries
            if e.duration_ms is not None or e.provider_duration_ms is not None
        ]
        mem_vals = [
            e.peak_memory_mb if e.peak_memory_mb is not None else e.memory_mb
            for e in entries
            if e.peak_memory_mb is not None or e.memory_mb is not None
        ]
        in_tok = [e.input_tokens for e in entries if e.input_tokens is not None]
        out_tok = [e.output_tokens for e in entries if e.output_tokens is not None]
        tot_tok = [e.total_tokens for e in entries if e.total_tokens is not None]
        actual_costs = [e.actual_cost_usd for e in entries if e.actual_cost_usd is not None]
        est_costs = [e.estimated_cost_usd for e in entries if e.estimated_cost_usd is not None]
        quota_vals = [e.quota_units_used for e in entries if e.quota_units_used is not None]

        return TaskResourceUsageSummary(
            task_id=task_id,
            entry_count=len(entries),
            run_ids=list(dict.fromkeys(e.run_id for e in entries if e.run_id is not None)),
            providers=list(dict.fromkeys(e.provider_id for e in entries)),
            connections=list(dict.fromkeys(e.connection_id for e in entries if e.connection_id is not None)),
            models=list(dict.fromkeys(e.model_id for e in entries if e.model_id is not None)),
            quota_buckets=list(dict.fromkeys(e.quota_bucket_key for e in entries)),
            measured_duration_ms_total=sum(int(v) for v in dur_vals if v is not None) if dur_vals else None,
            peak_memory_mb_max=max(float(v) for v in mem_vals if v is not None) if mem_vals else None,
            measured_input_tokens_total=sum(in_tok) if in_tok else None,
            measured_output_tokens_total=sum(out_tok) if out_tok else None,
            measured_total_tokens_total=sum(tot_tok) if tot_tok else None,
            actual_cost_usd_total=round(sum(actual_costs), 8) if actual_costs else None,
            estimated_cost_usd_total=round(sum(est_costs), 8) if est_costs else None,
            has_estimated_entries=any(e.is_estimated for e in entries),
            estimate_labels=list(
                dict.fromkeys(e.estimate_label for e in entries if e.estimate_label is not None)
            ),
            has_unavailable_duration_entries=any(e.duration_source == "UNAVAILABLE" for e in entries),
            has_unavailable_memory_entries=any(e.memory_source == "UNAVAILABLE" for e in entries),
            has_unavailable_token_entries=any(
                e.token_usage_status == "UNAVAILABLE_FROM_PROVIDER" for e in entries
            ),
            has_unavailable_cost_entries=any(
                e.cost_source == "UNAVAILABLE_FROM_PROVIDER" for e in entries
            ),
            quota_units_used_total=sum(quota_vals) if quota_vals else None,
            open_alert_count=len(alert_rows),
            critical_alert_count=sum(1 for r in alert_rows if str(r["severity"]) == "CRITICAL"),
        )

    def get_bucket_summary(self, quota_bucket_key: str) -> BucketQuotaSummary | None:
        """Aggregate shared/unproven quota bucket consumption without pretending key independence."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM resource_ledger_entries WHERE quota_bucket_key = ? ORDER BY recorded_at ASC;",
                (quota_bucket_key,),
            ).fetchall()
            if not rows:
                return None
            entries = [_row_to_ledger(r) for r in rows]
            alert_count_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM resource_threshold_alerts
                WHERE quota_bucket_key = ? AND status IN ('OPEN', 'ACKNOWLEDGED');
                """,
                (quota_bucket_key,),
            ).fetchone()

        q_vals = [e.quota_units_used for e in entries if e.quota_units_used is not None]
        lim_vals = [e.quota_limit_units for e in entries if e.quota_limit_units is not None]
        rem_vals = [e.quota_remaining_units for e in entries if e.quota_remaining_units is not None]
        proven = any(e.quota_independence_proven for e in entries)
        first = entries[0]
        return BucketQuotaSummary(
            quota_bucket_key=quota_bucket_key,
            provider_id=first.provider_id,
            quota_scope_kind=first.quota_scope_kind,
            quota_independence_proven=proven,
            quota_independence_status="VERIFIED_INDEPENDENT" if proven else "QUOTA_SHARED_OR_UNPROVEN",
            connections=list(dict.fromkeys(e.connection_id for e in entries if e.connection_id is not None)),
            task_ids=list(dict.fromkeys(e.task_id for e in entries)),
            entry_count=len(entries),
            quota_units_used_total=sum(q_vals) if q_vals else None,
            latest_quota_limit_units=lim_vals[-1] if lim_vals else None,
            latest_quota_remaining_units=rem_vals[-1] if rem_vals else None,
            open_alert_count=int(alert_count_row["cnt"]) if alert_count_row is not None else 0,
        )
