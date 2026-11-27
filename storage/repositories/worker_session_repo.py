"""SQLite persistence implementation for isolated Worker Sessions and transition audit logs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import sqlite3

from core.contracts.event import Event
from core.contracts.identifiers import EventId
from core.contracts.run import Run
from core.contracts.session import Session
from core.contracts.task import Task
from core.orchestration.worker_session_models import (
    DuplicateInvocationConflictError,
    ProviderConversationCollisionError,
    ReconciliationStatus,
    StaleSessionVersionError,
    StoredWorkerSession,
    WorkerSessionBinding,
    WorkerSessionError,
    WorkerSessionNotFoundError,
    WorkerSessionTransitionRecord,
)
from storage.repositories.errors import DuplicateEntityError, RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore

_STORED_SELECT_SQL = """
SELECT
    s.session_id AS s_session_id,
    s.legacy_session_ref AS s_legacy_session_ref,
    s.agent_id AS s_agent_id,
    s.task_id AS s_task_id,
    s.run_id AS s_run_id,
    s.invocation_id AS s_invocation_id,
    s.provider_id AS s_provider_id,
    s.connection_id AS s_connection_id,
    s.provider_conversation_id AS s_provider_conversation_id,
    s.workspace_reference AS s_workspace_reference,
    s.status AS s_status,
    s.started_at AS s_started_at,
    s.finished_at AS s_finished_at,
    b.session_id AS b_session_id,
    b.task_id AS b_task_id,
    b.run_id AS b_run_id,
    b.agent_id AS b_agent_id,
    b.plan_id AS b_plan_id,
    b.plan_revision AS b_plan_revision,
    b.team_proposal_id AS b_team_proposal_id,
    b.step_id AS b_step_id,
    b.checklist_item_id AS b_checklist_item_id,
    b.pinned_manifest_revision AS b_pinned_manifest_revision,
    b.pinned_manifest_sha256 AS b_pinned_manifest_sha256,
    b.provider_id AS b_provider_id,
    b.connection_id AS b_connection_id,
    b.model_id AS b_model_id,
    b.workspace_scope AS b_workspace_scope,
    b.private_context_namespace AS b_private_context_namespace,
    b.idempotency_key AS b_idempotency_key,
    b.request_fingerprint AS b_request_fingerprint,
    b.execution_id AS b_execution_id,
    b.correlation_id AS b_correlation_id,
    b.reconciliation_status AS b_reconciliation_status,
    b.reconciliation_reason AS b_reconciliation_reason,
    b.record_version AS b_record_version,
    b.last_audit_event_id AS b_last_audit_event_id,
    b.created_at AS b_created_at,
    b.updated_at AS b_updated_at
