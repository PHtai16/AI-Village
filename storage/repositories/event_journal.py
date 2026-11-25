"""SQLite-backed append-only EventJournal implementation compatible with core.events.journal."""

from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from pydantic import ValidationError

from core.contracts.event import Event
from core.events.journal import (
    AppendResult,
    BatchAppendResult,
    JournalEntry,
    JournalError,
    JournalErrorCode,
)
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
from storage.repositories.errors import SchemaCompatibilityError


def _clone_event(event: Event) -> Event:
    """Validate and copy an Event through JSON round-trip to enforce all schema/secret guards."""
    return Event.model_validate_json(event.model_dump_json())


def _classify_validation_error(
    exc: Exception,
    *,
    event_id: str | None = None,
    batch_index: int | None = None,
) -> JournalError:
    """Map Pydantic validation errors to the canonical ``JournalErrorCode`` taxonomy."""
    detail = str(exc)
    lowered = detail.lower()
    if "attribution anchor" in lowered:
        code: JournalErrorCode = "MISSING_ATTRIBUTION_ANCHOR"
    elif "timezone" in lowered or "timestamp" in lowered:
        code = "INVALID_TIMESTAMP"
    elif "secret" in lowered or "credential" in lowered or "sensitive key" in lowered:
        code = "UNSAFE_PAYLOAD"
    else:
        code = "INVALID_EVENT"
    return JournalError(
        error_code=code,
        event_id=event_id,
        batch_index=batch_index,
        message=f"Rejected invalid Event: {detail}",
    )


