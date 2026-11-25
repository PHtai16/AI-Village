"""SQLite repository and core adapter for durable idempotency and owned execution tracking."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from core.contracts.event import Event
from core.contracts.run import Run
from core.contracts.task import Task
from core.tasks.transitions import TransitionContext
from storage.db.connection import sqlite_transaction
from storage.repositories.errors import DuplicateEntityError, EntityNotFoundError, RepositoryError
from storage.repositories.recovery import recover_task
from storage.repositories.service import PersistentWorkflowService
from storage.repositories.sqlite_store import SQLiteRepositoryStore


class IdempotencyConflictError(RepositoryError):
    """Raised when an idempotency key is reused with a different fingerprint or Task scope."""

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__("DUPLICATE_ENTITY", message, entity_id=entity_id)


class IdempotencyRecord(BaseModel):
    """Persisted idempotency key record preventing duplicate submissions across restarts."""

    model_config = ConfigDict(extra="forbid")

    scope: str
    idempotency_key: str
    request_fingerprint: str
    task_id: str | None = None
    run_id: str | None = None
    execution_id: str | None = None
    status: Literal["PENDING", "COMMITTED", "REJECTED"]
    response_payload: dict[str, Any]
    created_at: str
    updated_at: str


class ExecutionRecord(BaseModel):
    """Persisted owned execution metadata binding ``execution_id`` to ``(task_id, run_id)``."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    task_id: str
    run_id: str
    owner_token: str
    pid: int | None = None
    lifecycle_state: str
    cancel_requested_at: str | None = None
    timeout_deadline_at: str | None = None
    created_at: str
    updated_at: str


