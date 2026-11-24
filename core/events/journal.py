"""Thread-safe in-memory append-only Event Journal and State Machine/Runtime recorders."""

from __future__ import annotations

from datetime import datetime
import threading
from typing import Literal, Protocol, Sequence, runtime_checkable
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.contracts.compat import core_event_from_runtime_event
from core.contracts.event import Event
from core.runtime.contract import AgentRuntimeEvent
from core.tasks.transitions import (
    PairedTransitionResult,
    RunTransitionResult,
    TaskTransitionResult,
)
from engines.antigravity.diagnostics import redact_secrets

JournalErrorCode = Literal[
    "DUPLICATE_EVENT_CONFLICT",
    "INVALID_EVENT",
    "MISSING_ATTRIBUTION_ANCHOR",
    "UNSAFE_PAYLOAD",
    "INVALID_TIMESTAMP",
    "EMPTY_BATCH",
    "BATCH_VALIDATION_FAILED",
    "INVALID_QUERY_CURSOR",
    "MISSING_TRANSITION_EVENT",
]
AppendOutcomeStatus = Literal["APPENDED", "IDEMPOTENT_DUPLICATE", "REJECTED"]


class JournalError(BaseModel):
    """Structured fail-closed error returned when an Event cannot be recorded."""

    model_config = ConfigDict(extra="forbid")

    error_code: JournalErrorCode
    event_id: str | None = None
    batch_index: int | None = None
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def _scrub_message(cls, value: str) -> str:
        """Redact potential credentials before surfacing journal error text."""
        return redact_secrets(value)


class JournalEntry(BaseModel):
    """Immutable append-only envelope pairing a monotonic sequence number with a Core Event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence_number: int = Field(ge=1)
    event: Event
    batch_id: str | None = Field(default=None, min_length=1, max_length=64)


class AppendResult(BaseModel):
    """Explicit outcome separating new writes from idempotent duplicates and rejections."""

    model_config = ConfigDict(extra="forbid")

    status: AppendOutcomeStatus
    appended: bool
    is_duplicate: bool = False
    entry: JournalEntry | None = None
    error: JournalError | None = None


class BatchAppendResult(BaseModel):
    """All-or-nothing batch append outcome without partial sequence advancement."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["APPENDED", "REJECTED"]
    appended: bool
    batch_id: str | None = None
    entries: list[JournalEntry] = Field(default_factory=list)
    error: JournalError | None = None


@runtime_checkable
class EventJournal(Protocol):
    """Append-only Event Journal abstraction ready for future SQLite persistence."""

    def append(self, event: Event) -> AppendResult:
        """Validate and append one canonical Event."""

    def append_batch(self, events: Sequence[Event]) -> BatchAppendResult:
        """Atomically validate and append an ordered batch of Events."""

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
        """Return ordered defensive copies matching the requested filters."""

    def get_by_event_id(self, event_id: str) -> JournalEntry | None:
        """Return a defensive copy of the recorded entry matching ``event_id``."""

    def count(self) -> int:
        """Return total number of recorded entries in the journal."""


def _clone_event(event: Event) -> Event:
    """Validate via JSON round-trip to isolate internal journal state from caller mutation."""
    return Event.model_validate_json(event.model_dump_json())


def _clone_entry(entry: JournalEntry) -> JournalEntry:
    """Return an isolated deep copy of a ``JournalEntry``."""
    return JournalEntry(
        sequence_number=entry.sequence_number,
        event=_clone_event(entry.event),
        batch_id=entry.batch_id,
    )


def _classify_event_validation_error(exc: Exception, *, event_id: str | None = None, batch_index: int | None = None) -> JournalError:
    """Map Pydantic Event validation failures to specific ``JournalErrorCode`` values."""
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


