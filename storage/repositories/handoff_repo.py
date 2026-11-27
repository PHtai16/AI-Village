"""SQLite persistence implementation for evidence-based Worker Handoffs."""

from __future__ import annotations

import json
import sqlite3

from core.contracts.event import Event
from core.orchestration.handoff_models import (
    DuplicateHandoffConflictError,
    HandoffError,
    HandoffNotFoundError,
    HandoffRevisionRecord,
    HandoffStatus,
    StaleHandoffVersionError,
    StoredWorkerHandoff,
    WorkerHandoffRecord,
)
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _row_to_stored_handoff(row: sqlite3.Row) -> StoredWorkerHandoff:
    """Rehydrate a validated ``StoredWorkerHandoff`` from SQLite ``worker_handoffs``."""
    record = WorkerHandoffRecord.model_validate_json(str(row["handoff_json"]))
    return StoredWorkerHandoff(
        handoff=record,
        record_version=int(row["record_version"]),
        last_audit_event_id=row["last_audit_event_id"],
    )


def _row_to_handoff_revision(row: sqlite3.Row) -> HandoffRevisionRecord:
    """Rehydrate a validated ``HandoffRevisionRecord`` from SQLite."""
    return HandoffRevisionRecord.model_validate(
        {
            "revision_id": row["revision_id"],
            "handoff_id": row["handoff_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "record_version": int(row["record_version"]),
            "status": row["status"],
            "actor_id": row["actor_id"],
            "reason": row["reason"],
            "content_sha256": row["content_sha256"],
            "event_id": row["event_id"],
            "timestamp": row["timestamp"],
        }
    )


class SQLiteWorkerHandoffRepository:
    """SQLite implementation of ``core.orchestration.handoff_models.WorkerHandoffRepository``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind Worker Handoff repository to an open ``SQLiteRepositoryStore``."""
        self._store = store

    def _append_audit_or_raise(self, conn: sqlite3.Connection, event: Event) -> None:
        """Append handoff audit Event inside active SQLite transaction or raise fail-closed."""
        res = self._store.events.append_on_connection(conn, event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Worker handoff event append rejected."
            raise HandoffError("EVENT_APPEND_FAILED", msg)

    @staticmethod
    def _insert_revision_in_conn(
        conn: sqlite3.Connection,
        revision: HandoffRevisionRecord,
    ) -> None:
        """Insert immutable revision snapshot row inside active SQLite transaction."""
        conn.execute(
            """
            INSERT INTO worker_handoff_revisions (
                revision_id, handoff_id, task_id, run_id, record_version,
                status, actor_id, reason, content_sha256, event_id, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                revision.revision_id,
                revision.handoff_id,
                revision.task_id,
                revision.run_id,
                revision.record_version,
                revision.status,
                revision.actor_id,
                revision.reason,
                revision.content_sha256,
                revision.event_id,
                revision.timestamp.isoformat(),
            ),
        )

    def create_handoff_atomic(
        self,
        *,
        handoff: WorkerHandoffRecord,
        revision: HandoffRevisionRecord,
        event: Event,
    ) -> StoredWorkerHandoff:
        """Atomically persist Event, Handoff snapshot, and immutable revision v1."""
        validated = WorkerHandoffRecord.model_validate_json(handoff.model_dump_json())
        rev_row = HandoffRevisionRecord.model_validate_json(revision.model_dump_json())
        content_hash = validated.content_sha256 or validated.compute_content_sha256()
        validated = validated.model_copy(update={"content_sha256": content_hash})
        canonical_json = json.dumps(
            validated.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )

        with self._store.transaction_scope() as conn:
            existing = conn.execute(
                "SELECT content_sha256 FROM worker_handoffs WHERE handoff_id = ?;",
                (validated.handoff_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["content_sha256"]) != content_hash:
                    raise DuplicateHandoffConflictError(
                        "DUPLICATE_HANDOFF_CONFLICT",
                        f"Handoff '{validated.handoff_id}' already exists with different content.",
                    )
                raise DuplicateHandoffConflictError(
                    "DUPLICATE_HANDOFF_EXISTS",
                    f"Handoff '{validated.handoff_id}' already exists.",
                )

            self._append_audit_or_raise(conn, event)

            try:
                conn.execute(
                    """
                    INSERT INTO worker_handoffs (
                        handoff_id, task_id, run_id, plan_id, plan_revision, team_proposal_id,
                        source_step_id, target_step_id, from_agent_id, to_agent_id,
                        from_session_id, to_session_id, status, content_sha256, handoff_json,
                        record_version, created_at, delivered_at, acknowledged_at, rejected_at,
                        last_audit_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated.handoff_id,
                        validated.task_id,
                        validated.run_id,
                        validated.plan_id,
                        validated.plan_revision,
                        validated.team_proposal_id,
                        validated.source_step_id,
                        validated.target_step_id,
                        validated.from_agent_id,
                        validated.to_agent_id,
                        validated.from_session_id,
                        validated.to_session_id,
                        validated.status,
                        content_hash,
                        canonical_json,
                        validated.created_at.isoformat(),
                        validated.delivered_at.isoformat() if validated.delivered_at else None,
                        validated.acknowledged_at.isoformat() if validated.acknowledged_at else None,
                        validated.rejected_at.isoformat() if validated.rejected_at else None,
                        event.event_id,
                    ),
                )
                self._insert_revision_in_conn(conn, rev_row)
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to persist WorkerHandoffRecord '{validated.handoff_id}': {exc}",
                    entity_id=validated.handoff_id,
                ) from exc

            row = conn.execute(
                "SELECT * FROM worker_handoffs WHERE handoff_id = ?;",
                (validated.handoff_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_handoff(row)

    def transition_handoff_atomic(
        self,
        *,
        handoff: WorkerHandoffRecord,
        expected_record_version: int,
        revision: HandoffRevisionRecord,
        event: Event,
    ) -> StoredWorkerHandoff:
        """Atomically transition Handoff state under optimistic lock and append revision + Event."""
        validated = WorkerHandoffRecord.model_validate_json(handoff.model_dump_json())
        rev_row = HandoffRevisionRecord.model_validate_json(revision.model_dump_json())
        content_hash = validated.content_sha256 or validated.compute_content_sha256()
        validated = validated.model_copy(update={"content_sha256": content_hash})
        canonical_json = json.dumps(
            validated.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )

        with self._store.transaction_scope() as conn:
            current = conn.execute(
                "SELECT record_version, content_sha256 FROM worker_handoffs WHERE handoff_id = ?;",
                (validated.handoff_id,),
            ).fetchone()
            if current is None:
                raise HandoffNotFoundError(
                    "HANDOFF_NOT_FOUND",
                    f"Handoff '{validated.handoff_id}' does not exist.",
                )
            if int(current["record_version"]) != expected_record_version:
                raise StaleHandoffVersionError(
                    "STALE_HANDOFF_VERSION",
                    (
                        f"Stale handoff version for '{validated.handoff_id}': expected "
                        f"record_version={expected_record_version}, found {int(current['record_version'])}."
                    ),
                )
            if str(current["content_sha256"]) != content_hash:
                raise DuplicateHandoffConflictError(
                    "HANDOFF_IMMUTABLE_CONTENT_MUTATION",
                    f"Cannot mutate immutable payload digest of handoff '{validated.handoff_id}'.",
                )

            self._append_audit_or_raise(conn, event)

            try:
                cursor = conn.execute(
                    """
                    UPDATE worker_handoffs
                    SET to_session_id = ?,
                        status = ?,
                        handoff_json = ?,
                        record_version = record_version + 1,
                        delivered_at = ?,
                        acknowledged_at = ?,
                        rejected_at = ?,
                        last_audit_event_id = ?
                    WHERE handoff_id = ? AND record_version = ?;
                    """,
                    (
                        validated.to_session_id,
                        validated.status,
                        canonical_json,
                        validated.delivered_at.isoformat() if validated.delivered_at else None,
                        validated.acknowledged_at.isoformat() if validated.acknowledged_at else None,
                        validated.rejected_at.isoformat() if validated.rejected_at else None,
                        event.event_id,
                        validated.handoff_id,
                        expected_record_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleHandoffVersionError(
                        "STALE_HANDOFF_VERSION",
                        f"Concurrent modification detected on handoff '{validated.handoff_id}'.",
                    )
                self._insert_revision_in_conn(conn, rev_row)
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to transition handoff '{validated.handoff_id}': {exc}",
                    entity_id=validated.handoff_id,
                ) from exc

            row = conn.execute(
                "SELECT * FROM worker_handoffs WHERE handoff_id = ?;",
                (validated.handoff_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_handoff(row)

    def get_handoff(self, handoff_id: str) -> StoredWorkerHandoff | None:
        """Load persisted Worker Handoff snapshot by identifier or return None."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM worker_handoffs WHERE handoff_id = ?;",
                (handoff_id,),
            ).fetchone()
            return _row_to_stored_handoff(row) if row is not None else None

    def list_handoffs(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        from_agent_id: str | None = None,
        to_agent_id: str | None = None,
        source_step_id: str | None = None,
        target_step_id: str | None = None,
        status: HandoffStatus | None = None,
    ) -> list[StoredWorkerHandoff]:
        """List persisted Worker Handoffs filtered cleanly by domain identifiers."""
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if from_agent_id is not None:
            clauses.append("from_agent_id = ?")
            params.append(from_agent_id)
        if to_agent_id is not None:
            clauses.append("to_agent_id = ?")
            params.append(to_agent_id)
        if source_step_id is not None:
            clauses.append("source_step_id = ?")
            params.append(source_step_id)
        if target_step_id is not None:
            clauses.append("target_step_id = ?")
            params.append(target_step_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = f"SELECT * FROM worker_handoffs {where_sql}ORDER BY created_at ASC, handoff_id ASC;"
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_stored_handoff(row) for row in rows]

    def list_revisions(self, handoff_id: str) -> list[HandoffRevisionRecord]:
        """Return ascending immutable revision history for ``handoff_id``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM worker_handoff_revisions
                WHERE handoff_id = ?
                ORDER BY record_version ASC;
                """,
                (handoff_id,),
            ).fetchall()
            return [_row_to_handoff_revision(row) for row in rows]
