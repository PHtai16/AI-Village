"""SQLite implementations for Task, Run, Session, and Metadata repositories."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any

from pydantic import ValidationError

from core.contracts.artifact import Artifact
from core.contracts.identifiers import reject_secret_text, validate_safe_payload
from core.contracts.references import ChecklistItem, ConnectionRef, NotificationRef, ProjectRef
from core.contracts.run import Run, RunErrorRef, RunTelemetryRef
from core.contracts.session import Session
from core.contracts.task import Task, TaskStatus
from storage.db.connection import (
    SQLiteConfig,
    open_sqlite_connection,
    resolve_database_path,
    sqlite_connection,
    sqlite_transaction,
)
from storage.db.migrations import (
    MigrationError,
    get_latest_migration_version,
    verify_schema_compatibility,
)
from storage.repositories.errors import (
    DuplicateEntityError,
    EntityNotFoundError,
    OwnershipMismatchError,
    RepositoryError,
    SchemaCompatibilityError,
    SecretViolationError,
    StaleStateError,
    StoredRun,
    StoredTask,
)
from storage.repositories.event_journal import SQLiteEventJournal

logger = logging.getLogger(__name__)


def _iso_or_none(value: datetime | None) -> str | None:
    """Serialize timezone-aware datetime to exact ISO-8601 text or None."""
    return value.isoformat() if value is not None else None


def _json_dumps(value: Any) -> str:
    """Serialize deterministic JSON without extra whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _map_sqlite_error(exc: sqlite3.Error, *, entity_id: str | None = None) -> RepositoryError:
    """Convert low-level SQLite integrity or operational failures into structured errors."""
    detail = str(exc)
    lowered = detail.lower()
    if "unique constraint failed" in lowered or "primary key" in lowered:
        return DuplicateEntityError(f"Duplicate record rejected by SQLite: {detail}", entity_id=entity_id)
    if "foreign key constraint failed" in lowered:
        return OwnershipMismatchError(
            f"Foreign key relationship constraint failed: {detail}",
            entity_id=entity_id,
        )
    if "credential_ref" in lowered:
        return SecretViolationError(
            f"Secret safety constraint failed: {detail}",
            entity_id=entity_id,
        )
    return RepositoryError("PERSISTENCE_FAILED", f"SQLite persistence failed: {detail}", entity_id=entity_id)


