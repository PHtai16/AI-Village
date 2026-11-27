"""SQLite persistence implementation for Phase 06 Meetings, Decisions, Conflicts, and Delivery."""

from __future__ import annotations

import json
import sqlite3

from core.contracts.event import Event
from core.orchestration.meeting_models import (
    CollaborationError,
    MasterDecisionRecord,
    MasterDeliverySummary,
    MeetingNotFoundError,
    MeetingRecord,
    MeetingStateError,
    StaleCollaborationVersionError,
    StoredMeeting,
    StoredWorkerConflict,
    WorkerConflictRecord,
)
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _to_json(model: object) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))  # type: ignore[attr-defined]


def _row_to_stored_meeting(row: sqlite3.Row) -> StoredMeeting:
    return StoredMeeting(
        meeting=MeetingRecord.model_validate_json(str(row["meeting_json"])),
        record_version=int(row["record_version"]),
        last_audit_event_id=row["last_audit_event_id"],
    )


def _row_to_stored_conflict(row: sqlite3.Row) -> StoredWorkerConflict:
    return StoredWorkerConflict(
        conflict=WorkerConflictRecord.model_validate_json(str(row["conflict_json"])),
        record_version=int(row["record_version"]),
        last_audit_event_id=row["last_audit_event_id"],
    )


