"""SQLite repository implementing durable conversations, concurrent intake, refinements, and Action Inbox (P11.01)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
import sqlite3
from typing import Any

from core.contracts.event import Event
from core.contracts.identifiers import validate_safe_payload
from core.contracts.references import NotificationRef
from core.contracts.task import Task
from core.departments.reception_models import (
    ActionInboxItem,
    ActionInboxStatus,
    ConcurrentIntakeClassification,
    ConversationLinkRole,
    ConversationRecord,
    ConversationTurnRecord,
    DedupCounterRecord,
    IntakeDecision,
    IntakeProcessingStatus,
    IntakeRecord,
    IntakeRecoverySnapshot,
    MasterHandoffStatus,
    ReceptionPersistenceError,
    ReceptionTransactionContext,
    RecoveredIntakeEntry,
    StaleIntakeVersionError,
    TaskRefinementRecord,
    UserRequestIntakeOutcome,
)
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore, _row_to_task

_PRIO_RANK: dict[str, int] = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _json_dumps(value: Any) -> str:
    """Serialize compact deterministic JSON payloads for SQLite storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row_to_conversation(conn: sqlite3.Connection, row: sqlite3.Row) -> ConversationRecord:
    """Rehydrate ``ConversationRecord`` including ordered linked Task IDs."""
    links = conn.execute(
        """
        SELECT task_id FROM conversation_task_links
        WHERE conversation_id = ?
        ORDER BY linked_at ASC, task_id ASC;
        """,
        (str(row["conversation_id"]),),
    ).fetchall()
    return ConversationRecord.model_validate(
        {
            "conversation_id": row["conversation_id"],
            "project_id": row["project_id"],
            "owner_actor_id": row["owner_actor_id"],
            "title": row["title"],
            "status": row["status"],
            "record_version": int(row["record_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "linked_task_ids": [str(link["task_id"]) for link in links],
        }
    )


def _row_to_turn(row: sqlite3.Row) -> ConversationTurnRecord:
    """Rehydrate immutable ``ConversationTurnRecord`` from SQLite row."""
    return ConversationTurnRecord.model_validate(
        {
            "turn_id": row["turn_id"],
            "conversation_id": row["conversation_id"],
            "turn_index": int(row["turn_index"]),
            "intake_id": row["intake_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "sender_actor_id": row["sender_actor_id"],
            "sender_kind": row["sender_kind"],
            "role": row["role"],
            "message_text": row["message_text"],
            "audit_event_id": row["audit_event_id"],
            "created_at": row["created_at"],
        }
    )


def _row_to_refinement(row: sqlite3.Row) -> TaskRefinementRecord:
    """Rehydrate immutable ``TaskRefinementRecord`` from SQLite row."""
    return TaskRefinementRecord.model_validate(
        {
            "refinement_id": row["refinement_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "conversation_id": row["conversation_id"],
            "intake_id": row["intake_id"],
            "sequence_in_task": int(row["sequence_in_task"]),
            "source_channel": row["source_channel"],
            "submitted_by": row["submitted_by"],
            "refinement_text": row["refinement_text"],
            "metadata": json.loads(str(row["metadata_json"])),
            "audit_event_id": row["audit_event_id"],
            "created_at": row["created_at"],
        }
    )


def _row_to_inbox_item(row: sqlite3.Row) -> ActionInboxItem:
    """Rehydrate ``ActionInboxItem`` from SQLite row."""
    return ActionInboxItem.model_validate(
        {
            "inbox_item_id": row["inbox_item_id"],
            "item_kind": row["item_kind"],
            "priority": row["priority"],
            "status": row["status"],
            "intake_id": row["intake_id"],
            "conversation_id": row["conversation_id"],
            "summary": row["summary"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "approval_id": row["approval_id"],
            "assigned_route": json.loads(str(row["assigned_route_json"])),
            "created_at": row["created_at"],
        }
    )


def _row_to_intake(row: sqlite3.Row) -> IntakeRecord:
    """Rehydrate ``IntakeRecord`` from SQLite ``intake_requests`` row."""
    extra_payload = json.loads(str(row["payload_json"]))
    questions = extra_payload.get("clarification_questions", [])
    reason_code = extra_payload.get("reason_code")
    return IntakeRecord.model_validate(
        {
            "intake_id": row["intake_id"],
            "conversation_id": row["conversation_id"],
            "idempotency_scope": row["idempotency_scope"],
            "idempotency_key": row["idempotency_key"],
            "request_fingerprint": row["request_fingerprint"],
            "source_channel": row["source_channel"],
            "submitted_by_actor_id": row["submitted_by_actor_id"],
            "submitted_by_actor_kind": row["submitted_by_actor_kind"],
            "intent_mode": row["intent_mode"],
            "classification": row["classification"],
            "decision": row["decision"],
            "processing_status": row["processing_status"],
            "summary": row["summary"],
            "reason_code": reason_code,
            "clarification_questions": questions if isinstance(questions, list) else [],
            "related_task_id": row["related_task_id"],
            "created_task_id": row["created_task_id"],
            "observed_running_task_id": row["observed_running_task_id"],
            "run_id": row["run_id"],
            "approval_id": row["approval_id"],
            "route_targets": json.loads(str(row["route_targets_json"])),
            "escalated_to_master": bool(row["escalated_to_master"]),
            "master_handoff_status": row["master_handoff_status"],
            "master_decision_note": row["master_decision_note"],
            "master_decided_by": row["master_decided_by"],
            "master_decided_at": row["master_decided_at"],
            "correlation_id": row["correlation_id"],
            "audit_event_id": row["audit_event_id"],
            "record_version": int(row["record_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


class _SQLiteReceptionTransactionContext:
    """Concrete SQLite primitive adapter operating inside one active ``BEGIN IMMEDIATE`` connection."""

    def __init__(self, store: SQLiteRepositoryStore, conn: sqlite3.Connection) -> None:
        self._store = store
        self._conn = conn

    def append_event(self, event: Event) -> Event:
        res = self._store.events.append_on_connection(self._conn, event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Reception audit event append rejected."
            raise ReceptionPersistenceError("AUDIT_EVENT_REJECTED", msg)
        return event

    def find_idempotent_intake(
        self,
        scope: str,
        key: str,
    ) -> tuple[str, UserRequestIntakeOutcome] | None:
        row = self._conn.execute(
            """
            SELECT request_fingerprint, outcome_json
            FROM intake_requests
            WHERE idempotency_scope = ? AND idempotency_key = ?;
            """,
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        outcome = UserRequestIntakeOutcome.model_validate_json(str(row["outcome_json"]))
        return str(row["request_fingerprint"]), outcome

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?;",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_conversation(self._conn, row)

    def create_conversation(self, record: ConversationRecord) -> ConversationRecord:
        validated = ConversationRecord.model_validate_json(record.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO conversations (
                conversation_id, project_id, owner_actor_id, title, status,
                record_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                validated.conversation_id,
                validated.project_id,
                validated.owner_actor_id,
                validated.title,
                validated.status,
                validated.record_version,
                validated.created_at.isoformat(),
                validated.updated_at.isoformat(),
            ),
        )
        loaded = self.get_conversation(validated.conversation_id)
        assert loaded is not None
        return loaded

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?;",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_task(row)

    def get_run_task_id(self, run_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT task_id FROM runs WHERE run_id = ?;",
            (run_id,),
        ).fetchone()
        return str(row["task_id"]) if row is not None else None

    def get_task_conversation_id(self, task_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT conversation_id FROM conversation_task_links WHERE task_id = ?;",
            (task_id,),
        ).fetchone()
        return str(row["conversation_id"]) if row is not None else None

    def list_active_tasks_for_conversation(self, conversation_id: str) -> list[Task]:
        rows = self._conn.execute(
            """
            SELECT t.*
            FROM tasks t
            JOIN conversation_task_links l ON l.task_id = t.task_id
            WHERE l.conversation_id = ?
              AND t.status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
            ORDER BY COALESCE(t.created_at, '') ASC, t.task_id ASC;
            """,
            (conversation_id,),
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_running_tasks(self) -> list[Task]:
        rows = self._conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'RUNNING'
            ORDER BY COALESCE(created_at, '') ASC, task_id ASC;
            """
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def create_task(self, task: Task, task_created_event: Event) -> Task:
        stored = self._store.tasks.create_task_on_connection(self._conn, task)
        self.append_event(task_created_event)
        return stored.task

    def insert_intake(
        self,
        record: IntakeRecord,
        outcome: UserRequestIntakeOutcome,
    ) -> IntakeRecord:
        validated = IntakeRecord.model_validate_json(record.model_dump_json())
        extra_payload = {
            "reason_code": validated.reason_code,
            "clarification_questions": validated.clarification_questions,
        }
        validate_safe_payload(extra_payload, "intake.payload")
        self._conn.execute(
            """
            INSERT INTO intake_requests (
                intake_id, conversation_id, idempotency_scope, idempotency_key,
                request_fingerprint, source_channel, submitted_by_actor_id, submitted_by_actor_kind,
                intent_mode, classification, decision, processing_status, summary,
                payload_json, related_task_id, created_task_id, observed_running_task_id,
                run_id, approval_id, route_targets_json, escalated_to_master,
                master_handoff_status, master_decision_note, master_decided_by, master_decided_at,
                correlation_id, audit_event_id, outcome_json, record_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                validated.intake_id,
                validated.conversation_id,
                validated.idempotency_scope,
                validated.idempotency_key,
                validated.request_fingerprint,
                validated.source_channel,
                validated.submitted_by_actor_id,
                validated.submitted_by_actor_kind,
                validated.intent_mode,
                validated.classification,
                validated.decision,
                validated.processing_status,
                validated.summary,
                _json_dumps(extra_payload),
                validated.related_task_id,
                validated.created_task_id,
                validated.observed_running_task_id,
                validated.run_id,
                validated.approval_id,
                _json_dumps(validated.route_targets),
                1 if validated.escalated_to_master else 0,
                validated.master_handoff_status,
                validated.master_decision_note,
                validated.master_decided_by,
                validated.master_decided_at.isoformat() if validated.master_decided_at else None,
                validated.correlation_id,
                validated.audit_event_id,
                outcome.model_dump_json(),
                validated.record_version,
                validated.created_at.isoformat(),
                validated.updated_at.isoformat(),
            ),
        )
        loaded = self.get_intake_for_update(validated.intake_id)
        assert loaded is not None
        return loaded

    def update_intake_outcome(
        self,
        intake_id: str,
        outcome: UserRequestIntakeOutcome,
    ) -> None:
        self._conn.execute(
            "UPDATE intake_requests SET outcome_json = ? WHERE intake_id = ?;",
            (outcome.model_dump_json(), intake_id),
        )

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
        existing = self.get_task_conversation_id(task_id)
        if existing is not None:
            if existing != conversation_id:
                raise ReceptionPersistenceError(
                    "CROSS_CONVERSATION_TASK_CONFLICT",
                    f"Task '{task_id}' is already bound to conversation '{existing}'.",
                )
            return
        self._conn.execute(
            """
            INSERT INTO conversation_task_links (
                task_id, conversation_id, link_role, linked_by_intake_id, audit_event_id, linked_at
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                task_id,
                conversation_id,
                link_role,
                intake_id,
                audit_event_id,
                linked_at.isoformat(),
            ),
        )

    def append_conversation_turn(self, turn: ConversationTurnRecord) -> ConversationTurnRecord:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(turn_index), 0) AS max_idx FROM conversation_turns WHERE conversation_id = ?;",
            (turn.conversation_id,),
        ).fetchone()
        next_idx = int(row["max_idx"]) + 1 if row is not None else 1
        assigned = turn.model_copy(update={"turn_index": next_idx})
        self._conn.execute(
            """
            INSERT INTO conversation_turns (
                turn_id, conversation_id, turn_index, intake_id, task_id, run_id,
                sender_actor_id, sender_kind, role, message_text, audit_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                assigned.turn_id,
                assigned.conversation_id,
                assigned.turn_index,
                assigned.intake_id,
                assigned.task_id,
                assigned.run_id,
                assigned.sender_actor_id,
                assigned.sender_kind,
                assigned.role,
                assigned.message_text,
                assigned.audit_event_id,
                assigned.created_at.isoformat(),
            ),
        )
        self._conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?;",
            (assigned.created_at.isoformat(), assigned.conversation_id),
        )
        return assigned

    def append_task_refinement(self, refinement: TaskRefinementRecord) -> TaskRefinementRecord:
        validate_safe_payload(refinement.metadata, "refinement.metadata")
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence_in_task), 0) AS max_seq FROM task_refinements WHERE task_id = ?;",
            (refinement.task_id,),
        ).fetchone()
        next_seq = int(row["max_seq"]) + 1 if row is not None else 1
        assigned = refinement.model_copy(update={"sequence_in_task": next_seq})
        self._conn.execute(
            """
            INSERT INTO task_refinements (
                refinement_id, task_id, run_id, conversation_id, intake_id, sequence_in_task,
                source_channel, submitted_by, refinement_text, metadata_json, audit_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                assigned.refinement_id,
                assigned.task_id,
                assigned.run_id,
                assigned.conversation_id,
                assigned.intake_id,
                assigned.sequence_in_task,
                assigned.source_channel,
                assigned.submitted_by,
                assigned.refinement_text,
                _json_dumps(assigned.metadata),
                assigned.audit_event_id,
                assigned.created_at.isoformat(),
            ),
        )
        return assigned

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

    def save_notification(self, notification: NotificationRef) -> NotificationRef:
        validated = NotificationRef.model_validate_json(notification.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO notifications (
                notification_id, task_id, run_id, event_id,
                severity, notification_type, created_at
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

    def get_intake_for_update(self, intake_id: str) -> IntakeRecord | None:
        row = self._conn.execute(
            "SELECT * FROM intake_requests WHERE intake_id = ?;",
            (intake_id,),
        ).fetchone()
        return _row_to_intake(row) if row is not None else None

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
        self.append_event(audit_event)
        ts_iso = decided_at.isoformat()
        cur = self._conn.execute(
            """
            UPDATE intake_requests
            SET processing_status = 'MASTER_ACKNOWLEDGED',
                master_handoff_status = 'ACKNOWLEDGED_BY_MASTER',
                master_decision_note = ?,
                master_decided_by = ?,
                master_decided_at = ?,
                audit_event_id = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE intake_id = ? AND record_version = ?;
            """,
            (
                decision_note,
                decided_by,
                ts_iso,
                audit_event.event_id,
                ts_iso,
                intake_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise StaleIntakeVersionError(
                "STALE_INTAKE_VERSION",
                f"Optimistic concurrency conflict while acknowledging intake '{intake_id}'.",
            )
        self._conn.execute(
            "UPDATE action_inbox_items SET status = 'ACKNOWLEDGED', updated_at = ? WHERE intake_id = ?;",
            (ts_iso, intake_id),
        )
        updated = self.get_intake_for_update(intake_id)
        assert updated is not None
        return updated

    def update_intake_resolution(
        self,
        *,
        intake_id: str,
        expected_record_version: int,
        classification: ConcurrentIntakeClassification,
        decision: IntakeDecision,
        processing_status: IntakeProcessingStatus,
        master_handoff_status: MasterHandoffStatus,
        related_task_id: str | None,
        created_task_id: str | None,
        reason_code: str | None,
        decision_note: str,
        decided_by: str,
        decided_at: datetime,
        audit_event: Event,
        inbox_status: ActionInboxStatus,
    ) -> IntakeRecord:
        self.append_event(audit_event)
        row = self._conn.execute(
            "SELECT payload_json FROM intake_requests WHERE intake_id = ?;",
            (intake_id,),
        ).fetchone()
        existing_payload = json.loads(str(row["payload_json"])) if row is not None else {}
        existing_payload["reason_code"] = reason_code
        ts_iso = decided_at.isoformat()
        cur = self._conn.execute(
            """
            UPDATE intake_requests
            SET classification = ?,
                decision = ?,
                processing_status = ?,
                master_handoff_status = ?,
                related_task_id = ?,
                created_task_id = ?,
                payload_json = ?,
                master_decision_note = ?,
                master_decided_by = ?,
                master_decided_at = ?,
                audit_event_id = ?,
                record_version = record_version + 1,
                updated_at = ?
            WHERE intake_id = ? AND record_version = ?;
            """,
            (
                classification,
                decision,
                processing_status,
                master_handoff_status,
                related_task_id,
                created_task_id,
                _json_dumps(existing_payload),
                decision_note,
                decided_by,
                ts_iso,
                audit_event.event_id,
                ts_iso,
                intake_id,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise StaleIntakeVersionError(
                "STALE_INTAKE_VERSION",
                f"Optimistic concurrency conflict while resolving intake '{intake_id}'.",
            )
        self._conn.execute(
            """
            UPDATE action_inbox_items
            SET status = ?, task_id = ?, updated_at = ?
            WHERE intake_id = ?;
            """,
            (inbox_status, related_task_id, ts_iso, intake_id),
        )
        updated = self.get_intake_for_update(intake_id)
        assert updated is not None
        return updated

    def get_dedup_state(self, dedup_scope_key: str) -> DedupCounterRecord | None:
        row = self._conn.execute(
            "SELECT * FROM reception_dedup_state WHERE dedup_scope_key = ?;",
            (dedup_scope_key,),
        ).fetchone()
        if row is None:
            return None
        return DedupCounterRecord.model_validate(
            {
                "dedup_scope_key": row["dedup_scope_key"],
                "task_id": row["task_id"],
                "notification_type": row["notification_type"],
                "severity": row["severity"],
                "dedup_token": row["dedup_token"],
                "occurrence_count": int(row["occurrence_count"]),
                "first_notification_id": row["first_notification_id"],
                "last_signal_id": row["last_signal_id"],
                "updated_at": row["updated_at"],
            }
        )

    def upsert_dedup_state(self, record: DedupCounterRecord) -> DedupCounterRecord:
        validated = DedupCounterRecord.model_validate_json(record.model_dump_json())
        self._conn.execute(
            """
            INSERT INTO reception_dedup_state (
                dedup_scope_key, task_id, notification_type, severity, dedup_token,
                occurrence_count, first_notification_id, last_signal_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedup_scope_key) DO UPDATE SET
                occurrence_count = excluded.occurrence_count,
                first_notification_id = COALESCE(
                    reception_dedup_state.first_notification_id,
                    excluded.first_notification_id
                ),
                last_signal_id = excluded.last_signal_id,
                updated_at = excluded.updated_at;
            """,
            (
                validated.dedup_scope_key,
                validated.task_id,
                validated.notification_type,
                validated.severity,
                validated.dedup_token,
                validated.occurrence_count,
                validated.first_notification_id,
                validated.last_signal_id,
                validated.updated_at.isoformat(),
            ),
        )
        return validated


class SQLiteReceptionRepository:
    """SQLite adapter backing Reception intake, conversations, refinements, and Action Inbox."""

    def __init__(
        self,
        store: SQLiteRepositoryStore,
        *,
        before_commit_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Bind Reception repository to an open ``SQLiteRepositoryStore`` with optional fault hook."""
        self._store = store
        self._before_commit_hook = before_commit_hook

    def execute_transaction(
        self,
        processor: Callable[[ReceptionTransactionContext], Any],
    ) -> Any:
        """Execute ``processor`` inside a single atomic ``BEGIN IMMEDIATE`` transaction."""
        try:
            with self._store.transaction_scope() as conn:
                tx = _SQLiteReceptionTransactionContext(self._store, conn)
                result = processor(tx)
                if self._before_commit_hook is not None:
                    self._before_commit_hook(conn)
                return result
        except (sqlite3.Error, RepositoryError) as exc:
            raise ReceptionPersistenceError(
                "RECEPTION_PERSISTENCE_FAILED",
                f"Atomic Reception persistence transaction failed and rolled back: {exc}",
            ) from exc

    def create_conversation(
        self,
        record: ConversationRecord,
        audit_event: Event,
    ) -> ConversationRecord:
        """Atomically append Event and persist a new ConversationRecord."""
        def _op(tx: ReceptionTransactionContext) -> ConversationRecord:
            tx.append_event(audit_event)
            return tx.create_conversation(record)

        return self.execute_transaction(_op)

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        """Fetch persisted ConversationRecord by ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?;",
                (conversation_id,),
            ).fetchone()
            return _row_to_conversation(conn, row) if row is not None else None

    def list_conversations(self, *, owner_actor_id: str | None = None) -> list[ConversationRecord]:
        """List persisted conversations optionally filtered by owner_actor_id."""
        with self._store.open_connection() as conn:
            if owner_actor_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM conversations
                    WHERE owner_actor_id = ?
                    ORDER BY created_at ASC, conversation_id ASC;
                    """,
                    (owner_actor_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conversations ORDER BY created_at ASC, conversation_id ASC;"
                ).fetchall()
            return [_row_to_conversation(conn, row) for row in rows]

    def list_conversation_turns(self, conversation_id: str) -> list[ConversationTurnRecord]:
        """List immutable turns in chronological ``turn_index`` order."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversation_turns
                WHERE conversation_id = ?
                ORDER BY turn_index ASC;
                """,
                (conversation_id,),
            ).fetchall()
            return [_row_to_turn(row) for row in rows]

    def get_intake_request(self, intake_id: str) -> IntakeRecord | None:
        """Fetch persisted IntakeRecord by ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM intake_requests WHERE intake_id = ?;",
                (intake_id,),
            ).fetchone()
            return _row_to_intake(row) if row is not None else None

    def list_intake_requests(
        self,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
        master_handoff_status: MasterHandoffStatus | None = None,
        processing_status: IntakeProcessingStatus | None = None,
    ) -> list[IntakeRecord]:
        """Query persisted IntakeRecords ordered deterministically by creation time."""
        clauses: list[str] = []
        params: list[object] = []
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if task_id is not None:
            clauses.append("(related_task_id = ? OR created_task_id = ?)")
            params.extend([task_id, task_id])
        if master_handoff_status is not None:
            clauses.append("master_handoff_status = ?")
            params.append(master_handoff_status)
        if processing_status is not None:
            clauses.append("processing_status = ?")
            params.append(processing_status)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = f"SELECT * FROM intake_requests {where_sql}ORDER BY created_at ASC, intake_id ASC;"
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_intake(row) for row in rows]

    def list_task_refinements(self, task_id: str) -> list[TaskRefinementRecord]:
        """List immutable addendum refinements attached to ``task_id`` in exact sequence."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_refinements
                WHERE task_id = ?
                ORDER BY sequence_in_task ASC;
                """,
                (task_id,),
            ).fetchall()
            return [_row_to_refinement(row) for row in rows]

    def list_action_inbox(
        self,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
        include_closed: bool = False,
    ) -> list[ActionInboxItem]:
        """List persisted ActionInboxItems sorted by priority rank then timestamp."""
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if not include_closed:
            clauses.append("status IN ('OPEN', 'ACKNOWLEDGED')")
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = f"SELECT * FROM action_inbox_items {where_sql}ORDER BY created_at ASC, inbox_item_id ASC;"
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            items = [_row_to_inbox_item(row) for row in rows]
        return sorted(items, key=lambda item: (_PRIO_RANK[item.priority], item.created_at))

    def get_task_snapshot(self, task_id: str) -> Task | None:
        """Read current persisted Task snapshot if present."""
        return self._store.tasks.get_task(task_id)

    def recover_intake_state(self) -> IntakeRecoverySnapshot:
        """Rehydrate persisted intake, conversation, inbox, and Task statuses without auto-starting."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.*,
                    t.status AS live_task_status,
                    t.active_run_id AS live_active_run_id
                FROM intake_requests i
                LEFT JOIN tasks t
                    ON t.task_id = COALESCE(i.created_task_id, i.related_task_id)
                ORDER BY i.created_at ASC, i.intake_id ASC;
                """
            ).fetchall()
            conv_rows = conn.execute(
                "SELECT * FROM conversations ORDER BY created_at ASC, conversation_id ASC;"
            ).fetchall()
            conversations = [_row_to_conversation(conn, row) for row in conv_rows]

        open_inbox = self.list_action_inbox(include_closed=False)
        entries: list[RecoveredIntakeEntry] = []
        awaiting_master = 0
        needs_clarification = 0
        queued = 0
        assigned = 0
        running = 0
        terminal = 0
        seen_tasks: set[str] = set()

        for row in rows:
            rec = _row_to_intake(row)
            task_status = row["live_task_status"]
            active_run_id = row["live_active_run_id"]
            entries.append(
                RecoveredIntakeEntry(
                    intake=rec,
                    linked_task_status=task_status,
                    active_run_id=active_run_id,
                )
            )
            if rec.master_handoff_status == "PENDING_MASTER_REVIEW":
                awaiting_master += 1
            if rec.processing_status == "NEEDS_CLARIFICATION":
                needs_clarification += 1

            linked_tid = rec.created_task_id or rec.related_task_id
            if linked_tid is not None and linked_tid not in seen_tasks and task_status is not None:
                seen_tasks.add(linked_tid)
                if task_status == "QUEUED":
                    queued += 1
                elif task_status == "ASSIGNED":
                    assigned += 1
                elif task_status == "RUNNING":
                    running += 1
                elif task_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                    terminal += 1

        return IntakeRecoverySnapshot(
            intakes=entries,
            conversations=conversations,
            open_inbox_items=open_inbox,
            awaiting_master_review=awaiting_master,
            needs_clarification=needs_clarification,
            queued_tasks=queued,
            assigned_tasks=assigned,
            running_tasks=running,
            terminal_tasks=terminal,
        )