def _row_to_task(row: sqlite3.Row) -> Task:
    """Rehydrate and validate a Core ``Task`` from a SQLite ``tasks`` row."""
    return Task.model_validate(
        {
            "task_id": row["task_id"],
            "project_id": row["project_id"],
            "title": row["title"],
            "description": row["description"],
            "requested_by": row["requested_by"],
            "status": row["status"],
            "priority": row["priority"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "assigned_agent_ids": json.loads(str(row["assigned_agent_ids_json"])),
            "checklist_item_ids": json.loads(str(row["checklist_item_ids_json"])),
            "active_run_id": row["active_run_id"],
            "artifact_ids": json.loads(str(row["artifact_ids_json"])),
            "acceptance_criteria": json.loads(str(row["acceptance_criteria_json"])),
            "metadata": json.loads(str(row["metadata_json"])),
        }
    )


def _row_to_stored_task(row: sqlite3.Row) -> StoredTask:
    """Rehydrate ``StoredTask`` including revision counter and immutable replay baseline."""
    task = _row_to_task(row)
    baseline = Task.model_validate_json(str(row["baseline_json"]))
    return StoredTask(task=task, version=int(row["version"]), baseline=baseline)


def _row_to_run(row: sqlite3.Row) -> Run:
    """Rehydrate and validate a Core ``Run`` while preserving SQL NULL telemetry values."""
    error_ref_raw = row["error_reference_json"]
    error_ref = (
        RunErrorRef.model_validate_json(str(error_ref_raw))
        if error_ref_raw is not None
        else None
    )

    telemetry: RunTelemetryRef | None = None
    if row["telemetry_status"] is not None:
        telemetry = RunTelemetryRef(
            total_duration_ms=row["telemetry_total_duration_ms"],
            provider_duration_ms=row["telemetry_provider_duration_ms"],
            engine_call_count=row["telemetry_engine_call_count"],
            input_tokens=row["telemetry_input_tokens"],
            output_tokens=row["telemetry_output_tokens"],
            thinking_tokens=row["telemetry_thinking_tokens"],
            cached_tokens=row["telemetry_cached_tokens"],
            total_tokens=row["telemetry_total_tokens"],
            estimated_cost_usd=row["telemetry_estimated_cost_usd"],
            actual_cost_usd=row["telemetry_actual_cost_usd"],
            telemetry_status=row["telemetry_status"],
            telemetry_source=row["telemetry_source"],
        )

    return Run.model_validate(
        {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "runtime_id": row["runtime_id"],
            "invocation_id": row["invocation_id"],
            "correlation_id": row["correlation_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "session_ids": json.loads(str(row["session_ids_json"])),
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "model_id": row["model_id"],
            "artifact_ids": json.loads(str(row["artifact_ids_json"])),
            "error_reference": error_ref.model_dump() if error_ref is not None else None,
            "telemetry": telemetry.model_dump() if telemetry is not None else None,
        }
    )


def _row_to_stored_run(row: sqlite3.Row) -> StoredRun:
    """Rehydrate ``StoredRun`` including revision counter and immutable replay baseline."""
    run = _row_to_run(row)
    baseline = Run.model_validate_json(str(row["baseline_json"]))
    return StoredRun(run=run, version=int(row["version"]), baseline=baseline)


def _row_to_session(row: sqlite3.Row) -> Session:
    """Rehydrate and validate a logical ``Session`` record."""
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


def _row_to_checklist_item(row: sqlite3.Row) -> ChecklistItem:
    """Rehydrate and validate a ``ChecklistItem`` record."""
    return ChecklistItem.model_validate(
        {
            "checklist_item_id": row["checklist_item_id"],
            "task_id": row["task_id"],
            "title": row["title"],
            "description": row["description"],
            "assigned_agent_id": row["assigned_agent_id"],
            "status": row["status"],
            "order": int(row["order_index"]),
            "evidence_reference": row["evidence_reference"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }
    )


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    """Rehydrate and validate an ``Artifact`` record without altering verification state."""
    return Artifact.model_validate(
        {
            "artifact_id": row["artifact_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "owner_agent_id": row["owner_agent_id"],
            "relative_path": row["relative_path"],
            "storage_ref": row["storage_ref"],
            "content_type": row["content_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "version": row["version"],
            "verification_status": row["verification_status"],
            "evidence_reference": row["evidence_reference"],
            "created_at": row["created_at"],
        }
    )


def _row_to_notification(row: sqlite3.Row) -> NotificationRef:
    """Rehydrate and validate a ``NotificationRef`` record."""
    return NotificationRef.model_validate(
        {
            "notification_id": row["notification_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "event_id": row["event_id"],
            "severity": row["severity"],
            "notification_type": row["notification_type"],
            "created_at": row["created_at"],
        }
    )


def _row_to_connection(row: sqlite3.Row) -> ConnectionRef:
    """Rehydrate and validate a non-secret ``ConnectionRef`` record."""
    return ConnectionRef.model_validate(
        {
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "model_id": row["model_id"],
            "project_ref": row["project_ref"],
            "credential_ref": row["credential_ref"],
        }
    )


class SQLiteTaskRepository:
    """Controlled SQLite repository for Task snapshots and replay baselines."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        self._store = store

    def create_task_on_connection(self, conn: sqlite3.Connection, task: Task) -> StoredTask:
        """Insert a new Task and its immutable replay baseline inside an open connection."""
        validated = Task.model_validate_json(task.model_dump_json())
        validate_safe_payload(validated.metadata, "task.metadata")
        baseline_json = validated.model_dump_json()

        with sqlite_transaction(conn, immediate=True):
            existing = conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?;", (validated.task_id,)
            ).fetchone()
            if existing is not None:
                raise DuplicateEntityError(
                    f"Task '{validated.task_id}' already exists.",
                    entity_id=validated.task_id,
                )
            if validated.project_id is not None:
                proj = conn.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?;", (validated.project_id,)
                ).fetchone()
                if proj is None:
                    raise EntityNotFoundError(
                        f"Project '{validated.project_id}' referenced by Task does not exist.",
                        entity_id=validated.project_id,
                    )
            if validated.active_run_id is not None:
                run_row = conn.execute(
                    "SELECT task_id FROM runs WHERE run_id = ?;", (validated.active_run_id,)
                ).fetchone()
                if run_row is None or str(run_row["task_id"]) != validated.task_id:
                    raise OwnershipMismatchError(
                        f"Task '{validated.task_id}' cannot bind nonexistent or foreign active_run_id '{validated.active_run_id}'.",
                        entity_id=validated.task_id,
                    )

            try:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, project_id, title, description, requested_by,
                        status, priority, created_at, updated_at,
                        assigned_agent_ids_json, checklist_item_ids_json, active_run_id,
                        artifact_ids_json, acceptance_criteria_json, metadata_json,
                        version, baseline_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?);
                    """,
                    (
                        validated.task_id,
                        validated.project_id,
                        validated.title,
                        validated.description,
                        validated.requested_by,
                        validated.status,
                        validated.priority,
                        _iso_or_none(validated.created_at),
                        _iso_or_none(validated.updated_at),
                        _json_dumps(validated.assigned_agent_ids),
                        _json_dumps(validated.checklist_item_ids),
                        validated.active_run_id,
                        _json_dumps(validated.artifact_ids),
                        _json_dumps(validated.acceptance_criteria),
                        _json_dumps(validated.metadata),
                        baseline_json,
                    ),
                )
            except sqlite3.Error as exc:
                raise _map_sqlite_error(exc, entity_id=validated.task_id) from exc

            stored = self.get_stored_task_on_connection(conn, validated.task_id)
            assert stored is not None
            return stored

    def create_task(self, task: Task) -> StoredTask:
        """Persist a new Task along with its immutable initial replay baseline."""
        with self._store.open_connection() as conn:
            return self.create_task_on_connection(conn, task)

    def get_stored_task_on_connection(
        self,
        conn: sqlite3.Connection,
        task_id: str,
    ) -> StoredTask | None:
        """Read ``StoredTask`` directly inside an open connection."""
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?;", (task_id,)).fetchone()
        if row is None:
            return None
        return _row_to_stored_task(row)

    def get_stored_task(self, task_id: str) -> StoredTask | None:
        """Fetch the latest Task snapshot, optimistic version, and initial baseline."""
        with self._store.open_connection() as conn:
            return self.get_stored_task_on_connection(conn, task_id)

    def get_task(self, task_id: str) -> Task | None:
        """Fetch the latest Task snapshot, or None if not found."""
        stored = self.get_stored_task(task_id)
        return stored.task if stored is not None else None

    def get_raw_task_row(self, task_id: str) -> dict[str, Any] | None:
        """Return raw row dictionary for fault-tolerant recovery diagnostics."""
        with self._store.open_connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?;", (task_id,)).fetchone()
            return dict(row) if row is not None else None

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        project_id: str | None = None,
    ) -> list[Task]:
        """List persisted Task snapshots ordered deterministically by creation time and ID."""
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)

        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = f"SELECT * FROM tasks {where_sql}ORDER BY COALESCE(created_at, '') ASC, task_id ASC;"
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_task(row) for row in rows]

    def save_task_state_on_connection(
        self,
        conn: sqlite3.Connection,
        task: Task,
        *,
        expected_version: int,
    ) -> StoredTask:
        """Update Task snapshot under optimistic locking while leaving ``baseline_json`` immutable."""
        if expected_version < 1:
            raise ValueError("expected_version must be >= 1.")
        validated = Task.model_validate_json(task.model_dump_json())
        validate_safe_payload(validated.metadata, "task.metadata")

        with sqlite_transaction(conn, immediate=True):
            if validated.active_run_id is not None:
                run_row = conn.execute(
                    "SELECT task_id FROM runs WHERE run_id = ?;",
                    (validated.active_run_id,),
                ).fetchone()
                if run_row is None:
                    raise EntityNotFoundError(
                        f"Referenced active_run_id '{validated.active_run_id}' does not exist.",
                        entity_id=validated.active_run_id,
                    )
                if str(run_row["task_id"]) != validated.task_id:
                    raise OwnershipMismatchError(
                        (
                            f"Task '{validated.task_id}' cannot reference active_run_id "
                            f"'{validated.active_run_id}' owned by Task '{run_row['task_id']}'."
                        ),
                        entity_id=validated.task_id,
                    )

            try:
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET project_id = ?,
                        title = ?,
                        description = ?,
                        requested_by = ?,
                        status = ?,
                        priority = ?,
                        created_at = ?,
                        updated_at = ?,
                        assigned_agent_ids_json = ?,
                        checklist_item_ids_json = ?,
                        active_run_id = ?,
                        artifact_ids_json = ?,
                        acceptance_criteria_json = ?,
                        metadata_json = ?,
                        version = version + 1
                    WHERE task_id = ? AND version = ?;
                    """,
                    (
                        validated.project_id,
                        validated.title,
                        validated.description,
                        validated.requested_by,
                        validated.status,
                        validated.priority,
                        _iso_or_none(validated.created_at),
                        _iso_or_none(validated.updated_at),
                        _json_dumps(validated.assigned_agent_ids),
                        _json_dumps(validated.checklist_item_ids),
                        validated.active_run_id,
                        _json_dumps(validated.artifact_ids),
                        _json_dumps(validated.acceptance_criteria),
                        _json_dumps(validated.metadata),
                        validated.task_id,
                        expected_version,
                    ),
                )
            except sqlite3.Error as exc:
                raise _map_sqlite_error(exc, entity_id=validated.task_id) from exc

            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT version FROM tasks WHERE task_id = ?;", (validated.task_id,)
                ).fetchone()
                if current is None:
                    raise EntityNotFoundError(
                        f"Task '{validated.task_id}' does not exist.",
                        entity_id=validated.task_id,
                    )
                raise StaleStateError(
                    (
                        f"Stale Task update for '{validated.task_id}': expected version "
                        f"{expected_version}, found {int(current['version'])}."
                    ),
                    entity_id=validated.task_id,
                )

            stored = self.get_stored_task_on_connection(conn, validated.task_id)
            assert stored is not None
            return stored

    def save_task_state(self, task: Task, *, expected_version: int) -> StoredTask:
        """Persist a Task state change using optimistic version locking."""
        with self._store.open_connection() as conn:
            return self.save_task_state_on_connection(conn, task, expected_version=expected_version)


class SQLiteRunRepository:
    """Controlled SQLite repository for Run records and nullable telemetry."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        self._store = store

    def create_run_on_connection(self, conn: sqlite3.Connection, run: Run) -> StoredRun:
        """Insert a new Run bound to an existing Task inside an open connection."""
        validated = Run.model_validate_json(run.model_dump_json())
        baseline_json = validated.model_dump_json()
        tel = validated.telemetry

        with sqlite_transaction(conn, immediate=True):
            existing = conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?;", (validated.run_id,)
            ).fetchone()
            if existing is not None:
                raise DuplicateEntityError(
                    f"Run '{validated.run_id}' already exists.",
                    entity_id=validated.run_id,
                )
            parent_task = conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?;", (validated.task_id,)
            ).fetchone()
            if parent_task is None:
                raise EntityNotFoundError(
                    f"Cannot create Run '{validated.run_id}': parent Task '{validated.task_id}' does not exist.",
                    entity_id=validated.task_id,
                )

            try:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, task_id, runtime_id, invocation_id, correlation_id,
                        status, started_at, finished_at, session_ids_json,
                        provider_id, connection_id, model_id, artifact_ids_json,
                        error_reference_json,
                        telemetry_total_duration_ms, telemetry_provider_duration_ms,
                        telemetry_engine_call_count, telemetry_input_tokens,
                        telemetry_output_tokens, telemetry_thinking_tokens,
                        telemetry_cached_tokens, telemetry_total_tokens,
                        telemetry_estimated_cost_usd, telemetry_actual_cost_usd,
                        telemetry_status, telemetry_source,
                        version, baseline_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?);
                    """,
                    (
                        validated.run_id,
                        validated.task_id,
                        validated.runtime_id,
                        validated.invocation_id,
                        validated.correlation_id,
                        validated.status,
                        _iso_or_none(validated.started_at),
                        _iso_or_none(validated.finished_at),
                        _json_dumps(validated.session_ids),
                        validated.provider_id,
                        validated.connection_id,
                        validated.model_id,
                        _json_dumps(validated.artifact_ids),
                        validated.error_reference.model_dump_json()
                        if validated.error_reference is not None
                        else None,
                        tel.total_duration_ms if tel is not None else None,
                        tel.provider_duration_ms if tel is not None else None,
                        tel.engine_call_count if tel is not None else None,
                        tel.input_tokens if tel is not None else None,
                        tel.output_tokens if tel is not None else None,
                        tel.thinking_tokens if tel is not None else None,
                        tel.cached_tokens if tel is not None else None,
                        tel.total_tokens if tel is not None else None,
                        tel.estimated_cost_usd if tel is not None else None,
                        tel.actual_cost_usd if tel is not None else None,
                        tel.telemetry_status if tel is not None else None,
                        tel.telemetry_source if tel is not None else None,
                        baseline_json,
                    ),
                )
            except sqlite3.Error as exc:
                raise _map_sqlite_error(exc, entity_id=validated.run_id) from exc

            stored = self.get_stored_run_on_connection(conn, validated.run_id)
            assert stored is not None
            return stored

    def create_run(self, run: Run) -> StoredRun:
        """Persist a new Run bound to an existing Task with its initial baseline."""
        with self._store.open_connection() as conn:
            return self.create_run_on_connection(conn, run)

    def get_stored_run_on_connection(
        self,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> StoredRun | None:
        """Read ``StoredRun`` directly inside an open connection."""
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?;", (run_id,)).fetchone()
        if row is None:
            return None
        return _row_to_stored_run(row)

    def get_stored_run(self, run_id: str) -> StoredRun | None:
        """Fetch the latest Run snapshot, optimistic version, and initial baseline."""
        with self._store.open_connection() as conn:
            return self.get_stored_run_on_connection(conn, run_id)

    def get_run(self, run_id: str) -> Run | None:
        """Fetch the latest Run snapshot, or None if not found."""
        stored = self.get_stored_run(run_id)
        return stored.run if stored is not None else None

    def get_raw_run_row(self, run_id: str) -> dict[str, Any] | None:
        """Return raw row dictionary for fault-tolerant recovery diagnostics."""
        with self._store.open_connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?;", (run_id,)).fetchone()
            return dict(row) if row is not None else None

    def list_runs_for_task(self, task_id: str) -> list[Run]:
        """List all Runs belonging to a parent Task ordered deterministically."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE task_id = ?
                ORDER BY COALESCE(started_at, '') ASC, run_id ASC;
                """,
                (task_id,),
            ).fetchall()
            return [_row_to_run(row) for row in rows]

    def save_run_state_on_connection(
        self,
        conn: sqlite3.Connection,
        run: Run,
        *,
        expected_version: int,
    ) -> StoredRun:
        """Update Run snapshot under optimistic locking while preserving ``baseline_json``."""
        if expected_version < 1:
            raise ValueError("expected_version must be >= 1.")
        validated = Run.model_validate_json(run.model_dump_json())
        tel = validated.telemetry

        with sqlite_transaction(conn, immediate=True):
            existing_row = conn.execute(
                "SELECT task_id, version FROM runs WHERE run_id = ?;",
                (validated.run_id,),
            ).fetchone()
            if existing_row is None:
                raise EntityNotFoundError(
                    f"Run '{validated.run_id}' does not exist.",
                    entity_id=validated.run_id,
                )
            if str(existing_row["task_id"]) != validated.task_id:
                raise OwnershipMismatchError(
                    (
                        f"Run '{validated.run_id}' belongs to Task '{existing_row['task_id']}', "
                        f"refusing reassignment to '{validated.task_id}'."
                    ),
                    entity_id=validated.run_id,
                )

            try:
                cursor = conn.execute(
                    """
                    UPDATE runs
                    SET runtime_id = ?,
                        invocation_id = ?,
                        correlation_id = ?,
                        status = ?,
                        started_at = ?,
                        finished_at = ?,
                        session_ids_json = ?,
                        provider_id = ?,
                        connection_id = ?,
                        model_id = ?,
                        artifact_ids_json = ?,
                        error_reference_json = ?,
                        telemetry_total_duration_ms = ?,
                        telemetry_provider_duration_ms = ?,
                        telemetry_engine_call_count = ?,
                        telemetry_input_tokens = ?,
                        telemetry_output_tokens = ?,
                        telemetry_thinking_tokens = ?,
                        telemetry_cached_tokens = ?,
                        telemetry_total_tokens = ?,
                        telemetry_estimated_cost_usd = ?,
                        telemetry_actual_cost_usd = ?,
                        telemetry_status = ?,
                        telemetry_source = ?,
                        version = version + 1
                    WHERE run_id = ? AND task_id = ? AND version = ?;
                    """,
                    (
                        validated.runtime_id,
                        validated.invocation_id,
                        validated.correlation_id,
                        validated.status,
                        _iso_or_none(validated.started_at),
                        _iso_or_none(validated.finished_at),
                        _json_dumps(validated.session_ids),
                        validated.provider_id,
                        validated.connection_id,
                        validated.model_id,
                        _json_dumps(validated.artifact_ids),
                        validated.error_reference.model_dump_json()
                        if validated.error_reference is not None
                        else None,
                        tel.total_duration_ms if tel is not None else None,
                        tel.provider_duration_ms if tel is not None else None,
                        tel.engine_call_count if tel is not None else None,
                        tel.input_tokens if tel is not None else None,
                        tel.output_tokens if tel is not None else None,
                        tel.thinking_tokens if tel is not None else None,
                        tel.cached_tokens if tel is not None else None,
                        tel.total_tokens if tel is not None else None,
                        tel.estimated_cost_usd if tel is not None else None,
                        tel.actual_cost_usd if tel is not None else None,
                        tel.telemetry_status if tel is not None else None,
                        tel.telemetry_source if tel is not None else None,
                        validated.run_id,
                        validated.task_id,
                        expected_version,
                    ),
                )
            except sqlite3.Error as exc:
                raise _map_sqlite_error(exc, entity_id=validated.run_id) from exc

            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT version FROM runs WHERE run_id = ?;", (validated.run_id,)
                ).fetchone()
                actual = int(current["version"]) if current is not None else -1
                raise StaleStateError(
                    (
                        f"Stale Run update for '{validated.run_id}': expected version "
                        f"{expected_version}, found {actual}."
                    ),
                    entity_id=validated.run_id,
                )

            stored = self.get_stored_run_on_connection(conn, validated.run_id)
            assert stored is not None
            return stored

    def save_run_state(self, run: Run, *, expected_version: int) -> StoredRun:
        """Persist a Run state change using optimistic version locking."""
        with self._store.open_connection() as conn:
            return self.save_run_state_on_connection(conn, run, expected_version=expected_version)


class SQLiteSessionRepository:
    """SQLite repository for logical Sessions separate from provider conversation threads."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        self._store = store

    def save_session(self, session: Session) -> Session:
        """Create or update a logical Session record without mutating its UUID v4 identity."""
        validated = Session.model_validate_json(session.model_dump_json())
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                existing = conn.execute(
                    "SELECT task_id, run_id FROM sessions WHERE session_id = ?;",
                    (validated.session_id,),
                ).fetchone()
                if existing is not None:
                    if existing["task_id"] is not None and validated.task_id != existing["task_id"]:
                        raise OwnershipMismatchError(
                            f"Session '{validated.session_id}' cannot change parent task_id.",
                            entity_id=validated.session_id,
                        )
                    if existing["run_id"] is not None and validated.run_id != existing["run_id"]:
                        raise OwnershipMismatchError(
                            f"Session '{validated.session_id}' cannot change parent run_id.",
                            entity_id=validated.session_id,
                        )
                    try:
                        conn.execute(
                            """
                            UPDATE sessions
                            SET legacy_session_ref = ?,
                                agent_id = ?,
                                task_id = ?,
                                run_id = ?,
                                invocation_id = ?,
                                provider_id = ?,
                                connection_id = ?,
                                provider_conversation_id = ?,
                                workspace_reference = ?,
                                status = ?,
                                started_at = ?,
                                finished_at = ?
                            WHERE session_id = ?;
                            """,
                            (
                                validated.legacy_session_ref,
                                validated.agent_id,
                                validated.task_id,
                                validated.run_id,
                                validated.invocation_id,
                                validated.provider_id,
                                validated.connection_id,
                                validated.provider_conversation_id,
                                validated.workspace_reference,
                                validated.status,
                                _iso_or_none(validated.started_at),
                                _iso_or_none(validated.finished_at),
                                validated.session_id,
                            ),
                        )
                    except sqlite3.Error as exc:
                        raise _map_sqlite_error(exc, entity_id=validated.session_id) from exc
                else:
                    try:
                        conn.execute(
                            """
                            INSERT INTO sessions (
                                session_id, legacy_session_ref, agent_id, task_id, run_id,
                                invocation_id, provider_id, connection_id,
                                provider_conversation_id, workspace_reference, status,
                                started_at, finished_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                validated.session_id,
                                validated.legacy_session_ref,
                                validated.agent_id,
                                validated.task_id,
                                validated.run_id,
                                validated.invocation_id,
                                validated.provider_id,
                                validated.connection_id,
                                validated.provider_conversation_id,
                                validated.workspace_reference,
                                validated.status,
                                _iso_or_none(validated.started_at),
                                _iso_or_none(validated.finished_at),
                            ),
                        )
                    except sqlite3.Error as exc:
                        raise _map_sqlite_error(exc, entity_id=validated.session_id) from exc

                loaded = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?;",
                    (validated.session_id,),
                ).fetchone()
                assert loaded is not None
                return _row_to_session(loaded)

    def get_session(self, session_id: str) -> Session | None:
        """Fetch a logical Session by UUID v4 identifier."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?;", (session_id,)
            ).fetchone()
            return _row_to_session(row) if row is not None else None

    def list_sessions(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Session]:
        """List logical Sessions optionally filtered by Task or Run."""
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = (
            f"SELECT * FROM sessions {where_sql}"
            "ORDER BY COALESCE(started_at, '') ASC, session_id ASC;"
        )
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_session(row) for row in rows]


