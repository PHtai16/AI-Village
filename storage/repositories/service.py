"""Transactional application service coupling State Machine transitions with SQLite persistence."""

from __future__ import annotations

from collections.abc import Callable
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.contracts.event import Event
from core.contracts.run import Run
from core.contracts.task import Task
from core.events.journal import JournalEntry
from core.tasks.state_machine import (
    cancel_task_run,
    complete_run_only,
    fail_task_run,
    start_task_run,
    timeout_task_run,
    transition_run,
    transition_task,
)
from core.tasks.transitions import (
    PairedTransitionResult,
    RunTransitionRequest,
    RunTransitionResult,
    TaskTransitionRequest,
    TaskTransitionResult,
    TransitionContext,
)
from storage.db.connection import sqlite_transaction
from storage.repositories.errors import (
    EntityNotFoundError,
    OwnershipMismatchError,
    RepositoryError,
    StaleStateError,
    StoredRun,
)
from storage.repositories.sqlite_store import SQLiteRepositoryStore

PairedOperation = Literal["start", "complete_run_only", "fail", "timeout", "cancel"]


class PersistentTransitionOutcome(BaseModel):
    """Structured outcome returned by atomic State Machine + SQLite persistence operations."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    task: Task | None = None
    task_version: int | None = Field(default=None, ge=1)
    run: Run | None = None
    run_version: int | None = Field(default=None, ge=1)
    entries: list[JournalEntry] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    task_result: TaskTransitionResult | None = None
    run_result: RunTransitionResult | None = None
    paired_result: PairedTransitionResult | None = None


class PersistentWorkflowService:
    """Executes guarded State Machine transitions and Event Journal writes in one transaction."""

    def __init__(
        self,
        store: SQLiteRepositoryStore,
        *,
        before_event_append_hook: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Bind service to a ``SQLiteRepositoryStore`` with optional fault-injection hook."""
        self._store = store
        self._before_event_append_hook = before_event_append_hook

    def create_task(
        self,
        task: Task,
        *,
        created_event: Event | None = None,
        correlation_id: str | None = None,
    ) -> PersistentTransitionOutcome:
        """Persist a new Task and its optional ``task.created`` Event in one SQLite transaction."""
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                stored = self._store.tasks.create_task_on_connection(conn, task)
                event_to_write = created_event
                if event_to_write is None and stored.task.created_at is not None:
                    event_to_write = Event(
                        timestamp=stored.task.created_at,
                        event_type="task.created",
                        source="core_runtime",
                        task_id=stored.task.task_id,
                        correlation_id=correlation_id,
                        redacted_payload={
                            "initial_status": stored.task.status,
                            "priority": stored.task.priority,
                            "requested_by": stored.task.requested_by,
                        },
                    )
                entries: list[JournalEntry] = []
                if event_to_write is not None:
                    if event_to_write.task_id != stored.task.task_id:
                        raise OwnershipMismatchError(
                            f"created_event task_id '{event_to_write.task_id}' does not match '{stored.task.task_id}'.",
                            entity_id=stored.task.task_id,
                        )
                    if self._before_event_append_hook is not None:
                        self._before_event_append_hook(conn)
                    append_res = self._store.events.append_on_connection(conn, event_to_write)
                    if not append_res.appended and append_res.status == "REJECTED":
                        msg = append_res.error.message if append_res.error else "Event append rejected."
                        raise RepositoryError("PERSISTENCE_FAILED", msg, entity_id=stored.task.task_id)
                    if append_res.entry is not None:
                        entries.append(append_res.entry)

                return PersistentTransitionOutcome(
                    accepted=True,
                    task=stored.task,
                    task_version=stored.version,
                    entries=entries,
                )

    def create_run(self, run: Run) -> StoredRun:
        """Persist a new Run linked to an existing Task."""
        return self._store.runs.create_run(run)

    def apply_task_transition(
        self,
        task_id: str,
        target_status: str,
        context: TransitionContext,
        *,
        expected_task_version: int | None = None,
    ) -> PersistentTransitionOutcome:
        """Validate, transition, persist Task, and append Event inside one SQLite transaction."""
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                stored = self._store.tasks.get_stored_task_on_connection(conn, task_id)
                if stored is None:
                    raise EntityNotFoundError(f"Task '{task_id}' does not exist.", entity_id=task_id)
                if expected_task_version is not None and stored.version != expected_task_version:
                    raise StaleStateError(
                        (
                            f"Stale Task transition for '{task_id}': expected version "
                            f"{expected_task_version}, found {stored.version}."
                        ),
                        entity_id=task_id,
                    )

                result = transition_task(
                    TaskTransitionRequest(
                        task=stored.task,
                        target_status=target_status,
                        context=context,
                    )
                )
                if not result.accepted or result.task is None or result.event is None:
                    err = result.error
                    return PersistentTransitionOutcome(
                        accepted=False,
                        task=stored.task,
                        task_version=stored.version,
                        error_code=err.error_code if err is not None else "TRANSITION_REJECTED",
                        error_message=err.message if err is not None else "Task transition rejected.",
                        task_result=result,
                    )

                updated_stored = self._store.tasks.save_task_state_on_connection(
                    conn,
                    result.task,
                    expected_version=stored.version,
                )
                if self._before_event_append_hook is not None:
                    self._before_event_append_hook(conn)
                append_res = self._store.events.append_on_connection(conn, result.event)
                if append_res.status == "REJECTED" or append_res.entry is None:
                    msg = append_res.error.message if append_res.error else "Event append failed."
                    raise RepositoryError("PERSISTENCE_FAILED", msg, entity_id=task_id)

                return PersistentTransitionOutcome(
                    accepted=True,
                    task=updated_stored.task,
                    task_version=updated_stored.version,
                    entries=[append_res.entry],
                    task_result=result,
                )

    def apply_run_transition(
        self,
        run_id: str,
        target_status: str,
        context: TransitionContext,
        *,
        expected_run_version: int | None = None,
    ) -> PersistentTransitionOutcome:
        """Validate, transition, persist Run, and append Event inside one SQLite transaction."""
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                stored = self._store.runs.get_stored_run_on_connection(conn, run_id)
                if stored is None:
                    raise EntityNotFoundError(f"Run '{run_id}' does not exist.", entity_id=run_id)
                if expected_run_version is not None and stored.version != expected_run_version:
                    raise StaleStateError(
                        (
                            f"Stale Run transition for '{run_id}': expected version "
                            f"{expected_run_version}, found {stored.version}."
                        ),
                        entity_id=run_id,
                    )

                result = transition_run(
                    RunTransitionRequest(
                        run=stored.run,
                        target_status=target_status,
                        context=context,
                    )
                )
                if not result.accepted or result.run is None or result.event is None:
                    err = result.error
                    return PersistentTransitionOutcome(
                        accepted=False,
                        run=stored.run,
                        run_version=stored.version,
                        error_code=err.error_code if err is not None else "TRANSITION_REJECTED",
                        error_message=err.message if err is not None else "Run transition rejected.",
                        run_result=result,
                    )

                updated_stored = self._store.runs.save_run_state_on_connection(
                    conn,
                    result.run,
                    expected_version=stored.version,
                )
                if self._before_event_append_hook is not None:
                    self._before_event_append_hook(conn)
                append_res = self._store.events.append_on_connection(conn, result.event)
                if append_res.status == "REJECTED" or append_res.entry is None:
                    msg = append_res.error.message if append_res.error else "Event append failed."
                    raise RepositoryError("PERSISTENCE_FAILED", msg, entity_id=run_id)

                return PersistentTransitionOutcome(
                    accepted=True,
                    run=updated_stored.run,
                    run_version=updated_stored.version,
                    entries=[append_res.entry],
                    run_result=result,
                )

    def apply_paired_transition(
        self,
        operation: PairedOperation,
        task_id: str,
        run_id: str,
        context: TransitionContext,
        *,
        expected_task_version: int | None = None,
        expected_run_version: int | None = None,
        target_task_status: Literal["FAILED", "BLOCKED"] = "FAILED",
    ) -> PersistentTransitionOutcome:
        """Execute a paired Task + Run transition and append all Events atomically."""
        with self._store.open_connection() as conn:
            with sqlite_transaction(conn, immediate=True):
                stored_task = self._store.tasks.get_stored_task_on_connection(conn, task_id)
                if stored_task is None:
                    raise EntityNotFoundError(f"Task '{task_id}' does not exist.", entity_id=task_id)
                stored_run = self._store.runs.get_stored_run_on_connection(conn, run_id)
                if stored_run is None:
                    raise EntityNotFoundError(f"Run '{run_id}' does not exist.", entity_id=run_id)

                if expected_task_version is not None and stored_task.version != expected_task_version:
                    raise StaleStateError(
                        (
                            f"Stale Task version for '{task_id}': expected {expected_task_version}, "
                            f"found {stored_task.version}."
                        ),
                        entity_id=task_id,
                    )
                if expected_run_version is not None and stored_run.version != expected_run_version:
                    raise StaleStateError(
                        (
                            f"Stale Run version for '{run_id}': expected {expected_run_version}, "
                            f"found {stored_run.version}."
                        ),
                        entity_id=run_id,
                    )

                if operation == "start":
                    paired = start_task_run(stored_task.task, stored_run.run, context)
                elif operation == "complete_run_only":
                    paired = complete_run_only(stored_task.task, stored_run.run, context)
                elif operation == "fail":
                    paired = fail_task_run(
                        stored_task.task,
                        stored_run.run,
                        context,
                        target_task_status=target_task_status,
                    )
                elif operation == "timeout":
                    paired = timeout_task_run(
                        stored_task.task,
                        stored_run.run,
                        context,
                        target_task_status=target_task_status,
                    )
                elif operation == "cancel":
                    paired = cancel_task_run(stored_task.task, stored_run.run, context)
                else:
                    raise ValueError(f"Unsupported paired transition operation: {operation!r}")

                if not paired.accepted or paired.task is None or paired.run is None or not paired.events:
                    err = paired.error
                    return PersistentTransitionOutcome(
                        accepted=False,
                        task=stored_task.task,
                        task_version=stored_task.version,
                        run=stored_run.run,
                        run_version=stored_run.version,
                        error_code=err.error_code if err is not None else "TRANSITION_REJECTED",
                        error_message=err.message if err is not None else "Paired transition rejected.",
                        paired_result=paired,
                    )

                # Save Run first on start so active_run_id FK exists; on teardown Task clears active_run_id safely too.
                if operation == "start":
                    updated_run = self._store.runs.save_run_state_on_connection(
                        conn,
                        paired.run,
                        expected_version=stored_run.version,
                    )
                    updated_task = self._store.tasks.save_task_state_on_connection(
                        conn,
                        paired.task,
                        expected_version=stored_task.version,
                    )
                else:
                    updated_task = self._store.tasks.save_task_state_on_connection(
                        conn,
                        paired.task,
                        expected_version=stored_task.version,
                    )
                    updated_run = self._store.runs.save_run_state_on_connection(
                        conn,
                        paired.run,
                        expected_version=stored_run.version,
                    )

                if self._before_event_append_hook is not None:
                    self._before_event_append_hook(conn)

                if len(paired.events) == 1:
                    single_res = self._store.events.append_on_connection(conn, paired.events[0])
                    if single_res.status == "REJECTED" or single_res.entry is None:
                        msg = single_res.error.message if single_res.error else "Event append failed."
                        raise RepositoryError("PERSISTENCE_FAILED", msg, entity_id=task_id)
                    entries = [single_res.entry]
                else:
                    batch_res = self._store.events.append_batch_on_connection(conn, paired.events)
                    if not batch_res.appended or batch_res.status != "APPENDED":
                        msg = batch_res.error.message if batch_res.error else "Batch event append failed."
                        raise RepositoryError("PERSISTENCE_FAILED", msg, entity_id=task_id)
                    entries = batch_res.entries

                return PersistentTransitionOutcome(
                    accepted=True,
                    task=updated_task.task,
                    task_version=updated_task.version,
                    run=updated_run.run,
                    run_version=updated_run.version,
                    entries=entries,
                    paired_result=paired,
                )
