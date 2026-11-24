"""Append-only Event Journal, deterministic Replay, and Activity Timeline projections."""

from __future__ import annotations

from core.events.journal import (
    AppendOutcomeStatus,
    AppendResult,
    BatchAppendResult,
    EventJournal,
    InMemoryEventJournal,
    JournalEntry,
    JournalError,
    JournalErrorCode,
    record_paired_transition,
    record_run_transition,
    record_runtime_event,
    record_task_transition,
)
from core.events.projections import (
    ActivityTimelineProjection,
    TimelineEntry,
    project_activity_timeline,
    project_timeline_from_entries,
)
from core.events.replay import (
    QaEventRecord,
    ReplayErrorCode,
    ReplayIssue,
    ReplayStatus,
    RunReplayResult,
    StatusStepRecord,
    TaskReplayResult,
    replay_run_history,
    replay_task_history,
    validate_journal_entries,
)

__all__ = [
    "ActivityTimelineProjection",
    "AppendOutcomeStatus",
    "AppendResult",
    "BatchAppendResult",
    "EventJournal",
    "InMemoryEventJournal",
    "JournalEntry",
    "JournalError",
    "JournalErrorCode",
    "QaEventRecord",
    "ReplayErrorCode",
    "ReplayIssue",
    "ReplayStatus",
    "RunReplayResult",
    "StatusStepRecord",
    "TaskReplayResult",
    "TimelineEntry",
    "project_activity_timeline",
    "project_timeline_from_entries",
    "record_paired_transition",
    "record_run_transition",
    "record_runtime_event",
    "record_task_transition",
    "replay_run_history",
    "replay_task_history",
    "validate_journal_entries",
]