class SQLiteCollaborationRepository:
    """SQLite implementation of ``core.orchestration.meeting_models.CollaborationRepository``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind Collaboration repository to an open ``SQLiteRepositoryStore``."""
        self._store = store

    def _append_events(self, conn: sqlite3.Connection, events: list[Event]) -> None:
        for ev in events:
            res = self._store.events.append_on_connection(conn, ev)
            if res.status == "REJECTED" or res.entry is None:
                msg = res.error.message if res.error else "Collaboration event append rejected."
                raise CollaborationError("EVENT_APPEND_FAILED", msg)

    def create_meeting_atomic(self, *, meeting: MeetingRecord, event: Event) -> StoredMeeting:
        """Atomically persist Event and newly opened Meeting."""
        validated = MeetingRecord.model_validate_json(meeting.model_dump_json())
        with self._store.transaction_scope() as conn:
            if conn.execute("SELECT 1 FROM meetings WHERE meeting_id = ?;", (validated.meeting_id,)).fetchone():
                raise MeetingStateError("DUPLICATE_MEETING_ID", f"Meeting '{validated.meeting_id}' already exists.")
            self._append_events(conn, [event])
            try:
                conn.execute(
                    """
                    INSERT INTO meetings (
                        meeting_id, task_id, run_id, plan_id, team_proposal_id,
                        status, meeting_json, record_version, created_at, last_audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?);
                    """,
                    (
                        validated.meeting_id,
                        validated.task_id,
                        validated.run_id,
                        validated.plan_id,
                        validated.team_proposal_id,
                        validated.status,
                        _to_json(validated),
                        validated.created_at.isoformat(),
                        event.event_id,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError("PERSISTENCE_FAILED", str(exc), entity_id=validated.meeting_id) from exc
            row = conn.execute("SELECT * FROM meetings WHERE meeting_id = ?;", (validated.meeting_id,)).fetchone()
            assert row is not None
            return _row_to_stored_meeting(row)

    def update_meeting_atomic(
        self,
        *,
        meeting: MeetingRecord,
        expected_record_version: int,
        event: Event,
    ) -> StoredMeeting:
        """Atomically update meeting snapshot with optimistic locking."""
        validated = MeetingRecord.model_validate_json(meeting.model_dump_json())
        with self._store.transaction_scope() as conn:
            cur = conn.execute("SELECT record_version FROM meetings WHERE meeting_id = ?;", (validated.meeting_id,)).fetchone()
            if cur is None:
                raise MeetingNotFoundError("MEETING_NOT_FOUND", f"Meeting '{validated.meeting_id}' not found.")
            if int(cur["record_version"]) != expected_record_version:
                raise StaleCollaborationVersionError("STALE_MEETING_VERSION", "Concurrent modification on meeting.")
            self._append_events(conn, [event])
            cursor = conn.execute(
                """
                UPDATE meetings
                SET status = ?, meeting_json = ?, record_version = record_version + 1, last_audit_event_id = ?
                WHERE meeting_id = ? AND record_version = ?;
                """,
                (validated.status, _to_json(validated), event.event_id, validated.meeting_id, expected_record_version),
            )
            if cursor.rowcount != 1:
                raise StaleCollaborationVersionError("STALE_MEETING_VERSION", "Concurrent modification on meeting.")
            row = conn.execute("SELECT * FROM meetings WHERE meeting_id = ?;", (validated.meeting_id,)).fetchone()
            assert row is not None
            return _row_to_stored_meeting(row)

    def record_decision_atomic(
        self,
        *,
        decision: MasterDecisionRecord,
        updated_meeting: MeetingRecord | None,
        expected_meeting_version: int | None,
        events: list[Event],
    ) -> MasterDecisionRecord:
        """Atomically insert immutable MasterDecisionRecord, optionally close/decide meeting, and append events."""
        validated_dec = MasterDecisionRecord.model_validate_json(decision.model_dump_json())
        with self._store.transaction_scope() as conn:
            if updated_meeting is not None:
                assert expected_meeting_version is not None
                cur = conn.execute(
                    "SELECT record_version FROM meetings WHERE meeting_id = ?;",
                    (updated_meeting.meeting_id,),
                ).fetchone()
                if cur is None:
                    raise MeetingNotFoundError("MEETING_NOT_FOUND", f"Meeting '{updated_meeting.meeting_id}' not found.")
                if int(cur["record_version"]) != expected_meeting_version:
                    raise StaleCollaborationVersionError("STALE_MEETING_VERSION", "Concurrent meeting modification.")
            self._append_events(conn, events)
            try:
                conn.execute(
                    """
                    INSERT INTO meeting_decisions (
                        decision_id, meeting_id, conflict_id, task_id, run_id,
                        decision_json, created_at, last_audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated_dec.decision_id,
                        validated_dec.meeting_id,
                        validated_dec.conflict_id,
                        validated_dec.task_id,
                        validated_dec.run_id,
                        _to_json(validated_dec),
                        validated_dec.timestamp.isoformat(),
                        events[0].event_id,
                    ),
                )
                if updated_meeting is not None:
                    assert expected_meeting_version is not None
                    validated_mtg = MeetingRecord.model_validate_json(updated_meeting.model_dump_json())
                    cursor = conn.execute(
                        """
                        UPDATE meetings
                        SET status = ?, meeting_json = ?, record_version = record_version + 1, last_audit_event_id = ?
                        WHERE meeting_id = ? AND record_version = ?;
                        """,
                        (
                            validated_mtg.status,
                            _to_json(validated_mtg),
                            events[-1].event_id,
                            validated_mtg.meeting_id,
                            expected_meeting_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise StaleCollaborationVersionError("STALE_MEETING_VERSION", "Concurrent meeting update.")
            except sqlite3.Error as exc:
                raise RepositoryError("PERSISTENCE_FAILED", str(exc), entity_id=validated_dec.decision_id) from exc
        return validated_dec

    def get_meeting(self, meeting_id: str) -> StoredMeeting | None:
        with self._store.open_connection() as conn:
            row = conn.execute("SELECT * FROM meetings WHERE meeting_id = ?;", (meeting_id,)).fetchone()
            return _row_to_stored_meeting(row) if row is not None else None

    def list_meetings(self, *, task_id: str | None = None) -> list[StoredMeeting]:
        with self._store.open_connection() as conn:
            if task_id is not None:
                rows = conn.execute(
                    "SELECT * FROM meetings WHERE task_id = ? ORDER BY created_at ASC;",
                    (task_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM meetings ORDER BY created_at ASC;").fetchall()
            return [_row_to_stored_meeting(r) for r in rows]

    def list_decisions(
        self,
        *,
        task_id: str | None = None,
        meeting_id: str | None = None,
    ) -> list[MasterDecisionRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if meeting_id is not None:
            clauses.append("meeting_id = ?")
            params.append(meeting_id)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT decision_json FROM meeting_decisions {where_sql}ORDER BY created_at ASC;",
                tuple(params),
            ).fetchall()
            return [MasterDecisionRecord.model_validate_json(str(r["decision_json"])) for r in rows]

    def create_conflict_atomic(self, *, conflict: WorkerConflictRecord, event: Event) -> StoredWorkerConflict:
        validated = WorkerConflictRecord.model_validate_json(conflict.model_dump_json())
        with self._store.transaction_scope() as conn:
            self._append_events(conn, [event])
            try:
                conn.execute(
                    """
                    INSERT INTO worker_conflicts (
                        conflict_id, task_id, run_id, status, conflict_json,
                        record_version, detected_at, last_audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?);
                    """,
                    (
                        validated.conflict_id,
                        validated.task_id,
                        validated.run_id,
                        validated.status,
                        _to_json(validated),
                        validated.detected_at.isoformat(),
                        event.event_id,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError("PERSISTENCE_FAILED", str(exc), entity_id=validated.conflict_id) from exc
            row = conn.execute("SELECT * FROM worker_conflicts WHERE conflict_id = ?;", (validated.conflict_id,)).fetchone()
            assert row is not None
            return _row_to_stored_conflict(row)

    def resolve_conflict_atomic(
        self,
        *,
        conflict: WorkerConflictRecord,
        expected_record_version: int,
        event: Event,
    ) -> StoredWorkerConflict:
        validated = WorkerConflictRecord.model_validate_json(conflict.model_dump_json())
        with self._store.transaction_scope() as conn:
            cur = conn.execute("SELECT record_version FROM worker_conflicts WHERE conflict_id = ?;", (validated.conflict_id,)).fetchone()
            if cur is None:
                raise MeetingNotFoundError("CONFLICT_NOT_FOUND", f"Conflict '{validated.conflict_id}' not found.")
            if int(cur["record_version"]) != expected_record_version:
                raise StaleCollaborationVersionError("STALE_CONFLICT_VERSION", "Concurrent conflict update.")
            self._append_events(conn, [event])
            cursor = conn.execute(
                """
                UPDATE worker_conflicts
                SET status = ?, conflict_json = ?, record_version = record_version + 1, last_audit_event_id = ?
                WHERE conflict_id = ? AND record_version = ?;
                """,
                (validated.status, _to_json(validated), event.event_id, validated.conflict_id, expected_record_version),
            )
            if cursor.rowcount != 1:
                raise StaleCollaborationVersionError("STALE_CONFLICT_VERSION", "Concurrent conflict update.")
            row = conn.execute("SELECT * FROM worker_conflicts WHERE conflict_id = ?;", (validated.conflict_id,)).fetchone()
            assert row is not None
            return _row_to_stored_conflict(row)

    def get_conflict(self, conflict_id: str) -> StoredWorkerConflict | None:
        with self._store.open_connection() as conn:
            row = conn.execute("SELECT * FROM worker_conflicts WHERE conflict_id = ?;", (conflict_id,)).fetchone()
            return _row_to_stored_conflict(row) if row is not None else None

    def list_conflicts(self, *, task_id: str | None = None, run_id: str | None = None) -> list[StoredWorkerConflict]:
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM worker_conflicts {where_sql}ORDER BY detected_at ASC;",
                tuple(params),
            ).fetchall()
            return [_row_to_stored_conflict(r) for r in rows]

    def save_delivery_atomic(self, *, delivery: MasterDeliverySummary, event: Event) -> MasterDeliverySummary:
        validated = MasterDeliverySummary.model_validate_json(delivery.model_dump_json())
        with self._store.transaction_scope() as conn:
            self._append_events(conn, [event])
            try:
                conn.execute(
                    """
                    INSERT INTO master_deliveries (
                        delivery_id, task_id, run_id, verification_status,
                        qa_verified, delivery_json, compiled_at, last_audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated.delivery_id,
                        validated.task_id,
                        validated.run_id,
                        validated.delivery_verification_status,
                        1 if validated.qa_verified else 0,
                        _to_json(validated),
                        validated.compiled_at.isoformat(),
                        event.event_id,
                    ),
                )
            except sqlite3.Error as exc:
                raise RepositoryError("PERSISTENCE_FAILED", str(exc), entity_id=validated.delivery_id) from exc
        return validated

    def get_delivery(self, delivery_id: str) -> MasterDeliverySummary | None:
        with self._store.open_connection() as conn:
            row = conn.execute("SELECT delivery_json FROM master_deliveries WHERE delivery_id = ?;", (delivery_id,)).fetchone()
            return MasterDeliverySummary.model_validate_json(str(row["delivery_json"])) if row is not None else None

    def list_deliveries(self, *, task_id: str | None = None) -> list[MasterDeliverySummary]:
        with self._store.open_connection() as conn:
            if task_id is not None:
                rows = conn.execute(
                    "SELECT delivery_json FROM master_deliveries WHERE task_id = ? ORDER BY compiled_at ASC;",
                    (task_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT delivery_json FROM master_deliveries ORDER BY compiled_at ASC;").fetchall()
            return [MasterDeliverySummary.model_validate_json(str(r["delivery_json"])) for r in rows]