FROM sessions s
JOIN worker_session_bindings b ON s.session_id = b.session_id
"""


def _row_to_stored_worker_session(row: sqlite3.Row) -> StoredWorkerSession:
    """Rehydrate a validated ``StoredWorkerSession`` from joined SQLite rows."""
    session = Session.model_validate(
        {
            "session_id": row["s_session_id"],
            "legacy_session_ref": row["s_legacy_session_ref"],
            "agent_id": row["s_agent_id"],
            "task_id": row["s_task_id"],
            "run_id": row["s_run_id"],
            "invocation_id": row["s_invocation_id"],
            "provider_id": row["s_provider_id"],
            "connection_id": row["s_connection_id"],
            "provider_conversation_id": row["s_provider_conversation_id"],
            "workspace_reference": row["s_workspace_reference"],
            "status": row["s_status"],
            "started_at": row["s_started_at"],
            "finished_at": row["s_finished_at"],
        }
    )
    binding = WorkerSessionBinding.model_validate(
        {
            "session_id": row["b_session_id"],
            "task_id": row["b_task_id"],
            "run_id": row["b_run_id"],
            "agent_id": row["b_agent_id"],
            "plan_id": row["b_plan_id"],
            "plan_revision": int(row["b_plan_revision"]),
            "team_proposal_id": row["b_team_proposal_id"],
            "step_id": row["b_step_id"],
            "checklist_item_id": row["b_checklist_item_id"],
            "pinned_manifest_revision": int(row["b_pinned_manifest_revision"]),
            "pinned_manifest_sha256": row["b_pinned_manifest_sha256"],
            "provider_id": row["b_provider_id"],
            "connection_id": row["b_connection_id"],
            "model_id": row["b_model_id"],
            "workspace_scope": row["b_workspace_scope"],
            "private_context_namespace": row["b_private_context_namespace"],
            "idempotency_key": row["b_idempotency_key"],
            "request_fingerprint": row["b_request_fingerprint"],
            "execution_id": row["b_execution_id"],
            "correlation_id": row["b_correlation_id"],
            "reconciliation_status": row["b_reconciliation_status"],
            "reconciliation_reason": row["b_reconciliation_reason"],
            "record_version": int(row["b_record_version"]),
            "last_audit_event_id": row["b_last_audit_event_id"],
            "created_at": row["b_created_at"],
            "updated_at": row["b_updated_at"],
        }
    )
    return StoredWorkerSession(
        session=session,
        binding=binding,
        record_version=binding.record_version,
    )


def _row_to_transition(row: sqlite3.Row) -> WorkerSessionTransitionRecord:
    """Rehydrate a validated ``WorkerSessionTransitionRecord`` from SQLite."""
    return WorkerSessionTransitionRecord.model_validate(
        {
            "transition_id": row["transition_id"],
            "session_id": row["session_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "actor_id": row["actor_id"],
            "reason": row["reason"],
            "evidence_summary": row["evidence_summary"],
            "event_id": row["event_id"],
            "timestamp": row["timestamp"],
        }
    )


def _row_to_bare_session(row: sqlite3.Row) -> Session:
    """Rehydrate a ``Session`` row directly from ``sessions``."""
    return Session.model_validate(
        {
            "session_id": row["session_id"],
            "legacy_session_ref": row["legacy_session_ref"],
            "agent_id": row["agent_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "invocation_id": row["invocation_id"],
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "provider_conversation_id": row["provider_conversation_id"],
            "workspace_reference": row["workspace_reference"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
    )


class SQLiteWorkerSessionRepository:
    """SQLite implementation of ``core.orchestration.worker_session_models.WorkerSessionRepository``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind Worker Session repository to an open ``SQLiteRepositoryStore``."""
        self._store = store

    def _append_events_in_conn(
        self,
        conn: sqlite3.Connection,
        events: Sequence[Event],
    ) -> list[EventId]:
        """Append Events inside the active SQLite transaction or raise fail-closed."""
        recorded: list[EventId] = []
        for event in events:
            res = self._store.events.append_on_connection(conn, event)
            if res.status == "REJECTED" or res.entry is None:
                msg = res.error.message if res.error else "Worker session event append rejected."
                raise WorkerSessionError("EVENT_APPEND_FAILED", msg)
            recorded.append(event.event_id)
        return recorded

    def _find_conflict_in_conn(
        self,
        conn: sqlite3.Connection,
        provider_id: str,
        provider_conversation_id: str,
        *,
        session_id: str,
        task_id: str,
        agent_id: str,
    ) -> Session | None:
        """Detect any different session/worker/task holding identical provider conversation ID."""
        row = conn.execute(
            """
            SELECT * FROM sessions
            WHERE provider_id = ?
              AND lower(provider_conversation_id) = lower(?)
              AND session_id != ?
              AND (task_id IS NULL OR task_id != ? OR agent_id != ?)
            LIMIT 1;
            """,
            (provider_id, provider_conversation_id, session_id, task_id, agent_id),
        ).fetchone()
        if row is not None:
            return _row_to_bare_session(row)

        same_owner_row = conn.execute(
            """
            SELECT * FROM sessions
            WHERE provider_id = ?
              AND lower(provider_conversation_id) = lower(?)
              AND session_id != ?
            LIMIT 1;
            """,
            (provider_id, provider_conversation_id, session_id),
        ).fetchone()
        return _row_to_bare_session(same_owner_row) if same_owner_row is not None else None

    @staticmethod
    def _insert_transition_in_conn(
        conn: sqlite3.Connection,
        transition: WorkerSessionTransitionRecord,
    ) -> None:
        """Insert immutable transition audit row referencing an appended Event."""
        conn.execute(
            """
            INSERT INTO worker_session_transitions (
                transition_id, session_id, task_id, run_id, from_status, to_status,
                actor_id, reason, evidence_summary, event_id, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                transition.transition_id,
                transition.session_id,
                transition.task_id,
                transition.run_id,
                transition.from_status,
                transition.to_status,
                transition.actor_id,
                transition.reason,
                transition.evidence_summary,
                transition.event_id,
                transition.timestamp.isoformat(),
            ),
        )

    def create_session_atomic(
        self,
        *,
        session: Session,
        binding: WorkerSessionBinding,
        transition: WorkerSessionTransitionRecord,
        events: Sequence[Event],
        new_run: Run | None = None,
        updated_run: Run | None = None,
        expected_run_version: int | None = None,
        updated_task: Task | None = None,
        expected_task_version: int | None = None,
    ) -> StoredWorkerSession:
        """Atomically persist Run link, Task link, logical Session, binding, transition, and Events."""
        validated_session = Session.model_validate_json(session.model_dump_json())
        validated_binding = WorkerSessionBinding.model_validate_json(binding.model_dump_json())
        validated_transition = WorkerSessionTransitionRecord.model_validate_json(
            transition.model_dump_json()
        )

        with self._store.transaction_scope() as conn:
            if validated_session.provider_conversation_id is not None and validated_session.provider_id is not None:
                conflict = self._find_conflict_in_conn(
                    conn,
                    validated_session.provider_id,
                    validated_session.provider_conversation_id,
                    session_id=validated_session.session_id,
                    task_id=validated_binding.task_id,
                    agent_id=validated_binding.agent_id,
                )
                if conflict is not None:
                    raise ProviderConversationCollisionError(
                        "PROVIDER_CONVERSATION_COLLISION",
                        (
                            f"Provider conversation '{validated_session.provider_conversation_id}' on provider "
                            f"'{validated_session.provider_id}' is already bound to session '{conflict.session_id}'."
                        ),
                    )

            if validated_binding.idempotency_key is not None:
                existing_idem = conn.execute(
                    """
                    SELECT session_id, request_fingerprint
                    FROM worker_session_bindings
                    WHERE task_id = ? AND idempotency_key = ?;
                    """,
                    (validated_binding.task_id, validated_binding.idempotency_key),
                ).fetchone()
                if existing_idem is not None:
                    if str(existing_idem["request_fingerprint"]) != (
                        validated_binding.request_fingerprint or ""
                    ):
                        raise DuplicateInvocationConflictError(
                            "IDEMPOTENCY_CONFLICT",
                            (
                                f"Idempotency key '{validated_binding.idempotency_key}' on Task "
                                f"'{validated_binding.task_id}' conflicts with session "
                                f"'{existing_idem['session_id']}'."
                            ),
                        )
                    raise DuplicateEntityError(
                        f"Idempotency key '{validated_binding.idempotency_key}' already materialized.",
                        entity_id=str(existing_idem["session_id"]),
                    )

            if new_run is not None:
                self._store.runs.create_run_on_connection(conn, new_run)
            elif updated_run is not None:
                if expected_run_version is None:
                    raise ValueError("expected_run_version is required when updating an existing Run.")
                self._store.runs.save_run_state_on_connection(
                    conn,
                    updated_run,
                    expected_version=expected_run_version,
                )

            if updated_task is not None:
                if expected_task_version is None:
                    raise ValueError("expected_task_version is required when updating Task.")
                self._store.tasks.save_task_state_on_connection(
                    conn,
                    updated_task,
                    expected_version=expected_task_version,
                )

            self._store.sessions.save_session(validated_session)
            self._append_events_in_conn(conn, events)

            try:
                conn.execute(
                    """
                    INSERT INTO worker_session_bindings (
                        session_id, task_id, run_id, agent_id, plan_id, plan_revision,
                        team_proposal_id, step_id, checklist_item_id,
                        pinned_manifest_revision, pinned_manifest_sha256,
                        provider_id, connection_id, model_id,
                        workspace_scope, private_context_namespace,
                        idempotency_key, request_fingerprint, execution_id,
                        correlation_id, reconciliation_status, reconciliation_reason,
                        record_version, last_audit_event_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        validated_binding.session_id,
                        validated_binding.task_id,
                        validated_binding.run_id,
                        validated_binding.agent_id,
                        validated_binding.plan_id,
                        validated_binding.plan_revision,
                        validated_binding.team_proposal_id,
                        validated_binding.step_id,
                        validated_binding.checklist_item_id,
                        validated_binding.pinned_manifest_revision,
                        validated_binding.pinned_manifest_sha256,
                        validated_binding.provider_id,
                        validated_binding.connection_id,
                        validated_binding.model_id,
                        validated_binding.workspace_scope,
                        validated_binding.private_context_namespace,
                        validated_binding.idempotency_key,
                        validated_binding.request_fingerprint,
                        validated_binding.execution_id,
                        validated_binding.correlation_id,
                        validated_binding.reconciliation_status,
                        validated_binding.reconciliation_reason,
                        1,
                        validated_binding.last_audit_event_id,
                        validated_binding.created_at.isoformat(),
                        validated_binding.updated_at.isoformat(),
                    ),
                )
                self._insert_transition_in_conn(conn, validated_transition)
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to persist worker session '{validated_binding.session_id}': {exc}",
                    entity_id=validated_binding.session_id,
                ) from exc

            row = conn.execute(
                f"{_STORED_SELECT_SQL} WHERE b.session_id = ?;",
                (validated_binding.session_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_worker_session(row)

    def transition_session_atomic(
        self,
        *,
        session: Session,
        binding: WorkerSessionBinding,
        expected_record_version: int,
        transition: WorkerSessionTransitionRecord,
        events: Sequence[Event],
        updated_run: Run | None = None,
        expected_run_version: int | None = None,
        updated_task: Task | None = None,
        expected_task_version: int | None = None,
    ) -> StoredWorkerSession:
        """Atomically transition Session state, verify conversation isolation, and append Events."""
        validated_session = Session.model_validate_json(session.model_dump_json())
        validated_binding = WorkerSessionBinding.model_validate_json(binding.model_dump_json())
        validated_transition = WorkerSessionTransitionRecord.model_validate_json(
            transition.model_dump_json()
        )

        with self._store.transaction_scope() as conn:
            current = conn.execute(
                "SELECT record_version, task_id, run_id, agent_id FROM worker_session_bindings WHERE session_id = ?;",
                (validated_binding.session_id,),
            ).fetchone()
            if current is None:
                raise WorkerSessionNotFoundError(
                    "SESSION_NOT_FOUND",
                    f"Worker session '{validated_binding.session_id}' does not exist.",
                )
            if int(current["record_version"]) != expected_record_version:
                raise StaleSessionVersionError(
                    "STALE_SESSION_VERSION",
                    (
                        f"Stale Worker session '{validated_binding.session_id}': expected "
                        f"record_version={expected_record_version}, found {int(current['record_version'])}."
                    ),
                )

            if validated_session.provider_conversation_id is not None and validated_session.provider_id is not None:
                conflict = self._find_conflict_in_conn(
                    conn,
                    validated_session.provider_id,
                    validated_session.provider_conversation_id,
                    session_id=validated_session.session_id,
                    task_id=validated_binding.task_id,
                    agent_id=validated_binding.agent_id,
                )
                if conflict is not None:
                    raise ProviderConversationCollisionError(
                        "PROVIDER_CONVERSATION_COLLISION",
                        (
                            f"Provider conversation '{validated_session.provider_conversation_id}' on provider "
                            f"'{validated_session.provider_id}' collides with session '{conflict.session_id}' "
                            f"(agent='{conflict.agent_id}', task='{conflict.task_id}')."
                        ),
                    )

            if updated_run is not None:
                if expected_run_version is None:
                    raise ValueError("expected_run_version is required when updating Run.")
                self._store.runs.save_run_state_on_connection(
                    conn,
                    updated_run,
                    expected_version=expected_run_version,
                )
            if updated_task is not None:
                if expected_task_version is None:
                    raise ValueError("expected_task_version is required when updating Task.")
                self._store.tasks.save_task_state_on_connection(
                    conn,
                    updated_task,
                    expected_version=expected_task_version,
                )

            self._store.sessions.save_session(validated_session)
            self._append_events_in_conn(conn, events)

            try:
                cursor = conn.execute(
                    """
                    UPDATE worker_session_bindings
                    SET model_id = ?,
                        execution_id = ?,
                        reconciliation_status = ?,
                        reconciliation_reason = ?,
                        record_version = record_version + 1,
                        last_audit_event_id = ?,
                        updated_at = ?
                    WHERE session_id = ? AND record_version = ?;
                    """,
                    (
                        validated_binding.model_id,
                        validated_binding.execution_id,
                        validated_binding.reconciliation_status,
                        validated_binding.reconciliation_reason,
                        validated_binding.last_audit_event_id,
                        validated_binding.updated_at.isoformat(),
                        validated_binding.session_id,
                        expected_record_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleSessionVersionError(
                        "STALE_SESSION_VERSION",
                        f"Concurrent update detected on Worker session '{validated_binding.session_id}'.",
                    )
                self._insert_transition_in_conn(conn, validated_transition)
            except sqlite3.Error as exc:
                raise RepositoryError(
                    "PERSISTENCE_FAILED",
                    f"Failed to update worker session '{validated_binding.session_id}': {exc}",
                    entity_id=validated_binding.session_id,
                ) from exc

            row = conn.execute(
                f"{_STORED_SELECT_SQL} WHERE b.session_id = ?;",
                (validated_binding.session_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_worker_session(row)

    def append_events_atomic(self, events: Sequence[Event]) -> list[EventId]:
        """Atomically append zero or more Events inside one SQLite transaction."""
        if not events:
            return []
        with self._store.transaction_scope() as conn:
            return self._append_events_in_conn(conn, events)

    def get_stored_session(self, session_id: str) -> StoredWorkerSession | None:
        """Fetch persisted logical Session + binding by UUID v4 identifier."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                f"{_STORED_SELECT_SQL} WHERE b.session_id = ?;",
                (session_id,),
            ).fetchone()
            return _row_to_stored_worker_session(row) if row is not None else None

    def list_stored_sessions(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        step_id: str | None = None,
    ) -> list[StoredWorkerSession]:
        """List persisted Worker Sessions filtered by Task, Run, Agent, or Step."""
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("b.task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("b.run_id = ?")
            params.append(run_id)
        if agent_id is not None:
            clauses.append("b.agent_id = ?")
            params.append(agent_id)
        if step_id is not None:
            clauses.append("b.step_id = ?")
            params.append(step_id)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = f"{_STORED_SELECT_SQL} {where_sql}ORDER BY b.created_at ASC, b.session_id ASC;"
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_stored_worker_session(row) for row in rows]

    def find_by_idempotency(
        self,
        task_id: str,
        idempotency_key: str,
    ) -> StoredWorkerSession | None:
        """Look up existing Worker Session created under ``(task_id, idempotency_key)``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                f"{_STORED_SELECT_SQL} WHERE b.task_id = ? AND b.idempotency_key = ?;",
                (task_id, idempotency_key),
            ).fetchone()
            return _row_to_stored_worker_session(row) if row is not None else None

    def find_provider_conversation_conflict(
        self,
        provider_id: str,
        provider_conversation_id: str,
        *,
        session_id: str,
        task_id: str,
        agent_id: str,
    ) -> Session | None:
        """Return conflicting persisted Session sharing ``(provider_id, provider_conversation_id)``."""
        with self._store.open_connection() as conn:
            return self._find_conflict_in_conn(
                conn,
                provider_id,
                provider_conversation_id,
                session_id=session_id,
                task_id=task_id,
                agent_id=agent_id,
            )

    def list_transitions(self, session_id: str) -> list[WorkerSessionTransitionRecord]:
        """Return ordered immutable state transitions recorded for ``session_id``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM worker_session_transitions
                WHERE session_id = ?
                ORDER BY timestamp ASC, transition_id ASC;
                """,
                (session_id,),
            ).fetchall()
            return [_row_to_transition(row) for row in rows]

    def record_reconciliation_atomic(
        self,
        session_id: str,
        *,
        expected_record_version: int,
        reconciliation_status: ReconciliationStatus,
        reconciliation_reason: str,
        timestamp: datetime,
        event: Event,
    ) -> StoredWorkerSession:
        """Persist reconciliation metadata and audit Event atomically."""
        with self._store.transaction_scope() as conn:
            self._append_events_in_conn(conn, [event])
            cursor = conn.execute(
                """
                UPDATE worker_session_bindings
                SET reconciliation_status = ?,
                    reconciliation_reason = ?,
                    record_version = record_version + 1,
                    last_audit_event_id = ?,
                    updated_at = ?
                WHERE session_id = ? AND record_version = ?;
                """,
                (
                    reconciliation_status,
                    reconciliation_reason,
                    event.event_id,
                    timestamp.isoformat(),
                    session_id,
                    expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT record_version FROM worker_session_bindings WHERE session_id = ?;",
                    (session_id,),
                ).fetchone()
                if current is None:
                    raise WorkerSessionNotFoundError(
                        "SESSION_NOT_FOUND",
                        f"Worker session '{session_id}' does not exist.",
                    )
                raise StaleSessionVersionError(
                    "STALE_SESSION_VERSION",
                    (
                        f"Stale Worker session '{session_id}': expected "
                        f"record_version={expected_record_version}, found {int(current['record_version'])}."
                    ),
                )
            row = conn.execute(
                f"{_STORED_SELECT_SQL} WHERE b.session_id = ?;",
                (session_id,),
            ).fetchone()
            assert row is not None
            return _row_to_stored_worker_session(row)