class SQLiteMetadataRepository:
    """SQLite repository for Project, Checklist, Artifact, Notification, and Connection metadata."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        self._store = store

    def save_project(self, project: ProjectRef) -> ProjectRef:
        """Persist an organizational ProjectRef grouping entry."""
        validated = ProjectRef.model_validate_json(project.model_dump_json())
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                existing = conn.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?;", (validated.project_id,)
                ).fetchone()
                try:
                    if existing is None:
                        conn.execute(
                            "INSERT INTO projects (project_id, name, description) VALUES (?, ?, ?);",
                            (validated.project_id, validated.name, validated.description),
                        )
                    else:
                        conn.execute(
                            "UPDATE projects SET name = ?, description = ? WHERE project_id = ?;",
                            (validated.name, validated.description, validated.project_id),
                        )
                except sqlite3.Error as exc:
                    raise _map_sqlite_error(exc, entity_id=validated.project_id) from exc

                row = conn.execute(
                    "SELECT project_id, name, description FROM projects WHERE project_id = ?;",
                    (validated.project_id,),
                ).fetchone()
                assert row is not None
                return ProjectRef.model_validate(dict(row))

    def get_project(self, project_id: str) -> ProjectRef | None:
        """Fetch a ProjectRef grouping record by ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT project_id, name, description FROM projects WHERE project_id = ?;",
                (project_id,),
            ).fetchone()
            return ProjectRef.model_validate(dict(row)) if row is not None else None

    def save_checklist_item(self, item: ChecklistItem) -> ChecklistItem:
        """Persist a ChecklistItem tied to an existing Task without bypassing completion guards."""
        validated = ChecklistItem.model_validate_json(item.model_dump_json())
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                existing = conn.execute(
                    "SELECT task_id FROM checklist_items WHERE checklist_item_id = ?;",
                    (validated.checklist_item_id,),
                ).fetchone()
                try:
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO checklist_items (
                                checklist_item_id, task_id, title, description,
                                assigned_agent_id, status, order_index,
                                evidence_reference, created_at, completed_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                validated.checklist_item_id,
                                validated.task_id,
                                validated.title,
                                validated.description,
                                validated.assigned_agent_id,
                                validated.status,
                                validated.order,
                                validated.evidence_reference,
                                _iso_or_none(validated.created_at),
                                _iso_or_none(validated.completed_at),
                            ),
                        )
                    else:
                        if str(existing["task_id"]) != validated.task_id:
                            raise OwnershipMismatchError(
                                f"ChecklistItem '{validated.checklist_item_id}' cannot change parent task_id.",
                                entity_id=validated.checklist_item_id,
                            )
                        conn.execute(
                            """
                            UPDATE checklist_items
                            SET title = ?,
                                description = ?,
                                assigned_agent_id = ?,
                                status = ?,
                                order_index = ?,
                                evidence_reference = ?,
                                created_at = ?,
                                completed_at = ?
                            WHERE checklist_item_id = ?;
                            """,
                            (
                                validated.title,
                                validated.description,
                                validated.assigned_agent_id,
                                validated.status,
                                validated.order,
                                validated.evidence_reference,
                                _iso_or_none(validated.created_at),
                                _iso_or_none(validated.completed_at),
                                validated.checklist_item_id,
                            ),
                        )
                except sqlite3.Error as exc:
                    raise _map_sqlite_error(exc, entity_id=validated.checklist_item_id) from exc

                row = conn.execute(
                    "SELECT * FROM checklist_items WHERE checklist_item_id = ?;",
                    (validated.checklist_item_id,),
                ).fetchone()
                assert row is not None
                return _row_to_checklist_item(row)

    def list_checklist_items(self, task_id: str) -> list[ChecklistItem]:
        """List ChecklistItems for a Task ordered strictly by ``order_index``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM checklist_items
                WHERE task_id = ?
                ORDER BY order_index ASC, checklist_item_id ASC;
                """,
                (task_id,),
            ).fetchall()
            return [_row_to_checklist_item(row) for row in rows]

    def save_artifact(self, artifact: Artifact) -> Artifact:
        """Persist Artifact metadata while keeping QA verification status untouched."""
        validated = Artifact.model_validate_json(artifact.model_dump_json())
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                existing = conn.execute(
                    "SELECT task_id, run_id FROM artifacts WHERE artifact_id = ?;",
                    (validated.artifact_id,),
                ).fetchone()
                try:
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO artifacts (
                                artifact_id, task_id, run_id, owner_agent_id,
                                relative_path, storage_ref, content_type, size_bytes,
                                sha256, version, verification_status, evidence_reference, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                validated.artifact_id,
                                validated.task_id,
                                validated.run_id,
                                validated.owner_agent_id,
                                validated.relative_path,
                                validated.storage_ref,
                                validated.content_type,
                                validated.size_bytes,
                                validated.sha256,
                                validated.version,
                                validated.verification_status,
                                validated.evidence_reference,
                                _iso_or_none(validated.created_at),
                            ),
                        )
                    else:
                        if (
                            str(existing["task_id"]) != validated.task_id
                            or str(existing["run_id"]) != validated.run_id
                        ):
                            raise OwnershipMismatchError(
                                f"Artifact '{validated.artifact_id}' cannot change parent Task/Run.",
                                entity_id=validated.artifact_id,
                            )
                        conn.execute(
                            """
                            UPDATE artifacts
                            SET owner_agent_id = ?,
                                relative_path = ?,
                                storage_ref = ?,
                                content_type = ?,
                                size_bytes = ?,
                                sha256 = ?,
                                version = ?,
                                verification_status = ?,
                                evidence_reference = ?,
                                created_at = ?
                            WHERE artifact_id = ?;
                            """,
                            (
                                validated.owner_agent_id,
                                validated.relative_path,
                                validated.storage_ref,
                                validated.content_type,
                                validated.size_bytes,
                                validated.sha256,
                                validated.version,
                                validated.verification_status,
                                validated.evidence_reference,
                                _iso_or_none(validated.created_at),
                                validated.artifact_id,
                            ),
                        )
                except sqlite3.Error as exc:
                    raise _map_sqlite_error(exc, entity_id=validated.artifact_id) from exc

                row = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?;",
                    (validated.artifact_id,),
                ).fetchone()
                assert row is not None
                return _row_to_artifact(row)

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        """Fetch Artifact metadata by ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?;", (artifact_id,)
            ).fetchone()
            return _row_to_artifact(row) if row is not None else None

    def list_artifacts(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Artifact]:
        """List Artifact metadata filtered by Task or Run."""
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = (
            f"SELECT * FROM artifacts {where_sql}"
            "ORDER BY COALESCE(created_at, '') ASC, artifact_id ASC;"
        )
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_artifact(row) for row in rows]

    def save_notification(self, notification: NotificationRef) -> NotificationRef:
        """Persist a NotificationRef linked to a real recorded Event."""
        validated = NotificationRef.model_validate_json(notification.model_dump_json())
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                try:
                    conn.execute(
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
                except sqlite3.Error as exc:
                    raise _map_sqlite_error(exc, entity_id=validated.notification_id) from exc

                row = conn.execute(
                    "SELECT * FROM notifications WHERE notification_id = ?;",
                    (validated.notification_id,),
                ).fetchone()
                assert row is not None
                return _row_to_notification(row)

    def list_notifications(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[NotificationRef]:
        """List NotificationRef records filtered by Task or Run."""
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = f"SELECT * FROM notifications {where_sql}ORDER BY created_at ASC, notification_id ASC;"
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_notification(row) for row in rows]

    def save_connection(self, connection: ConnectionRef) -> ConnectionRef:
        """Persist non-secret ConnectionRef metadata after rejecting credential strings."""
        try:
            validated = ConnectionRef.model_validate_json(connection.model_dump_json())
            if validated.credential_ref is not None:
                reject_secret_text(validated.credential_ref, "credential_ref")
        except (ValidationError, ValueError) as exc:
            raise SecretViolationError(
                f"Rejected unsafe ConnectionRef: {exc}",
                entity_id=getattr(connection, "connection_id", None),
            ) from exc

        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                existing = conn.execute(
                    """
                    SELECT 1 FROM connections
                    WHERE provider_id = ? AND connection_id = ?;
                    """,
                    (validated.provider_id, validated.connection_id),
                ).fetchone()
                try:
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO connections (
                                provider_id, connection_id, model_id, project_ref, credential_ref
                            ) VALUES (?, ?, ?, ?, ?);
                            """,
                            (
                                validated.provider_id,
                                validated.connection_id,
                                validated.model_id,
                                validated.project_ref,
                                validated.credential_ref,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE connections
                            SET model_id = ?,
                                project_ref = ?,
                                credential_ref = ?
                            WHERE provider_id = ? AND connection_id = ?;
                            """,
                            (
                                validated.model_id,
                                validated.project_ref,
                                validated.credential_ref,
                                validated.provider_id,
                                validated.connection_id,
                            ),
                        )
                except sqlite3.Error as exc:
                    raise _map_sqlite_error(exc, entity_id=validated.connection_id) from exc

                row = conn.execute(
                    """
                    SELECT provider_id, connection_id, model_id, project_ref, credential_ref
                    FROM connections
                    WHERE provider_id = ? AND connection_id = ?;
                    """,
                    (validated.provider_id, validated.connection_id),
                ).fetchone()
                assert row is not None
                return _row_to_connection(row)

    def get_connection(self, provider_id: str, connection_id: str) -> ConnectionRef | None:
        """Fetch ConnectionRef metadata by provider and connection ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                """
                SELECT provider_id, connection_id, model_id, project_ref, credential_ref
                FROM connections
                WHERE provider_id = ? AND connection_id = ?;
                """,
                (provider_id, connection_id),
            ).fetchone()
            return _row_to_connection(row) if row is not None else None

    def list_connections(self, *, provider_id: str | None = None) -> list[ConnectionRef]:
        """List stored ConnectionRef metadata ordered deterministically."""
        clauses: list[str] = []
        params: list[object] = []
        if provider_id is not None:
            clauses.append("provider_id = ?")
            params.append(provider_id)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        query = (
            "SELECT provider_id, connection_id, model_id, project_ref, credential_ref "
            f"FROM connections {where_sql}ORDER BY provider_id ASC, connection_id ASC;"
        )
        with self._store.open_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_row_to_connection(row) for row in rows]