class InMemoryEventJournal:
    """Thread-safe in-memory append-only Event Journal supporting monotonic sequencing.

    Durability boundary: stored strictly in memory for P04.03; process restarts do not
    preserve entries until SQLite repositories land in P04.04-P04.05.
    """

    def __init__(self) -> None:
        """Initialize an empty append-only journal protected by an ``RLock``."""
        self._lock = threading.RLock()
        self._entries: list[JournalEntry] = []
        self._by_event_id: dict[str, JournalEntry] = {}
        self._next_sequence: int = 1

    def append(self, event: Event) -> AppendResult:
        """Validate and append one canonical ``Event`` safely under lock."""
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
                error=_classify_event_validation_error(
                    exc,
                    event_id=str(raw_id) if raw_id is not None else None,
                ),
            )

        with self._lock:
            existing = self._by_event_id.get(validated_event.event_id)
            if existing is not None:
                if existing.event.model_dump(mode="json") == validated_event.model_dump(mode="json"):
                    return AppendResult(
                        status="IDEMPOTENT_DUPLICATE",
                        appended=False,
                        is_duplicate=True,
                        entry=_clone_entry(existing),
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

            entry = JournalEntry(
                sequence_number=self._next_sequence,
                event=validated_event,
                batch_id=None,
            )
            self._entries.append(entry)
            self._by_event_id[validated_event.event_id] = entry
            self._next_sequence += 1
            return AppendResult(
                status="APPENDED",
                appended=True,
                is_duplicate=False,
                entry=_clone_entry(entry),
                error=None,
            )

    def append_batch(self, events: Sequence[Event]) -> BatchAppendResult:
        """Pre-validate all Events and append atomically in memory without half-batches."""
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
        batch_seen_ids: set[str] = set()
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
                    error=_classify_event_validation_error(
                        exc,
                        event_id=str(raw_id) if raw_id is not None else None,
                        batch_index=idx,
                    ),
                )
            if cloned.event_id in batch_seen_ids:
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
            batch_seen_ids.add(cloned.event_id)
            validated_events.append(cloned)

        with self._lock:
            for idx, ev in enumerate(validated_events):
                if ev.event_id in self._by_event_id:
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
            created_entries: list[JournalEntry] = []
            for ev in validated_events:
                entry = JournalEntry(
                    sequence_number=self._next_sequence,
                    event=ev,
                    batch_id=batch_id,
                )
                self._entries.append(entry)
                self._by_event_id[ev.event_id] = entry
                self._next_sequence += 1
                created_entries.append(_clone_entry(entry))

            return BatchAppendResult(
                status="APPENDED",
                appended=True,
                batch_id=batch_id,
                entries=created_entries,
                error=None,
            )

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
        """Query entries ordered strictly by monotonic ``sequence_number``."""
        if after_sequence is not None and after_sequence < 0:
            raise ValueError("after_sequence cursor must be >= 0.")
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1.")

        with self._lock:
            matched: list[JournalEntry] = []
            for entry in self._entries:
                ev = entry.event
                if after_sequence is not None and entry.sequence_number <= after_sequence:
                    continue
                if task_id is not None and ev.task_id != task_id:
                    continue
                if run_id is not None and ev.run_id != run_id:
                    continue
                if session_id is not None and ev.session_id != session_id:
                    continue
                if correlation_id is not None and ev.correlation_id != correlation_id:
                    continue
                if event_type is not None and ev.event_type != event_type:
                    continue
                matched.append(_clone_entry(entry))
                if limit is not None and len(matched) >= limit:
                    break
            return matched

    def get_by_event_id(self, event_id: str) -> JournalEntry | None:
        """Retrieve a defensive copy of the entry matching ``event_id`` if recorded."""
        with self._lock:
            existing = self._by_event_id.get(event_id)
            return _clone_entry(existing) if existing is not None else None

    def count(self) -> int:
        """Return total number of recorded journal entries."""
        with self._lock:
            return len(self._entries)


def record_task_transition(
    journal: EventJournal,
    result: TaskTransitionResult,
) -> AppendResult:
    """Append the Event from an accepted ``TaskTransitionResult``; fail closed otherwise."""
    if not result.accepted or result.event is None:
        return AppendResult(
            status="REJECTED",
            appended=False,
            error=JournalError(
                error_code="MISSING_TRANSITION_EVENT",
                message=(
                    "Cannot append Task transition: transition was rejected or omitted an Event."
                ),
            ),
        )
    return journal.append(result.event)


def record_run_transition(
    journal: EventJournal,
    result: RunTransitionResult,
) -> AppendResult:
    """Append the Event from an accepted ``RunTransitionResult``; fail closed otherwise."""
    if not result.accepted or result.event is None:
        return AppendResult(
            status="REJECTED",
            appended=False,
            error=JournalError(
                error_code="MISSING_TRANSITION_EVENT",
                message=(
                    "Cannot append Run transition: transition was rejected or omitted an Event."
                ),
            ),
        )
    return journal.append(result.event)


def record_paired_transition(
    journal: EventJournal,
    result: PairedTransitionResult,
) -> BatchAppendResult:
    """Atomically append all Events produced by a ``PairedTransitionResult``."""
    if not result.accepted or not result.events:
        return BatchAppendResult(
            status="REJECTED",
            appended=False,
            error=JournalError(
                error_code="MISSING_TRANSITION_EVENT",
                message=(
                    "Cannot append paired transition: transition failed or produced no Events."
                ),
            ),
        )
    return journal.append_batch(result.events)


def record_runtime_event(
    journal: EventJournal,
    runtime_event: AgentRuntimeEvent,
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    connection_id: str | None = None,
    correlation_id: str | None = None,
    artifact_id: str | None = None,
    evidence_id: str | None = None,
    timestamp: datetime | None = None,
) -> AppendResult:
    """Project an ``AgentRuntimeEvent`` via ``core_event_from_runtime_event`` and append cleanly."""
    try:
        core_event = core_event_from_runtime_event(
            runtime_event,
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
            connection_id=connection_id,
            correlation_id=correlation_id,
            artifact_id=artifact_id,
            evidence_id=evidence_id,
            timestamp=timestamp,
        )
    except (ValidationError, ValueError) as exc:
        return AppendResult(
            status="REJECTED",
            appended=False,
            error=_classify_event_validation_error(exc),
        )
    return journal.append(core_event)
