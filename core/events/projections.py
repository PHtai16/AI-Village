"""Read-only Activity Timeline projections fed strictly from append-only Journal entries."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.contracts.event import Event, EventSource, EventType
from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    EventId,
    EvidenceId,
    RunId,
    SessionId,
    TaskId,
)
from core.events.journal import EventJournal, JournalEntry
from engines.antigravity.diagnostics import redact_secrets


class TimelineEntry(BaseModel):
    """Single safe, structured timeline row read directly from a ``JournalEntry``."""

    model_config = ConfigDict(extra="forbid")

    event_id: EventId
    sequence_number: int = Field(ge=1)
    timestamp: datetime
    event_type: EventType
    task_id: TaskId | None = None
    run_id: RunId | None = None
    session_id: SessionId | None = None
    actor_id: AgentId | None = None
    source: EventSource
    previous_status: str | None = None
    new_status: str | None = None
    artifact_id: ArtifactId | None = None
    evidence_id: EvidenceId | None = None
    tool_name: str | None = None
    provider_id: str | None = None
    connection_id: str | None = None
    model_id: str | None = None
    correlation_id: str | None = None
    legacy_session_ref: str | None = None
    summary: str = Field(min_length=1, max_length=1000)


class ActivityTimelineProjection(BaseModel):
    """Read-only Activity Timeline slice anchored by monotonic journal sequence."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId | None = None
    run_id: RunId | None = None
    correlation_id: str | None = None
    total_returned: int = Field(ge=0)
    last_sequence: int | None = None
    entries: list[TimelineEntry] = Field(default_factory=list)


def _format_summary(event: Event) -> str:
    """Build deterministic non-LLM, secret-redacted description for timeline rows."""
    prev_s = event.redacted_payload.get("previous_status")
    new_s = event.redacted_payload.get("new_status")
    if isinstance(prev_s, str) and isinstance(new_s, str):
        raw = f"{event.event_type}: {prev_s} -> {new_s}"
    elif event.event_type in {"tool.call_observed", "tool.call_completed", "engine.event_forwarded"}:
        tool_name = event.redacted_payload.get("tool_name") or event.redacted_payload.get("tool_id")
        if isinstance(tool_name, str) and tool_name.strip():
            raw = f"{event.event_type} tool={tool_name.strip()} source={event.source}"
        else:
            raw = f"{event.event_type} source={event.source}"
    elif event.event_type == "qa.verification_recorded":
        ver = event.redacted_payload.get("verification_status", "UNVERIFIED")
        raw = f"{event.event_type} status={ver} source={event.source}"
    else:
        raw = f"{event.event_type} source={event.source}"
    return redact_secrets(raw)[:1000]


def _entry_to_timeline_row(entry: JournalEntry) -> TimelineEntry:
    """Convert one ``JournalEntry`` into a read-only ``TimelineEntry``."""
    ev = entry.event
    payload = ev.redacted_payload
    prev_s = payload.get("previous_status")
    new_s = payload.get("new_status")
    legacy_sess = payload.get("legacy_session_ref")

    tool_name: str | None = None
    if ev.event_type in {"tool.call_observed", "tool.call_completed", "engine.event_forwarded"}:
        raw_tool = payload.get("tool_name") or payload.get("tool_id")
        if isinstance(raw_tool, str) and raw_tool.strip():
            tool_name = raw_tool.strip()

    return TimelineEntry(
        event_id=ev.event_id,
        sequence_number=entry.sequence_number,
        timestamp=ev.timestamp,
        event_type=ev.event_type,
        task_id=ev.task_id,
        run_id=ev.run_id,
        session_id=ev.session_id,
        actor_id=ev.actor_id,
        source=ev.source,
        previous_status=str(prev_s) if prev_s is not None else None,
        new_status=str(new_s) if new_s is not None else None,
        artifact_id=ev.artifact_id,
        evidence_id=ev.evidence_id,
        tool_name=tool_name,
        provider_id=ev.provider_id,
        connection_id=ev.connection_id,
        model_id=ev.model_id,
        correlation_id=ev.correlation_id,
        legacy_session_ref=str(legacy_sess) if isinstance(legacy_sess, str) else None,
        summary=_format_summary(ev),
    )


def project_timeline_from_entries(
    entries: Sequence[JournalEntry],
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    correlation_id: str | None = None,
) -> ActivityTimelineProjection:
    """Project an ordered sequence of ``JournalEntry`` records into an Activity Timeline."""
    rows = [_entry_to_timeline_row(entry) for entry in entries]
    return ActivityTimelineProjection(
        task_id=task_id,
        run_id=run_id,
        correlation_id=correlation_id,
        total_returned=len(rows),
        last_sequence=rows[-1].sequence_number if rows else None,
        entries=rows,
    )


def project_activity_timeline(
    journal: EventJournal,
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    correlation_id: str | None = None,
    after_sequence: int | None = None,
    limit: int | None = None,
) -> ActivityTimelineProjection:
    """Query an ``EventJournal`` and return a read-only ``ActivityTimelineProjection``."""
    entries = journal.read_entries(
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        correlation_id=correlation_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    return project_timeline_from_entries(
        entries,
        task_id=task_id,
        run_id=run_id,
        correlation_id=correlation_id,
    )