class SQLiteRepositoryStore:
    """Unified SQLite repository container enforcing schema compatibility and clean closure."""

    def __init__(
        self,
        config: SQLiteConfig | Path | str,
        *,
        expected_schema_version: int | None = None,
    ) -> None:
        """Open a migrated SQLite database store and bind all domain repositories."""
        resolved_version = (
            expected_schema_version
            if expected_schema_version is not None
            else get_latest_migration_version()
        )
        self._config = (
            config
            if isinstance(config, SQLiteConfig)
            else SQLiteConfig(db_path=resolve_database_path(config))
        )
        if self._config.is_in_memory:
            raise SchemaCompatibilityError(
                "SQLiteRepositoryStore requires a file-backed database path for restart persistence."
            )

        self._closed = False
        self._local = threading.local()
        try:
            with sqlite_connection(self._config) as conn:
                verify_schema_compatibility(conn, expected_version=resolved_version)
        except MigrationError as exc:
            raise SchemaCompatibilityError(exc.message) from exc

        self.tasks = SQLiteTaskRepository(self)
        self.runs = SQLiteRunRepository(self)
        self.sessions = SQLiteSessionRepository(self)
        self.metadata = SQLiteMetadataRepository(self)
        self.events = SQLiteEventJournal(
            self._config,
            expected_schema_version=resolved_version,
            shared_conn_provider=lambda: getattr(self._local, "tx_conn", None),
        )
        from storage.repositories.agent_registry_repo import SQLiteAgentRegistryRepository
        from storage.repositories.approval_repo import SQLiteApprovalRepository
        from storage.repositories.lifecycle_repo import SQLiteLifecycleRepository
        from storage.repositories.planning_repo import SQLitePlanRepository
        from storage.repositories.team_repo import SQLiteTeamProposalRepository
        from storage.repositories.worker_session_repo import SQLiteWorkerSessionRepository

        self.lifecycle = SQLiteLifecycleRepository(self)
        self.approvals = SQLiteApprovalRepository(self)
        self.agents = SQLiteAgentRegistryRepository(self)
        self.plans = SQLitePlanRepository(self)
        self.teams = SQLiteTeamProposalRepository(self)
        self.worker_sessions = SQLiteWorkerSessionRepository(self)

    @property
    def config(self) -> SQLiteConfig:
        """Return immutable configuration bound to this repository store."""
        return self._config

    @contextmanager
    def open_connection(self) -> Iterator[sqlite3.Connection]:
        """Open a managed SQLite connection, reusing thread-bound transaction if active."""
        if self._closed:
            raise RepositoryError("STORE_CLOSED", "SQLiteRepositoryStore has been closed.")
        active: sqlite3.Connection | None = getattr(self._local, "tx_conn", None)
        if active is not None:
            yield active
            return
        with sqlite_connection(self._config) as conn:
            yield conn

    @contextmanager
    def transaction_scope(self) -> Iterator[sqlite3.Connection]:
        """Execute multi-repository work inside one atomic SQLite transaction without deadlocks."""
        if self._closed:
            raise RepositoryError("STORE_CLOSED", "SQLiteRepositoryStore has been closed.")
        existing: sqlite3.Connection | None = getattr(self._local, "tx_conn", None)
        if existing is not None:
            with sqlite_transaction(existing, immediate=True):
                yield existing
            return
        with self.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                self._local.tx_conn = conn
                try:
                    yield conn
                finally:
                    self._local.tx_conn = None

    def close(self) -> None:
        """Mark the repository store and its EventJournal closed."""
        self._closed = True
        self.events.close()

    def __enter__(self) -> SQLiteRepositoryStore:
        """Enter context-managed repository lifecycle."""
        if self._closed:
            raise RepositoryError("STORE_CLOSED", "SQLiteRepositoryStore has been closed.")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Close store cleanly on context exit."""
        self.close()