class SQLiteLifecycleRepository:
    """SQLite persistence for idempotency keys and owned run executions."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        self._store = store

    def reserve_or_get_idempotency_key(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request_fingerprint: str,
        task_id: str,
        run_id: str | None,
        timestamp: datetime,
    ) -> tuple[Literal["NEW", "DUPLICATE", "CONFLICT"], dict[str, Any]]:
        """Atomically reserve an idempotency key or return duplicate/conflict metadata."""
        ts_iso = timestamp.isoformat()
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                row = conn.execute(
                    "SELECT * FROM idempotency_keys WHERE scope = ? AND idempotency_key = ?;",
                    (scope, idempotency_key),
                ).fetchone()
                if row is not None:
                    if str(row["request_fingerprint"]) != request_fingerprint:
                        return (
                            "CONFLICT",
                            {
                                "run_id": row["run_id"],
                                "task_id": row["task_id"],
                                "message": (
                                    f"Idempotency key '{idempotency_key}' in scope '{scope}' already exists "
                                    "with a different request payload/fingerprint."
                                ),
                            },
                        )
                    if row["task_id"] is not None and str(row["task_id"]) != task_id:
                        return (
                            "CONFLICT",
                            {
                                "run_id": row["run_id"],
                                "task_id": row["task_id"],
                                "message": (
                                    f"Idempotency key '{idempotency_key}' belongs to Task '{row['task_id']}', "
                                    f"not '{task_id}'."
                                ),
                            },
                        )
                    return (
                        "DUPLICATE",
                        {
                            "run_id": row["run_id"],
                            "task_id": row["task_id"],
                            "execution_id": row["execution_id"],
                            "status": row["status"],
                            "response": json.loads(str(row["response_json"])),
                        },
                    )

                conn.execute(
                    """
                    INSERT INTO idempotency_keys (
                        scope, idempotency_key, request_fingerprint, task_id, run_id,
                        execution_id, status, response_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, NULL, 'PENDING', '{}', ?, ?);
                    """,
                    (scope, idempotency_key, request_fingerprint, task_id, ts_iso, ts_iso),
                )
                return ("NEW", {"task_id": task_id, "run_id": run_id})

    def commit_idempotency_result(
        self,
        *,
        scope: str,
        idempotency_key: str,
        task_id: str,
        run_id: str | None,
        execution_id: str | None,
        status: Literal["COMMITTED", "REJECTED"],
        response_payload: dict[str, Any],
        timestamp: datetime,
    ) -> None:
        """Persist final run/execution binding on a reserved idempotency key."""
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                conn.execute(
                    """
                    UPDATE idempotency_keys
                    SET task_id = ?,
                        run_id = ?,
                        execution_id = ?,
                        status = ?,
                        response_json = ?,
                        updated_at = ?
                    WHERE scope = ? AND idempotency_key = ?;
                    """,
                    (
                        task_id,
                        run_id,
                        execution_id,
                        status,
                        json.dumps(response_payload, sort_keys=True, separators=(",", ":")),
                        timestamp.isoformat(),
                        scope,
                        idempotency_key,
                    ),
                )

    def get_idempotency_record(self, scope: str, idempotency_key: str) -> IdempotencyRecord | None:
        """Fetch an idempotency record by scope and key."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_keys WHERE scope = ? AND idempotency_key = ?;",
                (scope, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            return IdempotencyRecord(
                scope=str(row["scope"]),
                idempotency_key=str(row["idempotency_key"]),
                request_fingerprint=str(row["request_fingerprint"]),
                task_id=row["task_id"],
                run_id=row["run_id"],
                execution_id=row["execution_id"],
                status=row["status"],
                response_payload=json.loads(str(row["response_json"])),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )

    def register_execution(
        self,
        *,
        execution_id: str,
        task_id: str,
        run_id: str,
        owner_token: str,
        pid: int | None,
        lifecycle_state: str,
        timeout_deadline_at: datetime | None,
        timestamp: datetime,
    ) -> ExecutionRecord:
        """Persist a new owned execution row bound to ``(task_id, run_id)``."""
        ts_iso = timestamp.isoformat()
        deadline_iso = timeout_deadline_at.isoformat() if timeout_deadline_at is not None else None
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                existing = conn.execute(
                    "SELECT 1 FROM run_executions WHERE execution_id = ?;",
                    (execution_id,),
                ).fetchone()
                if existing is not None:
                    raise DuplicateEntityError(
                        f"Execution '{execution_id}' already registered.",
                        entity_id=execution_id,
                    )
                conn.execute(
                    """
                    INSERT INTO run_executions (
                        execution_id, task_id, run_id, owner_token, pid,
                        lifecycle_state, cancel_requested_at, timeout_deadline_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?);
                    """,
                    (
                        execution_id,
                        task_id,
                        run_id,
                        owner_token,
                        pid,
                        lifecycle_state,
                        deadline_iso,
                        ts_iso,
                        ts_iso,
                    ),
                )
        loaded = self.get_execution(execution_id)
        assert loaded is not None
        return loaded

    def update_execution_state(
        self,
        *,
        execution_id: str,
        lifecycle_state: str,
        pid: int | None = None,
        cancel_requested_at: datetime | None = None,
        timestamp: datetime,
    ) -> None:
        """Update execution lifecycle state and timestamps cleanly."""
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                cursor = conn.execute(
                    """
                    UPDATE run_executions
                    SET lifecycle_state = ?,
                        pid = COALESCE(?, pid),
                        cancel_requested_at = COALESCE(?, cancel_requested_at),
                        updated_at = ?
                    WHERE execution_id = ?;
                    """,
                    (
                        lifecycle_state,
                        pid,
                        cancel_requested_at.isoformat() if cancel_requested_at is not None else None,
                        timestamp.isoformat(),
                        execution_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise EntityNotFoundError(
                        f"Execution '{execution_id}' does not exist.",
                        entity_id=execution_id,
                    )

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        """Fetch an execution row by ``execution_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM run_executions WHERE execution_id = ?;",
                (execution_id,),
            ).fetchone()
            if row is None:
                return None
            return ExecutionRecord(
                execution_id=str(row["execution_id"]),
                task_id=str(row["task_id"]),
                run_id=str(row["run_id"]),
                owner_token=str(row["owner_token"]),
                pid=row["pid"],
                lifecycle_state=str(row["lifecycle_state"]),
                cancel_requested_at=row["cancel_requested_at"],
                timeout_deadline_at=row["timeout_deadline_at"],
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )

    def list_executions_for_run(self, run_id: str) -> list[ExecutionRecord]:
        """List all persisted execution rows for ``run_id``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM run_executions WHERE run_id = ? ORDER BY created_at ASC, execution_id ASC;",
                (run_id,),
            ).fetchall()
            return [
                ExecutionRecord(
                    execution_id=str(row["execution_id"]),
                    task_id=str(row["task_id"]),
                    run_id=str(row["run_id"]),
                    owner_token=str(row["owner_token"]),
                    pid=row["pid"],
                    lifecycle_state=str(row["lifecycle_state"]),
                    cancel_requested_at=row["cancel_requested_at"],
                    timeout_deadline_at=row["timeout_deadline_at"],
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
                for row in rows
            ]


class SQLiteLifecyclePersistenceAdapter:
    """Adapter implementing ``core.tasks.lifecycle.LifecyclePersistencePort`` over SQLite."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        self._store = store
        self._workflow = PersistentWorkflowService(store)

    def get_task(self, task_id: str) -> Task | None:
        """Load latest Task snapshot."""
        return self._store.tasks.get_task(task_id)

    def get_run(self, run_id: str) -> Run | None:
        """Load latest Run snapshot."""
        return self._store.runs.get_run(run_id)

    def append_event(self, event: Event) -> int:
        """Append Event to durable SQLiteEventJournal and return assigned sequence."""
        res = self._store.events.append(event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Event append failed."
            raise RepositoryError("PERSISTENCE_FAILED", msg, entity_id=event.event_id)
        return res.entry.sequence_number

    def submit_run_atomically(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request_fingerprint: str,
        run: Run,
        timestamp: datetime,
    ) -> tuple[Literal["NEW", "DUPLICATE", "CONFLICT"], Run | None, str]:
        """Atomically verify idempotency key and persist Run + COMMITTED record in one transaction."""
        ts_iso = timestamp.isoformat()
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                row = conn.execute(
                    "SELECT * FROM idempotency_keys WHERE scope = ? AND idempotency_key = ?;",
                    (scope, idempotency_key),
                ).fetchone()
                if row is not None:
                    existing_run_id = row["run_id"]
                    existing_run = (
                        self._store.runs.get_stored_run_on_connection(conn, str(existing_run_id)).run
                        if existing_run_id is not None
                        and self._store.runs.get_stored_run_on_connection(conn, str(existing_run_id)) is not None
                        else None
                    )
                    if str(row["request_fingerprint"]) != request_fingerprint:
                        return (
                            "CONFLICT",
                            existing_run,
                            (
                                f"Idempotency key '{idempotency_key}' in scope '{scope}' already exists "
                                "with a different request payload/fingerprint."
                            ),
                        )
                    if row["task_id"] is not None and str(row["task_id"]) != run.task_id:
                        return (
                            "CONFLICT",
                            existing_run,
                            (
                                f"Idempotency key '{idempotency_key}' belongs to Task '{row['task_id']}', "
                                f"not '{run.task_id}'."
                            ),
                        )
                    return (
                        "DUPLICATE",
                        existing_run,
                        "Duplicate submission matched existing committed request; reused existing Run.",
                    )

                created_run = self._store.runs.create_run_on_connection(conn, run).run
                response_json = json.dumps(
                    {"run_id": created_run.run_id, "task_id": created_run.task_id},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                conn.execute(
                    """
                    INSERT INTO idempotency_keys (
                        scope, idempotency_key, request_fingerprint, task_id, run_id,
                        execution_id, status, response_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, 'COMMITTED', ?, ?, ?);
                    """,
                    (
                        scope,
                        idempotency_key,
                        request_fingerprint,
                        created_run.task_id,
                        created_run.run_id,
                        response_json,
                        ts_iso,
                        ts_iso,
                    ),
                )
                return ("NEW", created_run, "Accepted and persisted new Run.")

    def register_execution(
        self,
        *,
        execution_id: str,
        task_id: str,
        run_id: str,
        owner_token: str,
        pid: int | None,
        lifecycle_state: str,
        timeout_deadline_at: datetime | None,
        timestamp: datetime,
    ) -> None:
        """Persist owned execution metadata."""
        self._store.lifecycle.register_execution(
            execution_id=execution_id,
            task_id=task_id,
            run_id=run_id,
            owner_token=owner_token,
            pid=pid,
            lifecycle_state=lifecycle_state,
            timeout_deadline_at=timeout_deadline_at,
            timestamp=timestamp,
        )

    def update_execution_state(
        self,
        *,
        execution_id: str,
        lifecycle_state: str,
        pid: int | None = None,
        cancel_requested_at: datetime | None = None,
        timestamp: datetime,
    ) -> None:
        """Update execution lifecycle state."""
        self._store.lifecycle.update_execution_state(
            execution_id=execution_id,
            lifecycle_state=lifecycle_state,
            pid=pid,
            cancel_requested_at=cancel_requested_at,
            timestamp=timestamp,
        )

    def get_execution_owner_token(self, execution_id: str) -> str | None:
        """Fetch owner token for registered execution."""
        rec = self._store.lifecycle.get_execution(execution_id)
        return rec.owner_token if rec is not None else None

    def list_executions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """List stored execution dictionaries for ``run_id``."""
        return [rec.model_dump() for rec in self._store.lifecycle.list_executions_for_run(run_id)]

    def apply_paired_transition(
        self,
        operation: Literal["start", "complete_run_only", "fail", "timeout", "cancel"],
        task_id: str,
        run_id: str,
        context: TransitionContext,
        *,
        target_task_status: Literal["FAILED", "BLOCKED"] = "FAILED",
    ) -> tuple[bool, Task | None, Run | None, str | None, str | None]:
        """Run paired transition atomically through ``PersistentWorkflowService``."""
        out = self._workflow.apply_paired_transition(
            operation,
            task_id,
            run_id,
            context,
            target_task_status=target_task_status,
        )
        return out.accepted, out.task, out.run, out.error_code, out.error_message

    def recover_task_state(self, task_id: str) -> dict[str, Any]:
        """Run restart recovery diagnostics over SQLite snapshot and Event Journal."""
        rep = recover_task(self._store, task_id)
        return {
            "recovery_status": rep.recovery_status,
            "snapshot_status": rep.snapshot_task.status if rep.snapshot_task else None,
            "interrupted_work": [item.model_dump() for item in rep.interrupted_work],
        }
