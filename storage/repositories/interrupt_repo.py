"""SQLite repository implementing durable P11.02 Interrupt classification, routing, dedup, ACK & escalation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
import sqlite3
from typing import Any

from core.contracts.event import Event
from core.contracts.references import NotificationRef
from core.contracts.task import Task
from core.departments.interrupt_models import (
    AgentAvailabilitySnapshot,
    InterruptClassification,
    InterruptEscalationRecord,
    InterruptEventRecord,
    InterruptImpact,
    InterruptIncidentRecord,
    InterruptPersistenceError,
    InterruptPriority,
    InterruptResolutionOutcome,
    InterruptSeverity,
    InterruptStatus,
    InterruptSubmitOutcome,
    InterruptTransactionContext,
    InterruptUrgency,
    RoutingTarget,
    StaleInterruptVersionError,
)
from core.departments.reception_models import ActionInboxItem, ActionInboxStatus
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore, _row_to_task


def _json_dumps(value: Any) -> str:
    """Serialize compact deterministic JSON payloads for SQLite storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row_to_incident(row: sqlite3.Row) -> InterruptIncidentRecord:
    """Rehydrate and validate ``InterruptIncidentRecord`` from SQLite row."""
    raw_targets = json.loads(str(row["routing_targets_json"]))
    targets = [RoutingTarget.model_validate(item) for item in raw_targets]
    return InterruptIncidentRecord.model_validate(
        {
            "incident_id": row["incident_id"],
            "interrupt_id": row["interrupt_id"],
            "dedup_scope": row["dedup_scope"],
            "incident_key": row["incident_key"],
            "request_fingerprint": row["request_fingerprint"],
            "classification": row["classification"],
            "severity": row["severity"],
            "priority": row["priority"],
            "urgency": row["urgency"],
            "impact": row["impact"],
            "status": row["status"],
            "source": row["source"],
            "event_category": row["event_category"],
            "summary": row["summary"],
            "details": row["details"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "conversation_id": row["conversation_id"],
            "approval_id": row["approval_id"],
            "routing_targets": targets,
            "broadcast_all_departments": bool(int(row["broadcast_all_departments"])),
            "master_notified": bool(int(row["master_notified"])),
            "action_inbox_item_id": row["action_inbox_item_id"],
            "notification_id": row["notification_id"],
            "occurrence_count": int(row["occurrence_count"]),
            "policy_rationale": row["policy_rationale"],
            "acknowledged_by": row["acknowledged_by"],
            "acknowledged_at": row["acknowledged_at"],
            "acknowledgement_note": row["acknowledgement_note"],
            "resolved_by": row["resolved_by"],
            "resolved_at": row["resolved_at"],
            "resolution_outcome": row["resolution_outcome"],
            "resolution_note": row["resolution_note"],
            "escalation_count": int(row["escalation_count"]),
            "submitted_by_actor_id": row["submitted_by_actor_id"],
            "submitted_by_actor_kind": row["submitted_by_actor_kind"],
            "audit_event_id": row["audit_event_id"],
            "record_version": int(row["record_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _row_to_interrupt_event(row: sqlite3.Row) -> InterruptEventRecord:
    """Rehydrate immutable ``InterruptEventRecord`` from SQLite row."""
    return InterruptEventRecord.model_validate(
        {
            "interrupt_event_id": row["interrupt_event_id"],
            "incident_id": row["incident_id"],
            "dedup_scope": row["dedup_scope"],
            "event_dedup_key": row["event_dedup_key"],
            "request_fingerprint": row["request_fingerprint"],
            "source": row["source"],
            "event_category": row["event_category"],
            "dedup_reason": row["dedup_reason"],
            "deduplicated": bool(int(row["deduplicated"])),
            "summary": row["summary"],
            "notification_suppressed": bool(int(row["notification_suppressed"])),
            "notification_id": row["notification_id"],
            "submitted_by_actor_id": row["submitted_by_actor_id"],
            "audit_event_id": row["audit_event_id"],
            "created_at": row["created_at"],
        }
    )


def _row_to_escalation(row: sqlite3.Row) -> InterruptEscalationRecord:
    """Rehydrate immutable ``InterruptEscalationRecord`` from SQLite row."""
    added = [RoutingTarget.model_validate(item) for item in json.loads(str(row["added_targets_json"]))]
    return InterruptEscalationRecord.model_validate(
        {
            "escalation_id": row["escalation_id"],
            "incident_id": row["incident_id"],
            "from_classification": row["from_classification"],
            "to_classification": row["to_classification"],
            "from_severity": row["from_severity"],
            "to_severity": row["to_severity"],
            "escalated_by": row["escalated_by"],
            "reason": row["reason"],
            "added_targets": added,
            "notification_id": row["notification_id"],
            "audit_event_id": row["audit_event_id"],
            "created_at": row["created_at"],
        }
    )


def _check_agent_availability_conn(
    conn: sqlite3.Connection,
    agent_id: str,
    *,
    task_id: str | None = None,
) -> AgentAvailabilitySnapshot:
    """Inspect real SQLite agent_registry, tasks, and active sessions without assuming online."""
    reg = conn.execute(
        "SELECT registration_status, department FROM agent_registry WHERE agent_id = ?;",
        (agent_id,),
    ).fetchone()
    registered = reg is not None
    reg_status = str(reg["registration_status"]) if reg is not None else None
    dept = str(reg["department"]) if reg is not None else None

    if agent_id == "core.master" and not registered:
        # Master core coordinator identity exists even before optional registry bootstrap.
        registered = True
        reg_status = "ACTIVE"
        dept = "orchestration"

    sess_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM sessions WHERE agent_id = ? AND status = 'ACTIVE';",
        (agent_id,),
    ).fetchone()
    active_sessions = int(sess_row["cnt"]) if sess_row is not None else 0

    assigned = False
    if task_id is not None:
        task_row = conn.execute(
            "SELECT assigned_agent_ids_json FROM tasks WHERE task_id = ?;",
            (task_id,),
        ).fetchone()
        if task_row is not None:
            assigned_list = json.loads(str(task_row["assigned_agent_ids_json"]))
            assigned = agent_id in assigned_list

    return AgentAvailabilitySnapshot(
        agent_id=agent_id,
        registered=registered,
        registration_status=reg_status,
        department=dept,
        active_session_count=active_sessions,
        online_confirmed=registered and reg_status == "ACTIVE" and active_sessions > 0,
        assigned_to_task=assigned,
    )


class _SQLiteInterruptTransactionContext:
    """Transaction-scoped SQLite implementation of ``InterruptTransactionContext``."""

    def __init__(self, store: SQLiteRepositoryStore, conn: sqlite3.Connection) -> None:
        self._store = store
        self._conn = conn

    def find_event_by_dedup_key(
        self,
        dedup_scope: str,
        event_dedup_key: str,
    ) -> tuple[str, InterruptSubmitOutcome] | None:
        row = self._conn.execute(
            """
            SELECT request_fingerprint, outcome_json
            FROM interrupt_events
            WHERE dedup_scope = ? AND event_dedup_key = ?;
            """,
            (dedup_scope, event_dedup_key),
        ).fetchone()
        if row is None:
            return None
        return (
            str(row["request_fingerprint"]),
            InterruptSubmitOutcome.model_validate_json(str(row["outcome_json"])),
        )

    def find_active_incident_by_key(
        self,
        dedup_scope: str,
        incident_key: str,
    ) -> InterruptIncidentRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM interrupt_incidents
            WHERE dedup_scope = ? AND incident_key = ?
              AND status NOT IN ('RESOLVED', 'DISMISSED')
            ORDER BY created_at DESC LIMIT 1;
            """,
            (dedup_scope, incident_key),
        ).fetchone()
        return _row_to_incident(row) if row is not None else None

    def find_latest_incident_by_key(
        self,
        dedup_scope: str,
        incident_key: str,
    ) -> InterruptIncidentRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM interrupt_incidents
            WHERE dedup_scope = ? AND incident_key = ?
            ORDER BY created_at DESC, incident_id DESC LIMIT 1;
            """,
            (dedup_scope, incident_key),
        ).fetchone()
        return _row_to_incident(row) if row is not None else None

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?;",
            (task_id,),
        ).fetchone()
        return _row_to_task(row) if row is not None else None

    def get_run_task_id(self, run_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT task_id FROM runs WHERE run_id = ?;",
            (run_id,),
        ).fetchone()
        return str(row["task_id"]) if row is not None else None

    def get_conversation_owner(self, conversation_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT owner_actor_id FROM conversations WHERE conversation_id = ?;",
            (conversation_id,),
        ).fetchone()
        return str(row["owner_actor_id"]) if row is not None else None

    def check_agent_availability(
        self,
        agent_id: str,
        *,
        task_id: str | None = None,
    ) -> AgentAvailabilitySnapshot:
        return _check_agent_availability_conn(self._conn, agent_id, task_id=task_id)

    def list_department_agents(
        self,
        department_id: str,
        *,
        task_id: str | None = None,
    ) -> list[AgentAvailabilitySnapshot]:
        rows = self._conn.execute(
            """
            SELECT agent_id FROM agent_registry
            WHERE department = ? AND registration_status = 'ACTIVE'
            ORDER BY agent_id ASC;
            """,
            (department_id,),
        ).fetchall()
        return [
            _check_agent_availability_conn(self._conn, str(r["agent_id"]), task_id=task_id)
            for r in rows
        ]

    def append_event(self, event: Event) -> Event:
        res = self._store.events.append_on_connection(self._conn, event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Audit event append rejected."
            raise InterruptPersistenceError("AUDIT_EVENT_REJECTED", msg)
        return res.entry.event

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
        self,
        item: ActionInboxItem,
        *,
        audit_event_id: str,
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
                _json_dumps(validated.assigned_route),
                audit_event_id,
                validated.created_at.isoformat(),
                validated.created_at.isoformat(),
            ),
        )
        return validated

    def update_action_inbox_status(
        self,
        inbox_item_id: str,
        *,
        status: ActionInboxStatus,
        updated_at: datetime,
    ) -> None:
        self._conn.execute(
            "UPDATE action_inbox_items SET status = ?, updated_at = ? WHERE inbox_item_id = ?;",
            (status, updated_at.isoformat(), inbox_item_id),
        )

    def insert_incident(self, incident: InterruptIncidentRecord) -> InterruptIncidentRecord:
        validated = InterruptIncidentRecord.model_validate_json(incident.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO interrupt_incidents (
                incident_id, interrupt_id, dedup_scope, incident_key, request_fingerprint,
                classification, severity, priority, urgency, impact, status,
                source, event_category, summary, details,
                task_id, run_id, conversation_id, approval_id,
                routing_targets_json, broadcast_all_departments, master_notified,
                action_inbox_item_id, notification_id, occurrence_count, policy_rationale,
                acknowledged_by, acknowledged_at, acknowledgement_note,
                resolved_by, resolved_at, resolution_outcome, resolution_note,
                escalation_count, submitted_by_actor_id, submitted_by_actor_kind,
                audit_event_id, record_version, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            );
            """,
            (
                validated.incident_id,
                validated.interrupt_id,
                validated.dedup_scope,
                validated.incident_key,
                validated.request_fingerprint,
                validated.classification,
                validated.severity,
                validated.priority,
                validated.urgency,
                validated.impact,
                validated.status,
                validated.source,
                validated.event_category,
                validated.summary,
                validated.details,
                validated.task_id,
                validated.run_id,
                validated.conversation_id,
                validated.approval_id,
                _json_dumps([t.model_dump(mode="json") for t in validated.routing_targets]),
                0,
                1 if validated.master_notified else 0,
                validated.action_inbox_item_id,
                validated.notification_id,
                validated.occurrence_count,
                validated.policy_rationale,
                validated.acknowledged_by,
                validated.acknowledged_at.isoformat() if validated.acknowledged_at else None,
                validated.acknowledgement_note,
                validated.resolved_by,
                validated.resolved_at.isoformat() if validated.resolved_at else None,
                validated.resolution_outcome,
                validated.resolution_note,
                validated.escalation_count,
                validated.submitted_by_actor_id,
                validated.submitted_by_actor_kind,
                validated.audit_event_id,
                validated.record_version,
                validated.created_at.isoformat(),
                validated.updated_at.isoformat(),
            ),
        )
        loaded = self.get_incident_for_update(validated.incident_id)
        assert loaded is not None
        return loaded

    def append_interrupt_event(
        self,
        event_record: InterruptEventRecord,
        *,
        outcome: InterruptSubmitOutcome,
    ) -> InterruptEventRecord:
        validated = InterruptEventRecord.model_validate_json(event_record.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO interrupt_events (
                interrupt_event_id, incident_id, dedup_scope, event_dedup_key,
                request_fingerprint, source, event_category, dedup_reason, deduplicated,
                summary, notification_suppressed, notification_id,
                submitted_by_actor_id, outcome_json, audit_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                validated.interrupt_event_id,
                validated.incident_id,
                validated.dedup_scope,
                validated.event_dedup_key,
                validated.request_fingerprint,
                validated.source,
                validated.event_category,
                validated.dedup_reason,
                1 if validated.deduplicated else 0,
                validated.summary,
                1 if validated.notification_suppressed else 0,
                validated.notification_id,
                validated.submitted_by_actor_id,
                outcome.model_dump_json(),
                validated.audit_event_id,
                validated.created_at.isoformat(),
            ),
        )
        return validated

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
        cur = self._conn.execute(
            """
            UPDATE interrupt_incidents
            SET occurrence_count = ?,
                classification = ?,
                severity = ?,
                priority = ?,
                urgency = ?,
                impact = ?,
                routing_targets_json = ?,
                master_notified = ?,
                action_inbox_item_id = COALESCE(?, action_inbox_item_id),
                audit_event_id = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE incident_id = ? AND record_version = ?;
            """,
            (
                occurrence_count,
                classification,
                severity,
                priority,
                urgency,
                impact,
                _json_dumps([t.model_dump(mode="json") for t in routing_targets]),
                1 if master_notified else 0,
                action_inbox_item_id,
                audit_event_id,
                updated_at.isoformat(),
                incident_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise StaleInterruptVersionError(
                "STALE_INTERRUPT_VERSION",
                f"Optimistic concurrency conflict aggregating incident '{incident_id}'.",
            )
        loaded = self.get_incident_for_update(incident_id)
        assert loaded is not None
        return loaded

    def get_incident_for_update(self, incident_id: str) -> InterruptIncidentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM interrupt_incidents WHERE incident_id = ?;",
            (incident_id,),
        ).fetchone()
        return _row_to_incident(row) if row is not None else None

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
        cur = self._conn.execute(
            """
            UPDATE interrupt_incidents
            SET status = 'ACKNOWLEDGED',
                acknowledged_by = ?,
                acknowledged_at = ?,
                acknowledgement_note = ?,
                audit_event_id = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE incident_id = ? AND record_version = ?;
            """,
            (
                acknowledged_by,
                acknowledged_at.isoformat(),
                note,
                audit_event_id,
                updated_at.isoformat(),
                incident_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise StaleInterruptVersionError(
                "STALE_INTERRUPT_VERSION",
                f"Optimistic concurrency conflict acknowledging incident '{incident_id}'.",
            )
        loaded = self.get_incident_for_update(incident_id)
        assert loaded is not None
        return loaded

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
        validated_esc = InterruptEscalationRecord.model_validate_json(escalation.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO interrupt_escalations (
                escalation_id, incident_id, from_classification, to_classification,
                from_severity, to_severity, escalated_by, reason, added_targets_json,
                notification_id, audit_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                validated_esc.escalation_id,
                validated_esc.incident_id,
                validated_esc.from_classification,
                validated_esc.to_classification,
                validated_esc.from_severity,
                validated_esc.to_severity,
                validated_esc.escalated_by,
                validated_esc.reason,
                _json_dumps([t.model_dump(mode="json") for t in validated_esc.added_targets]),
                validated_esc.notification_id,
                validated_esc.audit_event_id,
                validated_esc.created_at.isoformat(),
            ),
        )
        cur = self._conn.execute(
            """
            UPDATE interrupt_incidents
            SET status = 'ESCALATED',
                classification = ?,
                severity = ?,
                priority = ?,
                urgency = ?,
                routing_targets_json = ?,
                master_notified = ?,
                notification_id = COALESCE(?, notification_id),
                escalation_count = escalation_count + 1,
                audit_event_id = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE incident_id = ? AND record_version = ?;
            """,
            (
                classification,
                severity,
                priority,
                urgency,
                _json_dumps([t.model_dump(mode="json") for t in routing_targets]),
                1 if master_notified else 0,
                notification_id,
                audit_event_id,
                updated_at.isoformat(),
                incident_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise StaleInterruptVersionError(
                "STALE_INTERRUPT_VERSION",
                f"Optimistic concurrency conflict escalating incident '{incident_id}'.",
            )
        loaded = self.get_incident_for_update(incident_id)
        assert loaded is not None
        return loaded

    def update_incident_resolution(
        self,
        *,
        incident_id: str,
        expected_record_version: int,
        status: str,
        resolved_by: str,
        resolved_at: datetime,
        outcome: InterruptResolutionOutcome,
        note: str,
        audit_event_id: str,
        updated_at: datetime,
    ) -> InterruptIncidentRecord:
        cur = self._conn.execute(
            """
            UPDATE interrupt_incidents
            SET status = ?,
                resolved_by = ?,
                resolved_at = ?,
                resolution_outcome = ?,
                resolution_note = ?,
                audit_event_id = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE incident_id = ? AND record_version = ?;
            """,
            (
                status,
                resolved_by,
                resolved_at.isoformat(),
                outcome,
                note,
                audit_event_id,
                updated_at.isoformat(),
                incident_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise StaleInterruptVersionError(
                "STALE_INTERRUPT_VERSION",
                f"Optimistic concurrency conflict resolving incident '{incident_id}'.",
            )
        loaded = self.get_incident_for_update(incident_id)
        assert loaded is not None
        return loaded


class SQLiteInterruptRepository:
    """SQLite adapter backing persistent P11.02 interrupt classification, routing, dedup & escalations."""

    def __init__(
        self,
        store: SQLiteRepositoryStore,
        *,
        before_commit_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Bind repository to ``SQLiteRepositoryStore`` with optional rollback fault-injection hook."""
        self._store = store
        self._before_commit_hook = before_commit_hook

    def execute_transaction(
        self,
        processor: Callable[[InterruptTransactionContext], Any],
    ) -> Any:
        """Execute ``processor`` inside one atomic ``BEGIN IMMEDIATE`` SQLite transaction."""
        try:
            with self._store.transaction_scope() as conn:
                tx = _SQLiteInterruptTransactionContext(self._store, conn)
                result = processor(tx)
                if self._before_commit_hook is not None:
                    self._before_commit_hook(conn)
                return result
        except (sqlite3.Error, RepositoryError) as exc:
            raise InterruptPersistenceError(
                "INTERRUPT_PERSISTENCE_FAILED",
                f"Atomic interrupt transaction failed and rolled back: {exc}",
            ) from exc

    def check_agent(self, agent_id: str, *, task_id: str | None = None) -> AgentAvailabilitySnapshot:
        """Read-only implementation of ``AgentAvailabilityPort.check_agent``."""
        with self._store.open_connection() as conn:
            return _check_agent_availability_conn(conn, agent_id, task_id=task_id)

    def list_department_agents(
        self,
        department_id: str,
        *,
        task_id: str | None = None,
    ) -> list[AgentAvailabilitySnapshot]:
        """Read-only implementation of ``AgentAvailabilityPort.list_department_agents``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT agent_id FROM agent_registry
                WHERE department = ? AND registration_status = 'ACTIVE'
                ORDER BY agent_id ASC;
                """,
                (department_id,),
            ).fetchall()
            return [
                _check_agent_availability_conn(conn, str(r["agent_id"]), task_id=task_id)
                for r in rows
            ]

    def get_incident(self, incident_id: str) -> InterruptIncidentRecord | None:
        """Fetch persisted ``InterruptIncidentRecord`` by ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM interrupt_incidents WHERE incident_id = ?;",
                (incident_id,),
            ).fetchone()
            return _row_to_incident(row) if row is not None else None

    def list_incidents(
        self,
        *,
        task_id: str | None = None,
        status: InterruptStatus | None = None,
        severity: InterruptSeverity | None = None,
        classification: InterruptClassification | None = None,
    ) -> list[InterruptIncidentRecord]:
        """List persisted interrupt incidents matching optional filters."""
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        if classification is not None:
            clauses.append("classification = ?")
            params.append(classification)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = f"SELECT * FROM interrupt_incidents {where_sql}ORDER BY created_at ASC, incident_id ASC;"
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_incident(row) for row in rows]

    def list_interrupt_events(self, incident_id: str) -> list[InterruptEventRecord]:
        """List immutable interrupt event history entries for one Incident."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM interrupt_events
                WHERE incident_id = ?
                ORDER BY created_at ASC, interrupt_event_id ASC;
                """,
                (incident_id,),
            ).fetchall()
            return [_row_to_interrupt_event(row) for row in rows]

    def list_escalations(self, incident_id: str) -> list[InterruptEscalationRecord]:
        """List immutable escalation history records for one Incident."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM interrupt_escalations
                WHERE incident_id = ?
                ORDER BY created_at ASC, escalation_id ASC;
                """,
                (incident_id,),
            ).fetchall()
            return [_row_to_escalation(row) for row in rows]