def _row_to_entry(row: sqlite3.Row) -> JournalEntry:
    """Rehydrate a validated ``JournalEntry`` from a SQLite ``events`` row."""
    payload = json.loads(str(row["redacted_payload_json"]))
    event = Event.model_validate(
        {
            "event_id": row["event_id"],
            "timestamp": row["timestamp"],
            "event_type": row["event_type"],
            "source": row["source"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "actor_id": row["actor_id"],
            "provider_id": row["provider_id"],
            "connection_id": row["connection_id"],
            "model_id": row["model_id"],
            "artifact_id": row["artifact_id"],
            "evidence_id": row["evidence_id"],
            "correlation_id": row["correlation_id"],
            "redacted_payload": payload,
        }
    )
    return JournalEntry(
        sequence_number=int(row["sequence_number"]),
        event=event,
        batch_id=row["batch_id"],
    )


class SQLiteEventJournal:
    """Persistent, thread-safe append-only ``EventJournal`` backed by SQLite."""

    def __init__(
        self,
        config: SQLiteConfig | Path | str,
        *,
        expected_schema_version: int | None = None,
        shared_conn_provider: object | None = None,
    ) -> None:
        """Bind journal to an already-migrated SQLite database and verify schema compatibility."""
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
        self._lock = threading.RLock()
        self._closed = False
        self._memory_conn: sqlite3.Connection | None = None
        self._shared_conn_provider = shared_conn_provider

        if self._config.is_in_memory:
            conn = open_sqlite_connection(self._config)
            try:
                verify_schema_compatibility(conn, expected_version=resolved_version)
            except MigrationError as exc:
                conn.close()
                raise SchemaCompatibilityError(exc.message) from exc
            self._memory_conn = conn
        else:
            try:
                with sqlite_connection(self._config) as conn:
                    verify_schema_compatibility(conn, expected_version=resolved_version)
            except MigrationError as exc:
                raise SchemaCompatibilityError(exc.message) from exc

    def close(self) -> None:
        """Close any held connection and prevent further operations on this instance."""
        with self._lock:
            self._closed = True
            if self._memory_conn is not None:
                self._memory_conn.close()
                self._memory_conn = None

    def _ensure_open(self) -> None:
        """Fail closed if the journal instance has been explicitly closed."""
        if self._closed:
            raise RuntimeError("SQLiteEventJournal has been closed.")

    def _select_by_event_id_on_conn(
        self,
        conn: sqlite3.Connection,
        event_id: str,
    ) -> JournalEntry | None:
        """Fetch an existing journal entry inside the supplied connection."""
        row = conn.execute(
            """
            SELECT sequence_number, event_id, timestamp, event_type, source,
                   task_id, run_id, session_id, actor_id, provider_id,
                   connection_id, model_id, artifact_id, evidence_id,
                   correlation_id, redacted_payload_json, batch_id
            FROM events
            WHERE event_id = ?;
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_entry(row)

    def _insert_event_on_conn(
        self,
        conn: sqlite3.Connection,
        event: Event,
        *,
        batch_id: str | None = None,
    ) -> JournalEntry:
        """Insert a validated Event row without replacing existing records."""
        payload_json = json.dumps(event.redacted_payload, sort_keys=True, separators=(",", ":"))
        cursor = conn.execute(
            """
            INSERT INTO events (
                event_id, timestamp, event_type, source,
                task_id, run_id, session_id, actor_id,
                provider_id, connection_id, model_id,
                artifact_id, evidence_id, correlation_id,
                redacted_payload_json, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                event.event_id,
                event.timestamp.isoformat(),
                event.event_type,
                event.source,
                event.task_id,
                event.run_id,
                event.session_id,
                event.actor_id,
                event.provider_id,
                event.connection_id,
                event.model_id,
                event.artifact_id,
                event.evidence_id,
                event.correlation_id,
                payload_json,
                batch_id,
            ),
        )
        sequence_number = int(cursor.lastrowid)
        return JournalEntry(
            sequence_number=sequence_number,
            event=_clone_event(event),
            batch_id=batch_id,
        )

    def append_on_connection(
        self,
        conn: sqlite3.Connection,
        event: Event,
    ) -> AppendResult:
        """Append one Event within an existing SQLite transaction or connection."""
        if not isinstance(event, Event):
            return AppendResult(
                status="REJECTED",
                appended=False,
                error=JournalError(
                    error_code="INVALID_EVENT",
                    message=f"Expected Event model instance, got {type(event).__name__}.",
                ),
            )

        raw_id = getattr(event, "event_id", None)
        try:
            validated_event = _clone_event(event)
        except (ValidationError, ValueError, TypeError) as exc:
            return AppendResult(
                status="REJECTED",
                appended=False,
                error=_classify_validation_error(
                    exc,
                    event_id=str(raw_id) if raw_id is not None else None,
                ),
            )

        with sqlite_transaction(conn, immediate=True):
            existing = self._select_by_event_id_on_conn(conn, validated_event.event_id)
            if existing is not None:
                if existing.event.model_dump(mode="json") == validated_event.model_dump(mode="json"):
                    return AppendResult(
                        status="IDEMPOTENT_DUPLICATE",
                        appended=False,
                        is_duplicate=True,
                        entry=existing,
                        error=None,
                    )
                return AppendResult(
                    status="REJECTED",
                    appended=False,
                    is_duplicate=True,
                    entry=None,
                    error=JournalError(
                        error_code="DUPLICATE_EVENT_CONFLICT",
                        event_id=validated_event.event_id,
                        message=(
                            f"Event '{validated_event.event_id}' already exists with different content; "
                            "append-only journal refuses overwrite."
                        ),
                    ),
                )

            entry = self._insert_event_on_conn(conn, validated_event, batch_id=None)
            return AppendResult(
                status="APPENDED",
                appended=True,
                is_duplicate=False,
                entry=entry,
                error=None,
            )

    def append_batch_on_connection(
        self,
        conn: sqlite3.Connection,
        events: Sequence[Event],
    ) -> BatchAppendResult:
        """Pre-validate and append an ordered batch atomically within the given connection."""
        if not events:
            return BatchAppendResult(
                status="REJECTED",
                appended=False,
                error=JournalError(
                    error_code="EMPTY_BATCH",
                    message="append_batch requires at least one Event.",
                ),
            )

        validated_events: list[Event] = []
        seen_ids: set[str] = set()
        for idx, raw_event in enumerate(events):
            if not isinstance(raw_event, Event):
                return BatchAppendResult(
                    status="REJECTED",
                    appended=False,
                    error=JournalError(
                        error_code="BATCH_VALIDATION_FAILED",
                        batch_index=idx,
                        message=f"Batch item {idx} is not an Event instance.",
                    ),
                )
            raw_id = getattr(raw_event, "event_id", None)
            try:
                cloned = _clone_event(raw_event)
            except (ValidationError, ValueError, TypeError) as exc:
                return BatchAppendResult(
                    status="REJECTED",
                    appended=False,
                    error=_classify_validation_error(
                        exc,
                        event_id=str(raw_id) if raw_id is not None else None,
                        batch_index=idx,
                    ),
                )
            if cloned.event_id in seen_ids:
                return BatchAppendResult(
                    status="REJECTED",
                    appended=False,
                    error=JournalError(
                        error_code="DUPLICATE_EVENT_CONFLICT",
                        event_id=cloned.event_id,
                        batch_index=idx,
                        message=f"Duplicate event_id '{cloned.event_id}' within batch at index {idx}.",
                    ),
                )
            seen_ids.add(cloned.event_id)
            validated_events.append(cloned)

        with sqlite_transaction(conn, immediate=True):
            for idx, ev in enumerate(validated_events):
                existing = self._select_by_event_id_on_conn(conn, ev.event_id)
                if existing is not None:
                    return BatchAppendResult(
                        status="REJECTED",
                        appended=False,
                        error=JournalError(
                            error_code="DUPLICATE_EVENT_CONFLICT",
                            event_id=ev.event_id,
                            batch_index=idx,
                            message=(
                                f"Batch item {idx} collides with existing event_id '{ev.event_id}'; "
                                "entire batch aborted without changing sequence counter."
                            ),
                        ),
                    )

            batch_id = f"batch-{uuid.uuid4().hex[:12]}"
            created_entries: list[JournalEntry] = [
                self._insert_event_on_conn(conn, ev, batch_id=batch_id)
                for ev in validated_events
            ]
            return BatchAppendResult(
                status="APPENDED",
                appended=True,
                batch_id=batch_id,
                entries=created_entries,
                error=None,
            )

    def _active_shared_conn(self) -> sqlite3.Connection | None:
        """Return active thread-bound transaction connection from owning store, if any."""
        if callable(self._shared_conn_provider):
            return self._shared_conn_provider()
        return None

    def append(self, event: Event) -> AppendResult:
        """Validate and append one canonical ``Event`` safely to SQLite."""
        with self._lock:
            self._ensure_open()
            shared = self._active_shared_conn()
            if shared is not None:
                return self.append_on_connection(shared, event)
            if self._memory_conn is not None:
                return self.append_on_connection(self._memory_conn, event)
            with sqlite_connection(self._config) as conn:
                return self.append_on_connection(conn, event)

    def append_batch(self, events: Sequence[Event]) -> BatchAppendResult:
        """Atomically validate and append an ordered batch of Events to SQLite."""
        with self._lock:
            self._ensure_open()
            shared = self._active_shared_conn()
            if shared is not None:
                return self.append_batch_on_connection(shared, events)
            if self._memory_conn is not None:
                return self.append_batch_on_connection(self._memory_conn, events)
            with sqlite_connection(self._config) as conn:
                return self.append_batch_on_connection(conn, events)

    def _read_entries_on_conn(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        event_type: str | None = None,
        after_sequence: int | None = None,
        limit: int | None = None,
    ) -> list[JournalEntry]:
        """Execute parameterized query over ``events`` ordered strictly by ``sequence_number``."""
        clauses: list[str] = []
        params: list[object] = []

        if after_sequence is not None:
            clauses.append("sequence_number > ?")
            params.append(after_sequence)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)

        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        limit_sql = "LIMIT ? " if limit is not None else ""
        if limit is not None:
            params.append(limit)

        query = (
            "SELECT sequence_number, event_id, timestamp, event_type, source, "
            "task_id, run_id, session_id, actor_id, provider_id, "
            "connection_id, model_id, artifact_id, evidence_id, "
            "correlation_id, redacted_payload_json, batch_id "
            f"FROM events {where_sql}ORDER BY sequence_number ASC {limit_sql};"
        )
        rows = conn.execute(query, tuple(params)).fetchall()
        return [_row_to_entry(row) for row in rows]

    def read_entries(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
        event_type: str | None = None,
        after_sequence: int | None = None,
        limit: int | None = None,
    ) -> list[JournalEntry]:
        """Return ordered entries matching filters directly from SQLite."""
        if after_sequence is not None and after_sequence < 0:
            raise ValueError("after_sequence cursor must be >= 0.")
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1.")

        with self._lock:
            self._ensure_open()
            shared = self._active_shared_conn()
            if shared is not None:
                return self._read_entries_on_conn(
                    shared,
                    task_id=task_id,
                    run_id=run_id,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    event_type=event_type,
                    after_sequence=after_sequence,
                    limit=limit,
                )
            if self._memory_conn is not None:
                return self._read_entries_on_conn(
                    self._memory_conn,
                    task_id=task_id,
                    run_id=run_id,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    event_type=event_type,
                    after_sequence=after_sequence,
                    limit=limit,
                )
            with sqlite_connection(self._config) as conn:
                return self._read_entries_on_conn(
                    conn,
                    task_id=task_id,
                    run_id=run_id,
                    session_id=session_id,
                    correlation_id=correlation_id,
                    event_type=event_type,
                    after_sequence=after_sequence,
                    limit=limit,
                )

    def get_by_event_id(self, event_id: str) -> JournalEntry | None:
        """Return a copy of the recorded entry matching ``event_id`` from SQLite."""
        with self._lock:
            self._ensure_open()
            shared = self._active_shared_conn()
            if shared is not None:
                return self._select_by_event_id_on_conn(shared, event_id)
            if self._memory_conn is not None:
                return self._select_by_event_id_on_conn(self._memory_conn, event_id)
            with sqlite_connection(self._config) as conn:
                return self._select_by_event_id_on_conn(conn, event_id)

    def count(self) -> int:
        """Return total number of recorded entries in the SQLite ``events`` table."""
        with self._lock:
            self._ensure_open()
            shared = self._active_shared_conn()
            if shared is not None:
                row = shared.execute("SELECT COUNT(*) FROM events;").fetchone()
                return int(row[0]) if row is not None else 0
            if self._memory_conn is not None:
                row = self._memory_conn.execute("SELECT COUNT(*) FROM events;").fetchone()
                return int(row[0]) if row is not None else 0
            with sqlite_connection(self._config) as conn:
                row = conn.execute("SELECT COUNT(*) FROM events;").fetchone()
                return int(row[0]) if row is not None else 0
